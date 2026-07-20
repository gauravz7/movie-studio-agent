"""Movie Studio — a multi-user (per-LDAP) 3D chat UI for the movie-director ADK agent.

FastAPI backend that (1) hosts the `movie_director` agent and streams a turn over SSE, with sessions
PERSISTED per user via ADK's DatabaseSessionService (a returning LDAP sees their prior sessions +
generations), and (2) serves the generated media (images/video/audio) through a proxy so the
browser can render + download it. The agent's tool results only carry `movie://` links (or raw
paths) — this app resolves those links to real bytes ("links, not bytes": the host fetches).

Run:  MCP server on :9100 first, then
      GOOGLE_CLOUD_PROJECT=<proj> uv run --project movie_studio python app.py
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

# --- env defaults so the agent can reach Vertex + the MCP server -----------------------------
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("MCP_URL", "http://localhost:9100/mcp")

REPO = Path(__file__).resolve().parent.parent
GEN_ROOT = REPO / "movie" / "generated"          # where the pipeline saves all media
STATIC = Path(__file__).parent / "static"
USER_ID = "director1"                              # matches the agent instruction
APP_NAME = "movie_director"

# import the SAME agent the CLI uses (movie_agent/agent.py defines root_agent)
sys.path.insert(0, str(REPO / "movie_agent"))
from agent import root_agent, _mcp_headers  # noqa: E402

MCP_URL = os.environ["MCP_URL"]

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import DatabaseSessionService  # noqa: E402
from google.genai import types as genai_types  # noqa: E402

# Sessions are PERSISTED per user (their LDAP) in a SQLite DB via ADK's DatabaseSessionService, so a
# returning user sees ALL their previous sessions — with full transcript — even after a restart.
# Their generations/bibles are already user-partitioned on disk by movie_store
# (movie/generated/<ldap>/…), so those come back too, scoped to that user. Point SESSION_DB_URL at
# Postgres/Cloud SQL for a multi-instance deploy (SQLite is single-instance only).
SESSION_DB_URL = os.environ.get(
    "SESSION_DB_URL", f"sqlite+aiosqlite:///{Path(__file__).parent / 'sessions.db'}")

app = FastAPI(title="Movie Studio")
_session_service = DatabaseSessionService(db_url=SESSION_DB_URL)
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=_session_service)


def _clean_user(user: str) -> str:
    """Sanitise the LDAP into a safe workspace id (alnum/-/_). This id is the ADK session user_id
    AND the movie_store user_id, so a user's sessions + generations share one private namespace."""
    u = "".join(ch for ch in (user or "") if ch.isalnum() or ch in ("-", "_"))
    return u or USER_ID


def _session_meta(s) -> dict:
    st = getattr(s, "state", None) or {}
    return {"id": s.id, "title": st.get("title") or f"Session {s.id[:6]}",
            "created": getattr(s, "last_update_time", 0) or 0}


async def _list_metas(user_id: str) -> list[dict]:
    resp = await _session_service.list_sessions(app_name=APP_NAME, user_id=user_id)
    return sorted((_session_meta(s) for s in resp.sessions), key=lambda m: m["created"])

MEDIA_EXT = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
             ".mp4": "video", ".webm": "video", ".mp3": "audio", ".wav": "audio", ".ogg": "audio"}


# --------------------------------------------------------------------------- path safety
def _safe_component(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_"))
    if not safe:
        raise ValueError(f"unsafe path component: {value!r}")
    return safe


def _safe_name(name: str) -> str:
    n = Path(name).name                       # strips any dir / '..' traversal
    if not n or n.startswith("."):
        raise ValueError(f"unsafe asset name: {name!r}")
    return n


def _to_asset_url(value: str) -> tuple[str, str] | None:
    """Resolve a media reference -> (asset_url, filename). Accepts a movie:// URI OR a raw
    filesystem path under .../generated/<user>/<project>/<name>. Returns None if not local."""
    if not value or not isinstance(value, str):
        return None
    user = project = name = None
    if value.startswith("movie://"):
        parts = value[len("movie://"):].split("/")
        if len(parts) >= 3:
            user, project, name = parts[0], parts[1], parts[-1]
    elif "/generated/" in value.replace("\\", "/"):
        tail = value.replace("\\", "/").split("/generated/", 1)[1].split("/")
        if len(tail) >= 3:
            user, project, name = tail[0], tail[1], tail[-1]
    if not (user and project and name):
        return None
    return f"/asset/{user}/{project}/{name}", name


def _collect_media(result: dict) -> list[dict]:
    """Pull renderable media out of a tool result dict (dedup by url)."""
    out, seen = [], set()
    for key in ("resource_uri", "video_uri", "music_uri", "microshot_uri", "keyframe_uri",
                "establish_uri", "ref_uri", "style_ref"):
        resolved = _to_asset_url(result.get(key, ""))
        if not resolved:
            continue
        url, name = resolved
        if url in seen:
            continue
        seen.add(url)
        kind = MEDIA_EXT.get(Path(name).suffix.lower())
        if kind:
            out.append({"kind": kind, "url": url, "name": name})
    return out


def _parse_tool_result(response) -> dict:
    """ADK wraps MCP tool results as {'content': [{'type':'text','text': '<json>'}], ...}.
    Return the decoded JSON payload (merged), or {} if not parseable."""
    merged: dict = {}
    if isinstance(response, dict) and isinstance(response.get("content"), list):
        for block in response["content"]:
            txt = block.get("text") if isinstance(block, dict) else None
            if not txt:
                continue
            try:
                obj = json.loads(txt)
                if isinstance(obj, dict):
                    merged.update(obj)
            except (ValueError, TypeError):
                pass
    elif isinstance(response, dict):
        merged = response
    return merged


# --------------------------------------------------------------------------- SSE chat
def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


# The 4 tools the ADK SkillToolset exposes — used to label an activity step as a SKILL vs an MCP tool.
_SKILL_TOOLS = {"list_skills", "load_skill", "load_skill_resource", "run_skill_script"}


def _short(v, n: int = 80) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _arg_preview(args: dict) -> dict:
    """Compact, safe view of tool args for the activity panel (long prompts trimmed)."""
    return {k: _short(v, 90) for k, v in (args or {}).items()}


def _result_summary(result: dict) -> str:
    """One-line summary of a tool result for the activity panel (ids, status, qc, errors)."""
    if not isinstance(result, dict):
        return ""
    keys = ("project_id", "char_id", "scene_id", "shot_id", "status", "qc_ok", "qc_issues",
            "panels", "video_uri", "error", "note", "title")
    bits = [f"{k}={_short(result[k], 60)}" for k in keys if result.get(k) not in (None, "", [])]
    return ", ".join(bits[:5])


async def _run_turn(user_id: str, adk_sid: str, message: str):
    # make sure the session exists & is retained (e.g. the client sent an id we don't know yet,
    # or the process was restarted) — create one and tell the client its (possibly new) id.
    existing = await _session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=adk_sid)
    if existing is None:                       # unknown/stale id → make a fresh one and tell the client
        s = await _session_service.create_session(app_name=APP_NAME, user_id=user_id)
        adk_sid = s.id
        yield _sse({"type": "session", "id": adk_sid})

    msg = genai_types.Content(role="user", parts=[genai_types.Part(text=message)])
    try:
        async for event in runner.run_async(user_id=user_id, session_id=adk_sid, new_message=msg):
            for part in (event.content.parts if event.content else []) or []:
                fc = getattr(part, "function_call", None)
                if fc:
                    args = dict(fc.args) if getattr(fc, "args", None) else {}
                    yield _sse({"type": "tool", "name": fc.name})           # inline chip
                    yield _sse({"type": "activity",                          # activity panel
                                "kind": "skill" if fc.name in _SKILL_TOOLS else "tool",
                                "name": fc.name, "args": _arg_preview(args)})
                fr = getattr(part, "function_response", None)
                if fr:
                    result = _parse_tool_result(fr.response)
                    for m in _collect_media(result):          # paint the image the instant it's ready
                        yield _sse({"type": "media", "tool": fr.name, **m})
                    yield _sse({"type": "activity", "kind": "result",
                                "name": fr.name, "summary": _result_summary(result)})
                text = getattr(part, "text", None)
                if text:
                    yield _sse({"type": "text", "text": text})
                    yield _sse({"type": "activity", "kind": "thought", "text": _short(text, 220)})
    except Exception as e:  # surface errors to the UI instead of a dead stream
        yield _sse({"type": "error", "text": f"{type(e).__name__}: {e}"})
        yield _sse({"type": "activity", "kind": "error", "text": f"{type(e).__name__}: {e}"})
    yield _sse({"type": "done"})


@app.get("/chat/stream")
async def chat_stream(session: str = Query(...), message: str = Query(...),
                      user: str = Query(USER_ID)):
    return StreamingResponse(_run_turn(_clean_user(user), session, message),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- uploads (BYO character/prop)
async def _mcp_call(tool: str, args: dict) -> dict:
    """Call a movie-mcp tool directly (Studio registers an upload without going through the LLM).
    Uses the same Bearer auth as the agent so it works against an IAM-gated Cloud Run backend."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = _mcp_headers() or None
    async with streamablehttp_client(MCP_URL, headers=headers, timeout=60) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            if getattr(res, "structuredContent", None):
                return res.structuredContent
            return _parse_tool_result({"content": [c.model_dump() for c in (res.content or [])]})


_UPLOAD_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp"}


@app.post("/upload")
async def upload(request: Request, project: str = Query(...), name: str = Query(...),
                 user: str = Query(USER_ID), kind: str = Query("character"),
                 description: str = Query("")):
    """Upload a character or prop image and register it on the current project (no AI generation).
    The image is sent as the raw request body; it's saved into the project's shared media dir and
    then registered via import_character / import_prop so the director reuses it in scenes."""
    uid = _clean_user(user)
    data = await request.body()
    if not data:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    if len(data) > 15 * 1024 * 1024:
        return JSONResponse({"error": "file too large (max 15MB)"}, status_code=413)
    ext = _UPLOAD_EXT.get(request.headers.get("content-type", "").split(";")[0].strip().lower(), ".png")
    safe = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_")) or "upload"
    tag = "prop" if kind == "prop" else "char"
    fname = f"upload_{tag}_{safe}{ext}"
    try:
        d = GEN_ROOT / _safe_component(uid) / _safe_component(project)
    except ValueError:
        return JSONResponse({"error": "bad user/project"}, status_code=400)
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_bytes(data)
    tool = "import_prop" if kind == "prop" else "import_character"
    try:
        r = await _mcp_call(tool, {"user_id": uid, "project_id": project, "name": name,
                                   "description": description, "image_name": fname})
    except Exception as e:  # saved to disk but couldn't register — tell the UI
        return JSONResponse({"error": f"saved but registration failed: {type(e).__name__}: {e}"},
                            status_code=502)
    return JSONResponse({"ok": True, "kind": kind, "name": name,
                        "asset_url": f"/asset/{uid}/{project}/{fname}", **(r or {})})


# --------------------------------------------------------------------------- media proxy
@app.get("/asset/{user}/{project}/{name}")
def get_asset(user: str, project: str, name: str, download: int = 0):
    try:
        path = GEN_ROOT / _safe_component(user) / _safe_component(project) / _safe_name(name)
    except ValueError:
        return Response(status_code=400)
    if not path.exists() or not path.is_file():
        return Response(status_code=404)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # Generated files are MUTABLE — a regenerated shot/scene reuses the same filename. Tell the
    # browser to always revalidate so it never shows a stale cached image after a regenerate.
    return FileResponse(path, media_type=mime, filename=(name if download else None),
                        content_disposition_type=("attachment" if download else "inline"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/assets/{user}/{project}")
def list_assets(user: str, project: str):
    try:
        d = GEN_ROOT / _safe_component(user) / _safe_component(project)
    except ValueError:
        return JSONResponse([], status_code=400)
    if not d.is_dir():
        return JSONResponse([])
    items = []
    for p in sorted(d.iterdir()):
        kind = MEDIA_EXT.get(p.suffix.lower())
        if p.is_file() and kind:
            items.append({"kind": kind, "name": p.name,
                          "url": f"/asset/{user}/{project}/{p.name}"})
    return JSONResponse(items)


# --------------------------------------------------------------------------- static SPA
@app.get("/sessions")
async def list_sessions(user: str = Query(USER_ID)):
    """All PERSISTED sessions for this user (their LDAP), oldest first — survives restarts."""
    uid = _clean_user(user)
    return {"user_id": uid, "sessions": await _list_metas(uid)}


@app.post("/sessions")
async def create_session(user: str = Query(USER_ID), title: str = Query("")):
    """Start a NEW session for this user — an independent, parallel build with its own project."""
    uid = _clean_user(user)
    n = len((await _session_service.list_sessions(app_name=APP_NAME, user_id=uid)).sessions) + 1
    s = await _session_service.create_session(
        app_name=APP_NAME, user_id=uid, state={"title": title.strip() or f"Session {n}"})
    return _session_meta(s)


@app.delete("/sessions/{sid}")
async def delete_session(sid: str, user: str = Query(USER_ID)):
    """Delete a session and its transcript from the persistent store."""
    try:
        await _session_service.delete_session(
            app_name=APP_NAME, user_id=_clean_user(user), session_id=sid)
    except Exception:
        pass
    return {"ok": True}


@app.get("/sessions/{sid}/history")
async def session_history(sid: str, user: str = Query(USER_ID)):
    """Replay a persisted session's transcript (text + media) so a reload or switch restores it."""
    try:
        s = await _session_service.get_session(
            app_name=APP_NAME, user_id=_clean_user(user), session_id=sid)
    except Exception:
        s = None
    events: list[dict] = []
    for ev in (getattr(s, "events", None) or []):
        content = getattr(ev, "content", None)
        role = getattr(ev, "author", "") or (getattr(content, "role", "") if content else "")
        who = "you" if role == "user" else "director"
        for part in (getattr(content, "parts", None) or []):
            fr = getattr(part, "function_response", None)
            if fr:
                for m in _collect_media(_parse_tool_result(fr.response)):
                    events.append({"type": "media", "tool": fr.name, **m})
            txt = getattr(part, "text", None)
            if txt:
                events.append({"type": "text", "role": who, "text": txt})
    return {"events": events}


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8090)))
