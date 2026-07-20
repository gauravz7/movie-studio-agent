"""movie-mcp — an MCP server for AI movie production.

Composes three modules:
  * movie_store  — the per-user "story bible" (projects/characters/scenes/shots), user-scoped.
  * film_grammar — validates shot plans against film-grammar rules (180°, eyeline, 30°, …).
  * imagegen     — nano-banana keyframes (text->image and refs+text->image).

The pipeline: create_project -> add_character (reference sheet) -> establish_scene
(establishing frame + blocking) -> plan_scene (structured shots, VALIDATED) -> generate_shot
(keyframe composed from the scene's anchors, keeping characters/set consistent).

Multi-user: every tool takes user_id; projects are owned by one user and never shared.
Video (Veo) is an async job layered on top (stub here — see generate_shot_video).

Run:  GOOGLE_CLOUD_PROJECT=<proj> uv run python movie_server.py [--http --port 9100]
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger("movie.server")

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ResourceLink
from pydantic import BaseModel

import film_grammar as fg
import imagegen
import movie_store as store
import musicgen
import videogen

GEN_ROOT = Path(__file__).parent / "generated"

# Model used for the per-character insertions. Defaults to the lite model (fast, and the
# character reference sheets are still passed in as inputs on every insertion). Bump this via
# $HIFI_IMAGE_MODEL (e.g. gemini-3.1-flash-image / gemini-3-pro-image) if you want stronger
# identity retention on dense multi-character shots.
HIFI_IMAGE_MODEL = os.environ.get("HIFI_IMAGE_MODEL", "gemini-3.1-flash-lite-image")

# Vision film-editor critic: render -> review -> if not ok, regenerate WITH the critic's issues fed
# back into the prompt, up to QC_MAX_TRIES total attempts (1 render + up to N-1 corrective retries).
QC_MAX_TRIES = max(1, int(os.environ.get("QC_MAX_TRIES", "2")))


def _qc_refs(bible: dict, scene: dict | None = None, cast=None) -> list[str]:
    """Canonical reference images the critic compares against: style ref, the scene's establishing
    plate, and each cast member's sheet. Capped so we don't pass a huge image set to the critic."""
    refs: list[str] = []
    if bible.get("style_ref") and os.path.exists(bible["style_ref"]):
        refs.append(bible["style_ref"])
    if scene and scene.get("establish_uri") and os.path.exists(scene["establish_uri"]):
        refs.append(scene["establish_uri"])
    for sheet, _name in (cast or []):
        if sheet and os.path.exists(sheet):
            refs.append(sheet)
    return refs[:5]


def _gen_dir(user_id: str, project_id: str) -> Path:
    d = GEN_ROOT / user_id / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------------------
# Resource indirection (links, not bytes)
# --------------------------------------------------------------------------------------
# The pipeline stores real filesystem paths in the bible (it chains files together). But a
# tool RESULT should carry a small, readable *link* — not an absolute server path (useless
# to a remote client) and never the image bytes. So the image tools also return a
# `movie://<user>/<project>/<name>` resource_uri, and this templated resource reads the
# bytes back on demand — exactly the pattern learn-mcp uses with `image://`.
def _safe_component(value: str) -> str:
    """Reduce a user_id/project_id to safe path chars (alnum, dash, underscore)."""
    safe = "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_"))
    if not safe:
        raise ValueError(f"unsafe path component: {value!r}")
    return safe


def _safe_name(name: str) -> str:
    """Reduce an asset name to a bare filename (strips any directory / traversal)."""
    n = Path(name).name  # 'a/../b.png' -> 'b.png'; blocks path traversal
    if not n or n.startswith("."):
        raise ValueError(f"unsafe asset name: {name!r}")
    return n


def _asset_uri(user_id: str, project_id: str, path: str) -> str:
    """Map a generated file path -> its readable MCP resource URI."""
    return f"movie://{user_id}/{project_id}/{Path(path).name}"


def _known_anchors(bible: dict, scene_id: str) -> list[str]:
    """Anchor ids that exist in the bible for this scene (for validation)."""
    anchors: list[str] = []
    sc = bible.get("scenes", {}).get(scene_id, {})
    if sc.get("establish_uri"):
        anchors.append(f"establish:{scene_id}")
    anchors += [f"char:{cid}" for cid in bible.get("characters", {})]
    anchors += [f"loc:{lid}" for lid in bible.get("locations", {})]
    anchors += [f"frame:{s['shot_id']}" for s in bible.get("shots", []) if s.get("keyframe_uri")]
    return anchors


def _resolve_anchor_paths(bible: dict, shot: dict) -> list[str]:
    """Map a shot's anchor ids -> actual image file paths for keyframe composition."""
    paths: list[str] = []
    scene = bible.get("scenes", {}).get(shot["scene"], {})
    for a in shot.get("anchors", []):
        kind, _, val = a.partition(":")
        if kind == "establish" and scene.get("establish_uri"):
            paths.append(scene["establish_uri"])
        elif kind == "char":
            c = bible.get("characters", {}).get(val)
            if c and c.get("refs"):
                paths.append(c["refs"][0])
        elif kind == "loc":
            loc = bible.get("locations", {}).get(val)
            if loc and loc.get("refs"):
                paths.append(loc["refs"][0])
        elif kind == "frame":
            fs = next((s for s in bible["shots"] if s["shot_id"] == val), None)
            if fs and fs.get("keyframe_uri"):
                paths.append(fs["keyframe_uri"])
    return [p for p in paths if p and os.path.exists(p)]


def _match_char(bible: dict, token: str) -> str | None:
    """Resolve a character token (id, name, or id-prefix) -> real char_id, or None.
    The agent gets opaque hex ids back from add_character and frequently passes the NAME
    instead (or an id fragment); resolving all three keeps generate_shot from silently
    rendering a people-free plate."""
    token = (token or "").strip()
    if token.startswith("char:"):          # agent sometimes puts the whole 'char:<id>' in subject
        token = token.split(":", 1)[1].strip()
    if not token:
        return None
    chars = bible.get("characters", {})
    if token in chars:                                   # exact id
        return token
    low = token.lower()
    for cid, c in chars.items():                         # exact name (case-insensitive)
        if (c.get("name") or "").strip().lower() == low:
            return cid
    for cid in chars:                                    # unambiguous id-prefix
        if cid.startswith(token):
            return cid
    return None


def _resolve_cast(bible: dict, shot: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Build the (ref_path, name) cast for a shot from BOTH its `char:` anchors AND its
    `subject`, matching by id/name/prefix. Returns (cast, warnings). A named-but-unresolved
    character or a character with a missing reference sheet becomes a warning, not silence."""
    tokens: list[str] = []
    for a in shot.get("anchors", []):
        if a.startswith("char:"):
            tokens.append(a.split(":", 1)[1])
    if shot.get("subject"):
        tokens.append(shot["subject"])

    cast: list[tuple[str, str]] = []
    warnings: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        cid = _match_char(bible, tok)
        if not cid:
            # framing words / descriptive subjects aren't characters — remember but don't
            # warn unless the shot ends up with nobody at all
            if tok.lower() not in ("center", "centre", "left", "right", "none", "set", ""):
                unresolved.append(tok)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        c = bible["characters"][cid]
        ref = (c.get("refs") or [None])[0]
        if ref and os.path.exists(ref):
            cast.append((ref, c.get("name", cid)))
        else:  # a resolved character with no sheet is always worth flagging
            warnings.append(f"character '{c.get('name', cid)}' has no reference sheet on disk")
    if not cast and unresolved:
        warnings.append("could not resolve any character from " + repr(unresolved))
    return cast, warnings


# ======================================================================================
# Core pipeline functions (plain, importable — the local test drives these directly)
# ======================================================================================
def mv_create_project(user_id: str, title: str, style_guide: str = "") -> dict:
    b = store.create_project(user_id, title, style_guide)
    return {"project_id": b["project_id"], "title": b["title"], "style_guide": b["style_guide"]}


def mv_list_projects(user_id: str) -> list[dict]:
    return store.list_projects(user_id)


def mv_get_project(user_id: str, project_id: str) -> dict:
    return store.get_project(user_id, project_id)


def mv_set_style(user_id: str, project_id: str, style_guide: str) -> dict:
    store.update_style(user_id, project_id, style_guide)
    return {"ok": True, "style_guide": style_guide}


def mv_generate_style_ref(user_id: str, project_id: str, description: str = "",
                          prompt: str = "") -> dict:
    """Generate the project's global STYLE reference image (art style/palette/texture anchor).
    The AGENT may pass a rich `prompt`; otherwise one is built from the style_guide."""
    b = store.get_project(user_id, project_id)
    gen = prompt.strip() or (
        f"{b['style_guide']}. A single representative key frame that DEFINES the film's art "
        f"style, colour palette, lighting and texture. {description}. Establishes the consistent "
        f"visual look reused across every scene.")
    data, mime = imagegen.generate_image(gen, aspect_ratio="16:9")
    path = imagegen.save_bytes(data, _gen_dir(user_id, project_id), "style_ref", mime)
    store.set_style_ref(user_id, project_id, str(path))
    return {"style_ref": str(path)}


def mv_add_character(user_id: str, project_id: str, name: str, description: str,
                     prompt: str = "") -> dict:
    """Generate a canonical character reference sheet (the consistency anchor) and store it.

    The AGENT should pass a rich `prompt` (full image prompt in the chosen style — medium,
    wardrobe, lighting, detail). If omitted, a basic prompt is built from the description.
    NOTE: photorealistic *humans* are often refused by the image filter (IMAGE_PROHIBITED_CONTENT)."""
    b = store.get_project(user_id, project_id)
    gen = prompt.strip() or (
        f"Character reference sheet of an original, fictional  character: {description}. "
        f"Front view and 3/4 view, neutral background, consistent features. "
        f"Rendered in this exact visual style: {b['style_guide']}.")
    # The sheet is the IDENTITY anchor every later shot is composed from, so QC it: a clean,
    # well-formed sheet in the project style, with corrective feedback fed back on a retry.
    stem = f"char_{name.lower().replace(' ', '_')}"
    out = _gen_dir(user_id, project_id)
    expects = (f"a clean character reference sheet of {description}; front and 3/4 views; consistent "
               "features; well-formed hands/face/limbs; project art style; no gibberish text.")
    style_ref = [b["style_ref"]] if b.get("style_ref") and os.path.exists(b["style_ref"]) else []
    review = {"ok": True, "score": 1.0, "issues": ""}
    src = ""
    feedback = ""
    for attempt in range(QC_MAX_TRIES):
        data, mime = imagegen.generate_image(gen + feedback, aspect_ratio="1:1")
        src = str(imagegen.save_bytes(data, out, f"{stem}_a{attempt}", mime))
        review = imagegen.review_image(src, expects, refs=style_ref)
        if review["ok"]:
            break
        feedback = (" FIX these problems: " + review["issues"]) if review.get("issues") else ""
    path = out / f"{stem}.png"
    path.write_bytes(Path(src).read_bytes())
    cid = store.add_character(user_id, project_id, name, description, refs=[str(path)])
    return {"char_id": cid, "name": name, "ref_uri": str(path),
            "qc_ok": review["ok"], "qc_score": review.get("score"),
            "qc_issues": review.get("issues", "")}


def mv_establish_scene(user_id: str, project_id: str, scene_id: str, description: str,
                       lighting: str = "", blocking: dict | None = None, prompt: str = "") -> dict:
    """Generate the scene's establishing frame (locks set + lighting) and record blocking.
    The AGENT may pass a rich `prompt` for the (people-free) set; else one is built."""
    b = store.get_project(user_id, project_id)
    # People-FREE set plate, conditioned on the global style reference so every set shares the
    # same art style / palette / texture. Characters come only from the character sheets.
    base = prompt.strip() or (
        f"{b['style_guide']}. Establishing wide shot of the SET only: {description}. "
        f"Lighting: {lighting}. EMPTY room — NO people, NO characters. "
        f"Show the space, layout, props and lighting clearly.")
    style_ref = b.get("style_ref")
    if style_ref and os.path.exists(style_ref):
        data, mime = imagegen.compose_image(
            "Match the ART STYLE, colour palette, brushwork and texture of the reference image "
            "EXACTLY. " + base, [style_ref])
    else:
        data, mime = imagegen.generate_image(base, aspect_ratio="16:9")
    path = imagegen.save_bytes(data, _gen_dir(user_id, project_id), f"establish_{scene_id}", mime)
    store.set_scene(user_id, project_id, scene_id, lighting=lighting,
                    blocking=blocking or {}, establish_uri=str(path))
    return {"scene": scene_id, "establish_uri": str(path)}


def mv_plan_scene(user_id: str, project_id: str, scene_id: str, shots: list[dict]) -> dict:
    """Validate a structured shot plan against film grammar; persist only if error-free."""
    b = store.get_project(user_id, project_id)
    known = _known_anchors(b, scene_id)
    fg_shots = [
        fg.Shot(id=sd["id"], scene=scene_id, subject=sd["subject"],
                camera=fg.Camera(**sd.get("camera", {})),
                anchors=sd.get("anchors", []), side=sd.get("side", "center"),
                faces=sd.get("faces", "center"), movement=sd.get("movement", "none"),
                intent=sd.get("intent", ""))
        for sd in shots
    ]
    plan = fg.ShotPlan(scene=scene_id, shots=fg_shots, known_anchors=known)
    violations = fg.validate_plan(plan)
    errors = [v for v in violations if v.severity == "error"]
    persisted = False
    if not errors:
        for sd in shots:
            store.add_shot(user_id, project_id, {
                "shot_id": sd["id"], "scene": scene_id, "subject": sd["subject"],
                "camera": sd.get("camera", {}), "anchors": sd.get("anchors", []),
                "intent": sd.get("intent", ""), "keyframe_uri": None,
                "video_uri": None, "status": "planned"})
        persisted = True
    return {"errors": len(errors), "persisted": persisted,
            "violations": [v.model_dump() for v in violations]}


def mv_generate_shot(user_id: str, project_id: str, shot_id: str,
                     prompt: str = "", qc: bool = True) -> dict:
    """Compose the KEYFRAME by ITERATIVE insertion (one character at a time), then run a vision
    QC pass and REGENERATE if a required character is missing / hands are mangled / extras appear.

    The AGENT should pass a rich `prompt` (the vivid scene description); it drives each insertion.
    """
    b = store.get_project(user_id, project_id)
    shot = next(s for s in b["shots"] if s["shot_id"] == shot_id)
    scene = b["scenes"].get(shot["scene"], {})
    style_ref = b.get("style_ref")
    out = _gen_dir(user_id, project_id)
    action = prompt.strip() or shot.get("intent", "")

    cast, cast_warnings = _resolve_cast(b, shot)

    # Identity retention across 2+ characters is where the lite model drops/blends faces, so
    # use the higher-fidelity image model for the per-character insertions once the cast >= 2.
    ins_model = HIFI_IMAGE_MODEL if len(cast) >= 2 else None

    def render_once(tag: str, feedback: str = "") -> str:
        # base = styled, people-free set plate; else synthesize one from the style reference
        if scene.get("establish_uri") and os.path.exists(scene["establish_uri"]):
            running = scene["establish_uri"]
        else:
            base = f"{b['style_guide']}. {scene.get('lighting', '')}. Empty set, NO people."
            if style_ref and os.path.exists(style_ref):
                d, m = imagegen.compose_image("Match the art style of the reference EXACTLY. " + base, [style_ref])
            else:
                d, m = imagegen.generate_image(base)
            running = str(imagegen.save_bytes(d, out, f"base_{shot_id}", m))
        # insert one character at a time (2 refs per call → reliable identity). Naming the
        # characters ALREADY in the frame is the key signal that stops the model from redrawing,
        # replacing or merging them when the next character is added.
        present: list[str] = []
        for i, (sheet, name) in enumerate(cast, 1):
            keep = ""
            if present:
                keep = (f" The scene ALREADY contains {', '.join(present)} — keep each of them "
                        "EXACTLY as they appear (identical face, hair, body, wardrobe, colours and "
                        "position); do NOT redraw, restyle, replace, duplicate or merge them.")
            p = ("Edit the FIRST image (the scene). Add ONLY the character shown in the SECOND "
                 f"image EXACTLY as drawn — same species, face, colours and wardrobe — as {name}."
                 + keep +
                 f" Scene: {action}. Keep the existing set and art style unchanged. Do NOT add any "
                 "other creatures or people, and no text." + feedback)
            d, m = imagegen.compose_image(p, [running, sheet], model=ins_model)
            running = str(imagegen.save_bytes(d, out, f"kf_{shot_id}_{tag}s{i}", m))
            present.append(name)
        return running

    expects = (f"{action}. The frame MUST clearly include: "
               f"{', '.join(n for _, n in cast) or 'the set'}; no extra people/creatures, "
               "no malformed hands/faces, no gibberish text.")
    refs = _qc_refs(b, scene, cast)
    review = {"ok": True, "score": 1.0, "issues": ""}
    final_src = ""
    feedback = ""
    attempts = 0
    for attempt in range(QC_MAX_TRIES if qc else 1):      # render -> review -> corrective re-render
        attempts += 1
        final_src = render_once(f"a{attempt}_", feedback)
        if not qc or not cast:
            break
        review = imagegen.review_image(final_src, expects, refs=refs)
        if review["ok"]:
            break
        # feed the critic's issues back into the NEXT attempt instead of re-rolling blind
        feedback = (" FIX these problems from the previous attempt: " + review["issues"]
                    if review.get("issues") else "")

    final = out / f"kf_{shot_id}.png"
    final.write_bytes(Path(final_src).read_bytes())
    store.update_shot(user_id, project_id, shot_id,
                      {"keyframe_uri": str(final), "status": "keyframed",
                       "qc_ok": review["ok"], "qc_score": review.get("score"),
                       "qc_issues": review.get("issues", "")})
    if not cast and not cast_warnings:
        cast_warnings.append(
            "no characters resolved for this shot — set the shot's `subject` to a character "
            "name/id and/or add `char:<id>` anchors so people are composited in")
    return {"shot_id": shot_id, "keyframe_uri": str(final),
            "chars_inserted": len(cast), "cast": [n for _, n in cast],
            "qc_ok": review["ok"], "qc_score": review.get("score"),
            "qc_issues": review.get("issues", ""), "qc_attempts": attempts,
            "warnings": cast_warnings}


def mv_start_shot_video(user_id: str, project_id: str, shot_id: str,
                        model: str = "gemini-omni-flash-preview", duration_seconds: int = 6) -> dict:
    """Start an ASYNC video job for a shot (default Omni; storyboard->video from the keyframe).
    Falls back to Veo if Omni fails. Poll with mv_get_shot_video; state lives upstream + the
    job name in the bible."""
    b = store.get_project(user_id, project_id)
    shot = next(s for s in b["shots"] if s["shot_id"] == shot_id)
    if not shot.get("keyframe_uri"):
        raise ValueError(f"shot {shot_id} has no keyframe; run generate_shot first")
    prompt = (f"{b['style_guide']}. {shot.get('intent', '')}. "
              f"Subtle, natural motion consistent with the framing.")
    fallback = None
    try:
        r = videogen.start_video(prompt, model=model, image_path=shot["keyframe_uri"],
                                 duration_seconds=duration_seconds)
    except Exception as e:  # robust: default to Omni, fall back to verified Veo
        fallback = f"omni failed → veo: {str(e)[:120]}"
        r = videogen.start_video(prompt, model="veo-3.1-fast-generate-001",
                                 image_path=shot["keyframe_uri"], duration_seconds=duration_seconds)
    store.update_shot(user_id, project_id, shot_id,
                      {"status": "video_running", "video_job": r["job_name"],
                       "video_backend": r["backend"]})
    out = {"shot_id": shot_id, **r}
    if fallback:
        out["fallback"] = fallback
    return out


def mv_get_shot_video(user_id: str, project_id: str, shot_id: str) -> dict:
    """Poll a shot's async video job (by its upstream name — stateless). When done, saves the
    video and records its URI in the bible."""
    b = store.get_project(user_id, project_id)
    shot = next(s for s in b["shots"] if s["shot_id"] == shot_id)
    job = shot.get("video_job")
    if not job:
        return {"shot_id": shot_id, "status": "not_started"}
    save_path = _gen_dir(user_id, project_id) / f"vid_{shot_id}.mp4"
    r = videogen.poll_video(job, backend=shot.get("video_backend", "veo"), save_path=str(save_path))
    if r.get("status") == "done":
        store.update_shot(user_id, project_id, shot_id,
                          {"status": "video_done", "video_uri": r.get("video_uri")})
    elif r.get("status") == "error":
        reason = r.get("error", "") or "unknown upstream failure"
        store.update_shot(user_id, project_id, shot_id,
                          {"status": "error", "video_error": reason})
        _log.warning("shot %s video error: %s", shot_id, reason)
    return {"shot_id": shot_id, **r}


# ======================================================================================
# Micro-shot -> reference-to-video pipeline
#   1) ONE nano-banana call renders a 3-panel storyboard strip (composition + beats +
#      character consistency) from the scene's establish plate + character sheets.
#   2) Omni reference_to_video animates it into one continuous clip, re-anchored on the
#      clean character sheets + background plate so identity holds. (Proven end-to-end.)
# ======================================================================================
def _scene_cast(bible: dict, scene_id: str, subjects: list[str] | None = None,
                beats: list | None = None, limit: int = 3) -> list[tuple[str, str]]:
    """(ref_path, name) for the characters ACTUALLY in a scene — resolved in priority order from:
    explicit `subjects`, the beats' `speaker`s, the scene's PERSISTED cast, its blocking, then its
    shots. Only if NONE of those name anyone does it fall back to all project characters. This is
    scene-specific so a one-character scene never pulls in the whole cast (which made videos add
    people who aren't in the scene). Capped at `limit`; only characters with a sheet on disk."""
    sc = bible.get("scenes", {}).get(scene_id, {})
    cids: list[str] = []

    def _add(tok: str) -> None:
        cid = _match_char(bible, tok)
        if cid and cid not in cids:
            cids.append(cid)

    if subjects:                                     # explicit list = authoritative (who is PRESENT)
        for s in subjects:
            _add(s)
    else:
        # UNION of everyone present in the scene — NOT just who speaks. A character can be in the
        # frame without a line, so we pool: persisted cast + blocking + shot anchors + beat speakers.
        for tok in (sc.get("cast") or []):           # persisted when the micro-shot was built
            _add(tok)
        for tok in (sc.get("blocking") or {}):       # blocking lists everyone placed in the scene
            _add(tok)
        for s in bible.get("shots", []):
            if s.get("scene") == scene_id:
                for _ref, nm in _resolve_cast(bible, s)[0]:
                    _add(nm)
        for beat in (beats or []):                   # speakers, in case blocking/shots are absent
            _a, _e, _d, speaker = _beat_fields(beat)
            if speaker:
                _add(speaker)
        if not cids:                                 # last resort only (nothing named the cast)
            cids = list(bible.get("characters", {}))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cid in cids:
        if cid in seen:
            continue
        seen.add(cid)
        c = bible.get("characters", {}).get(cid, {})
        ref = (c.get("refs") or [None])[0]
        if ref and os.path.exists(ref):
            out.append((ref, c.get("name", cid)))
    return out[:limit]


def _screen_direction(cast: list[tuple[str, str]]) -> str:
    """A 180-degree-rule clause pinning each character to a fixed screen side, so the image model
    keeps a CONSISTENT left/right layout across panels (and the video keeps it across time) and
    only mirrors it for an explicit reverse angle from behind."""
    if len(cast) < 2:
        return ""
    slots = ["on the LEFT side of frame", "on the RIGHT side of frame",
             "in the CENTER of frame", "on the FAR LEFT of frame", "on the FAR RIGHT of frame"]
    layout = ", ".join(f"{name} stays {slot}" for (_, name), slot in zip(cast, slots))
    return (" SCREEN DIRECTION — obey the 180-degree line: keep a CONSISTENT left-to-right "
            f"layout in EVERY panel/moment — {layout}. When the camera faces the characters do "
            "NOT swap their left/right positions; mirror the layout ONLY for a shot explicitly "
            "taken from BEHIND them (a reverse angle).")


class Beat(BaseModel):
    """One storyboard beat / shot. `action` is what happens; `emotion` drives expression + vocal
    tone; `dialogue` becomes a spoken lip-synced line (video, when audio on); `speaker` is who."""
    action: str = ""
    emotion: str = ""
    dialogue: str = ""
    speaker: str = ""


def _beat_fields(beat) -> tuple[str, str, str, str]:
    """Normalise a beat -> (action, emotion, dialogue, speaker). A beat may be a Beat model, a
    plain string (action only), or a dict {action|shot|intent, emotion, dialogue|dialog,
    speaker|character}."""
    if hasattr(beat, "model_dump"):          # a Beat (or any pydantic model)
        beat = beat.model_dump()
    if isinstance(beat, dict):
        return (
            str(beat.get("action") or beat.get("shot") or beat.get("intent") or "").strip(),
            str(beat.get("emotion") or "").strip(),
            str(beat.get("dialogue") or beat.get("dialog") or "").strip(),
            str(beat.get("speaker") or beat.get("character") or "").strip(),
        )
    return (str(beat).strip(), "", "", "")


def _scene_beats(bible: dict, scene_id: str, beats: list | None, panels: int) -> list:
    """Beat text per panel: explicit `beats` if given, else the scene's shot intents, else a
    generic establishing/action/reaction arc — always exactly `panels` entries."""
    if beats:
        return beats[:panels]
    intents = [s.get("intent", "") for s in bible.get("shots", [])
               if s.get("scene") == scene_id and s.get("intent")]
    if len(intents) >= panels:
        return intents[:panels]
    generic = ["wide establishing shot of the scene",
               "medium shot, the main action of the scene",
               "closer shot, the emotional reaction / resolution"]
    return (intents + generic)[:panels]


def mv_generate_microshot(user_id: str, project_id: str, scene_id: str,
                          beats: list[str] | None = None, subjects: list[str] | None = None,
                          panels: int = 3, prompt: str = "") -> dict:
    """ONE image-model call -> a labelled N-panel micro-shot storyboard for a scene, conditioned
    on the scene's establish plate + character sheets (identity held across panels)."""
    b = store.get_project(user_id, project_id)
    scene = b.get("scenes", {}).get(scene_id, {})
    out = _gen_dir(user_id, project_id)
    beat_list = _scene_beats(b, scene_id, beats, panels)
    panels = len(beat_list)
    cast = _scene_cast(b, scene_id, subjects, beats=beat_list)   # beats' speakers drive the cast
    names = ", ".join(n for _, n in cast) or "the characters"

    refs: list[str] = []
    if scene.get("establish_uri") and os.path.exists(scene["establish_uri"]):
        refs.append(scene["establish_uri"])
    refs += [sheet for sheet, _ in cast]
    props = _prop_refs(b)                       # user-uploaded props → extra reference images
    refs += [r for r, _ in props]
    prop_note = (f" Include these props, matching their reference images: "
                 f"{', '.join(n for _, n in props)}." if props else "")

    def _panel_desc(i: int, beat) -> str:
        action, emotion, _dialogue, speaker = _beat_fields(beat)
        s = f"SHOT {i}: {action}."
        if emotion:  # emotion drives the facial expression / body language in the still
            who = speaker or "the character"
            s += f" {who}'s expression clearly reads {emotion}."
        return s

    beats_text = " ".join(_panel_desc(i, beat) for i, beat in enumerate(beat_list, 1))
    p = (prompt.strip() + " " if prompt.strip() else "") + (
        f"Create ONE single image that is a {panels}-PANEL STORYBOARD STRIP: {panels} equal "
        "panels side by side, left to right, separated by thin white borders, labelled "
        + ", ".join(f"SHOT {i}" for i in range(1, panels + 1)) + ". "
        f"Use the SAME characters ({names}) from the reference sheets and the SAME setting from "
        "the scene reference — identical faces, wardrobe, art style and lighting in EVERY panel. "
        + beats_text + _screen_direction(cast) + prop_note + " Convey each character's emotion "
        f"through facial expression and body language. {b.get('style_guide', '')}. Cinematic, "
        "consistent character identity across all panels; no gibberish text besides the SHOT labels.")

    expects = (f"a {panels}-panel left-to-right storyboard strip; EVERY panel shows {names} with "
               "consistent identity, wardrobe, art style and lighting matching the references; "
               "correct SHOT labels; no gibberish text, no malformed anatomy, no extra/missing "
               "characters.")
    qc_refs = _qc_refs(b, scene, cast)
    review = {"ok": True, "score": 1.0, "issues": ""}
    feedback = ""
    src = ""
    attempts = 0
    for attempt in range(QC_MAX_TRIES):                   # render -> review -> corrective re-render
        attempts += 1
        pf = p + feedback
        if refs:
            data, mime = imagegen.compose_image(pf, refs)
        else:
            data, mime = imagegen.generate_image(pf)
        src = str(imagegen.save_bytes(data, out, f"microshot_{scene_id}_a{attempt}", mime))
        review = imagegen.review_image(src, expects, refs=qc_refs)
        if review["ok"]:
            break
        feedback = (" FIX these problems from the previous attempt: " + review["issues"]
                    if review.get("issues") else "")

    path = out / f"microshot_{scene_id}.png"
    path.write_bytes(Path(src).read_bytes())
    # persist the resolved cast so the VIDEO step animates the SAME characters (not all of them)
    cast_ids = [cid for cid in (_match_char(b, nm) for _, nm in cast) if cid]
    store.update_scene(user_id, project_id, scene_id,
                       {"microshot_uri": str(path), "cast": cast_ids, "qc_ok": review["ok"],
                        "qc_score": review.get("score"), "qc_issues": review.get("issues", "")})
    return {"scene_id": scene_id, "microshot_uri": str(path), "panels": panels,
            "beats": beat_list, "cast": [n for _, n in cast], "qc_ok": review["ok"],
            "qc_score": review.get("score"), "qc_issues": review.get("issues", ""),
            "qc_attempts": attempts}


def mv_start_scene_video(user_id: str, project_id: str, scene_id: str,
                         beats: list[str] | None = None, duration_seconds: int = 10,
                         audio: bool = True, wait: bool = True) -> dict:
    """Animate a scene's micro-shot into ONE continuous clip via Omni reference_to_video. Inputs:
    the micro-shot storyboard + the scene's character sheets + the background plate. Run
    mv_generate_microshot first. Poll with mv_get_scene_video."""
    b = store.get_project(user_id, project_id)
    scene = b.get("scenes", {}).get(scene_id, {})
    micro = scene.get("microshot_uri")
    if not micro or not os.path.exists(micro):
        raise ValueError(f"scene {scene_id} has no micro-shot; run generate_microshot first")
    duration_seconds = min(int(duration_seconds), videogen.OMNI_MAX_DURATION)  # Omni caps at 10s
    beat_list = _scene_beats(b, scene_id, beats, 3)
    cast = _scene_cast(b, scene_id, beats=beat_list)   # scene's own cast (persisted at micro-shot)

    refs = [micro] + [sheet for sheet, _ in cast]
    if scene.get("establish_uri") and os.path.exists(scene["establish_uri"]):
        refs.append(scene["establish_uri"])

    names = ", ".join(n for _, n in cast) or "the characters"
    only = (f" Feature ONLY {names} — do NOT add any other people or characters who are not in "
            "the reference sheets." if cast else "")
    n = len(beat_list)
    win = max(1, round(duration_seconds / n))
    has_dialogue = False

    def _window_desc(i: int, beat) -> str:
        nonlocal has_dialogue
        action, emotion, dialogue, speaker = _beat_fields(beat)
        t0, t1 = i * win, min((i + 1) * win, duration_seconds)
        s = f"{t0}-{t1}s (SHOT {i+1}): {action}."
        if emotion:
            voice = " and voice" if audio else ""
            who = speaker or "the character"
            s += f" {who} feels {emotion} — show it in their face, body language{voice}."
        if dialogue and audio:  # spoken lines only when audio is requested
            has_dialogue = True
            who = speaker or "a character"
            s += f' {who} says, with a {emotion or "natural"} tone: "{dialogue}"'
        return s

    windows = " ".join(_window_desc(i, beat) for i, beat in enumerate(beat_list))
    if not audio:
        audio_line = " SILENT clip — no dialogue and no audio track; convey everything visually."
    elif has_dialogue:
        audio_line = (" Include natural SPOKEN DIALOGUE with lip movement synced to each line and "
                      "the matching emotional tone of voice, plus subtle ambient sound.")
    else:
        audio_line = " Include subtle ambient sound and expressive performances."
    prompt = (
        f"The FIRST reference image is a {n}-panel micro-shot storyboard (SHOT 1..{n}). Generate "
        f"ONE continuous {duration_seconds}-second video that plays those beats IN ORDER. Feature "
        f"{names} exactly as in their reference sheets, in the setting from the background "
        f"reference.{only} {windows}{_screen_direction(cast)}{audio_line} {b.get('style_guide', '')}. "
        "Consistent character identity, emotive acting, gentle cinematic motion, 16:9.")

    try:
        r = videogen.start_reference_video(prompt, refs, duration_seconds=duration_seconds)
    except Exception as e:  # capture WHY the job couldn't even start, don't lose it
        reason = str(e)[:300]
        _log.warning("scene %s video failed to start: %s", scene_id, reason)
        store.update_scene(user_id, project_id, scene_id,
                           {"status": "error", "video_error": reason})
        raise ValueError(f"could not start scene video: {reason}")
    # persist the prompt+refs+duration so a transient upstream failure can be auto-retried at
    # poll time (Omni occasionally fails a job; the same inputs succeed on a fresh attempt).
    store.update_scene(user_id, project_id, scene_id,
                       {"status": "scene_video_running", "video_job": r["job_name"],
                        "video_backend": r["backend"], "audio": bool(audio),
                        "video_prompt": prompt, "video_refs": refs,
                        "video_duration": duration_seconds, "video_retries": 0,
                        "video_error": None})
    result = {"scene_id": scene_id, "audio": bool(audio), **r}
    if not wait:
        return result  # async: caller must poll get_scene_video
    # BLOCK until the clip is saved (handles transient failures via get_scene_video's auto-retry)
    # so the agent gets the video_uri in ONE call instead of firing-and-forgetting.
    deadline = 150  # stay under the client's 180s MCP timeout
    waited = 0
    while waited < deadline:
        time.sleep(6)
        waited += 6
        p = mv_get_scene_video(user_id, project_id, scene_id)
        if p.get("status") in ("scene_video_done", "done", "error"):
            return {**result, **p}
    return {**result, "status": "running",
            "note": "still rendering after 150s — poll get_scene_video"}


SCENE_VIDEO_MAX_RETRIES = 1


def mv_get_scene_video(user_id: str, project_id: str, scene_id: str) -> dict:
    """Poll a scene's reference-to-video job (stateless, by upstream name). Saves the mp4 and
    records its URI when done. On a transient upstream FAILURE, auto-restarts the job (same
    inputs) up to SCENE_VIDEO_MAX_RETRIES times."""
    b = store.get_project(user_id, project_id)
    scene = b.get("scenes", {}).get(scene_id, {})
    job = scene.get("video_job")
    if not job:
        return {"scene_id": scene_id, "status": "not_started"}
    save_path = _gen_dir(user_id, project_id) / f"vid_scene_{scene_id}.mp4"
    r = videogen.poll_video(job, backend=scene.get("video_backend", "omni"),
                            save_path=str(save_path))
    if r.get("status") == "done":
        store.update_scene(user_id, project_id, scene_id,
                           {"status": "scene_video_done", "video_uri": r.get("video_uri")})
        return {"scene_id": scene_id, **r}
    if r.get("status") == "error":
        reason = r.get("error", "") or "unknown upstream failure"
        retries = scene.get("video_retries", 0)
        prompt, refs = scene.get("video_prompt"), scene.get("video_refs")
        # record the reason on the scene either way, so it's inspectable in the bible / UI
        store.update_scene(user_id, project_id, scene_id, {"video_error": reason})
        _log.warning("scene %s video error (retry %d): %s", scene_id, retries, reason)
        if retries < SCENE_VIDEO_MAX_RETRIES and prompt and refs:
            nr = videogen.start_reference_video(
                prompt, refs, duration_seconds=scene.get("video_duration", 10))
            store.update_scene(user_id, project_id, scene_id,
                               {"video_job": nr["job_name"], "video_retries": retries + 1,
                                "status": "scene_video_running"})
            return {"scene_id": scene_id, "status": "running", "retried": retries + 1,
                    "error": reason, "note": f"transient failure — retrying: {reason}"}
        return {"scene_id": scene_id, "status": "error", "error": reason,
                "note": f"failed after {retries} retries: {reason}"}
    return {"scene_id": scene_id, **r}


def mv_generate_music(user_id: str, project_id: str, prompt: str = "", mood: str = "") -> dict:
    """Generate an INSTRUMENTAL score (Lyria 3) for the project, themed to its style/mood. The
    agent should pass a rich `prompt` (instruments, tempo, mood); the server enforces
    instrumental-only and ties it to the project. Saved once per project as the score."""
    b = store.get_project(user_id, project_id)
    out = _gen_dir(user_id, project_id)
    brief = prompt.strip() or mood.strip() or "cinematic underscore matching the story mood"
    full = (f"{brief}. Instrumental only — NO vocals, NO lyrics, NO spoken words. "
            f"A cohesive score for: {b.get('title', 'the film')}. {b.get('style_guide', '')}.")
    data, mime = musicgen.generate_music(full)
    path = musicgen.save_music(data, out, "music", mime)
    store.set_music(user_id, project_id, str(path))
    return {"project_id": project_id, "music_uri": str(path),
            "mime": mime, "size_bytes": len(data)}


# ======================================================================================
# MCP server (thin wrappers exposing the pipeline as tools)
# ======================================================================================
# Behind Cloud Run the Host header is the *.run.app domain, so FastMCP's default localhost-only
# DNS-rebinding allowlist would 421 every MCP request (that's what makes clients see an EMPTY
# toolset). Cloud Run + IAM is the security boundary here, so disable that specific check.
# stateless_http + json_response: no per-connection session id to lose across Cloud Run cold
# starts/instances (avoids "Session terminated" toolset drops); each request is self-contained.
mcp = FastMCP("movie-mcp", stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


@mcp.tool()
def create_project(user_id: str, title: str, style_guide: str = "") -> dict:
    """Create a new movie project (owned by user_id). Returns project_id."""
    return mv_create_project(user_id, title, style_guide)


@mcp.tool()
def list_projects(user_id: str) -> list[dict]:
    """List a user's movie projects (only their own)."""
    return mv_list_projects(user_id)


@mcp.tool()
def get_project(user_id: str, project_id: str) -> dict:
    """Read the full story bible for one of the user's projects."""
    return mv_get_project(user_id, project_id)


@mcp.tool()
async def generate_style_ref(user_id: str, project_id: str, ctx: Context,
                             prompt: str = "", description: str = "") -> dict:
    """Generate the project's global STYLE reference image. Pass a rich `prompt` (in the chosen
    visual style) for best quality."""
    await ctx.info("Generating style reference…")
    r = mv_generate_style_ref(user_id, project_id, description, prompt)
    r["resource_uri"] = _asset_uri(user_id, project_id, r["style_ref"])
    return r


@mcp.tool()
async def add_character(user_id: str, project_id: str, name: str, description: str, ctx: Context,
                        prompt: str = "") -> dict:
    """Lock a character reference sheet. YOU (the agent) should pass a rich `prompt` — a full,
    vivid image prompt in the project's visual style (medium, wardrobe, lighting, detail)."""
    await ctx.info(f"Generating character sheet for {name}…")
    r = mv_add_character(user_id, project_id, name, description, prompt)
    r["resource_uri"] = _asset_uri(user_id, project_id, r["ref_uri"])
    return r


@mcp.tool()
async def establish_scene(user_id: str, project_id: str, scene_id: str, description: str,
                          ctx: Context, lighting: str = "", blocking: dict | None = None,
                          prompt: str = "") -> dict:
    """Generate a scene's people-free set plate. Pass a rich `prompt` (the set, no characters)
    for best quality; record blocking (screen sides / eyelines)."""
    await ctx.info(f"Establishing scene {scene_id}…")
    r = mv_establish_scene(user_id, project_id, scene_id, description, lighting, blocking, prompt)
    r["resource_uri"] = _asset_uri(user_id, project_id, r["establish_uri"])
    return r


@mcp.tool()
def plan_scene(user_id: str, project_id: str, scene_id: str, shots: list[dict]) -> dict:
    """Validate a structured shot plan against film grammar; persist it only if error-free.
    Returns violations (180°, eyeline, 30°, establish-first, lens, anchors)."""
    return mv_plan_scene(user_id, project_id, scene_id, shots)


@mcp.tool()
async def generate_shot(user_id: str, project_id: str, shot_id: str, ctx: Context,
                        prompt: str = "") -> dict:
    """Compose the shot's keyframe (iterative per-character insertion) and run a vision QC pass
    that regenerates if a character is missing / hands mangled / extras appear. Pass a rich
    `prompt` (the vivid scene description) for best quality. Returns qc_ok / qc_issues."""
    await ctx.report_progress(0.2, 1.0, "composing keyframe")
    r = mv_generate_shot(user_id, project_id, shot_id, prompt)
    r["resource_uri"] = _asset_uri(user_id, project_id, r["keyframe_uri"])
    await ctx.report_progress(1.0, 1.0, "saved")
    return r


@mcp.tool()
def start_shot_video(user_id: str, project_id: str, shot_id: str,
                     model: str = "gemini-omni-flash-preview", duration_seconds: int = 6) -> dict:
    """Start the async video job for a shot's keyframe (default Omni, storyboard->video; falls
    back to Veo). Returns a job handle to poll with get_shot_video."""
    return mv_start_shot_video(user_id, project_id, shot_id, model, duration_seconds)


@mcp.tool()
def get_shot_video(user_id: str, project_id: str, shot_id: str) -> dict:
    """Poll a shot's async video job; when done, returns and records the video URI."""
    return mv_get_shot_video(user_id, project_id, shot_id)


@mcp.tool()
async def generate_microshot(user_id: str, project_id: str, scene_id: str, ctx: Context,
                             beats: list[Beat] | None = None, subjects: list[str] | None = None,
                             panels: int = 3, prompt: str = "") -> dict:
    """ONE image-model call -> a labelled N-panel micro-shot storyboard for a scene (composition
    + beats + character consistency in a single image), conditioned on the scene's establish
    plate + character sheets. Each `beats` item is either a short action string OR a dict
    {action, emotion, dialogue, speaker} — `emotion` drives the facial expression in the still
    (dialogue is used later by start_scene_video). Optionally pass `subjects` (character
    ids/names). Feed the result to start_scene_video. Returns a movie:// resource_uri."""
    await ctx.report_progress(0.3, 1.0, "rendering micro-shot storyboard")
    r = mv_generate_microshot(user_id, project_id, scene_id, beats, subjects, panels, prompt)
    r["resource_uri"] = _asset_uri(user_id, project_id, r["microshot_uri"])
    await ctx.report_progress(1.0, 1.0, "saved")
    return r


@mcp.tool()
def start_scene_video(user_id: str, project_id: str, scene_id: str,
                      beats: list[Beat] | None = None, duration_seconds: int = 10,
                      audio: bool = True, wait: bool = True) -> dict:
    """Animate a scene's micro-shot into ONE continuous clip (Omni reference_to_video) using the
    micro-shot + the scene's character sheets + the background plate. Run generate_microshot
    first. Each `beats` item may be a string OR a dict {action, emotion, dialogue, speaker}:
    `emotion` shapes each character's performance/tone. If `audio` is true, `dialogue` becomes
    SPOKEN, lip-synced lines (in the emotional tone) plus ambient sound; if `audio` is false the
    clip is SILENT. ASK the user whether they want audio before calling. Max duration 10s.
    By default (wait=true) this BLOCKS until the clip is rendered and returns `video_uri`
    directly (auto-retrying transient failures) — no separate polling needed. Pass wait=false to
    fire-and-forget and poll get_scene_video yourself."""
    return mv_start_scene_video(user_id, project_id, scene_id, beats, duration_seconds, audio, wait)


@mcp.tool()
def get_scene_video(user_id: str, project_id: str, scene_id: str) -> dict:
    """Poll a scene's reference-to-video job; when done, returns and records the video URI."""
    return mv_get_scene_video(user_id, project_id, scene_id)


@mcp.tool()
async def generate_music(user_id: str, project_id: str, ctx: Context,
                         prompt: str = "", mood: str = "") -> dict:
    """Generate an INSTRUMENTAL score (Lyria 3) themed to the project. YOU (the agent) write a
    rich `prompt` — instruments, tempo, mood, genre (e.g. 'slow melancholic solo piano with soft
    strings, sparse, cinematic'). The server forces instrumental-only (no vocals/lyrics) and ties
    it to the project's title + style. Returns a movie:// resource_uri for the audio track."""
    await ctx.report_progress(0.3, 1.0, "composing score")
    r = mv_generate_music(user_id, project_id, prompt, mood)
    r["resource_uri"] = _asset_uri(user_id, project_id, r["music_uri"])
    await ctx.report_progress(1.0, 1.0, "saved")
    return r


@mcp.tool()
def review_asset(user_id: str, project_id: str, name: str, expects: str = "") -> dict:
    """Run the vision film-editor critic on a generated image (by movie:// URI or filename) and
    return {ok, score, issues, dims}. It compares the asset against the project's canonical
    references (style ref + character sheets) to judge identity/style consistency, and returns a
    short ACTIONABLE `issues` string. Use it to check a frame's quality/continuity on demand and
    to decide whether to regenerate. `expects` = a one-line description of what the frame should show."""
    fname = _safe_name(name.split("/")[-1])
    path = _gen_dir(user_id, project_id) / fname
    if not path.exists():
        raise ValueError(f"no such asset: {name}")
    b = store.get_project(user_id, project_id)
    refs = []
    if b.get("style_ref") and os.path.exists(b["style_ref"]):
        refs.append(b["style_ref"])
    for c in b.get("characters", {}).values():
        ref = (c.get("refs") or [None])[0]
        if ref and os.path.exists(ref):
            refs.append(ref)
    r = imagegen.review_image(str(path), expects or "the intended scene and characters", refs=refs[:5])
    return {"name": fname, "resource_uri": _asset_uri(user_id, project_id, str(path)), **r}


def mv_import_character(user_id: str, project_id: str, name: str, description: str = "",
                       image_name: str = "") -> dict:
    """Register an ALREADY-UPLOADED image (saved in the project's media dir, e.g. by the Studio
    upload endpoint) as a character reference — NO generation. The character then composites into
    shots exactly like a generated one (mv_generate_shot / generate_microshot read refs[0])."""
    src = _gen_dir(user_id, project_id) / _safe_name(image_name.split("/")[-1])
    if not src.exists():
        raise ValueError(f"uploaded image not found for import: {image_name}")
    b = store.get_project(user_id, project_id)
    low = name.strip().lower()
    existing = next((cid for cid, c in b.get("characters", {}).items()
                     if (c.get("name") or "").strip().lower() == low), None)
    if existing:   # REPLACE the existing same-name character's sheet with the upload (no duplicate)
        store.update_character(user_id, project_id, existing,
                               {"refs": [str(src)],
                                "desc": description or b["characters"][existing].get("desc", "")})
        return {"char_id": existing, "name": name, "ref_uri": str(src), "uploaded": True,
                "replaced": True, "resource_uri": _asset_uri(user_id, project_id, str(src)),
                "note": "replaced existing character's reference — re-run generate_microshot for their scenes"}
    cid = store.add_character(user_id, project_id, name,
                             description or f"user-uploaded reference for {name}", refs=[str(src)])
    return {"char_id": cid, "name": name, "ref_uri": str(src), "uploaded": True, "replaced": False,
            "resource_uri": _asset_uri(user_id, project_id, str(src))}


@mcp.tool()
def import_character(user_id: str, project_id: str, name: str, description: str = "",
                    image_name: str = "") -> dict:
    """Register a USER-UPLOADED image as a character reference sheet (no AI generation). `image_name`
    is a file already saved in the project's media dir (via the Studio upload). The character is then
    reused across shots like any other — reference it by the returned char_id; do NOT regenerate it."""
    return mv_import_character(user_id, project_id, name, description, image_name)


@mcp.tool()
async def update_character(user_id: str, project_id: str, character: str, change: str,
                          ctx: Context, prompt: str = "") -> dict:
    """Change a character's WARDROBE/APPEARANCE (e.g. 'dress to dark blue', 'add glasses'). This
    re-styles their REFERENCE SHEET while keeping identity — the ONLY reliable way to change an
    outfit, because scene compositing is locked to the sheet (a per-scene prompt won't change it).
    `character` = char_id or name. AFTER this, RE-RUN generate_microshot for scenes with them."""
    await ctx.info(f"Restyling {character}: {change}…")
    return mv_update_character(user_id, project_id, character, change, prompt)


def mv_update_character(user_id: str, project_id: str, character: str, change: str,
                        prompt: str = "") -> dict:
    """Re-style a character's REFERENCE SHEET to change wardrobe/appearance while KEEPING identity,
    then point the character at the new sheet. Every later shot composites from refs[0], so this is
    how you actually change an outfit/hair/colour — a per-scene prompt can't (the compositing is
    locked to the sheet). `character` may be a char_id or name; `change` e.g. 'dress to dark blue'."""
    b = store.get_project(user_id, project_id)
    cid = _match_char(b, character)
    if not cid:
        raise ValueError(f"character not found: {character!r}")
    c = b["characters"][cid]
    ref = (c.get("refs") or [None])[0]
    if not ref or not os.path.exists(ref):
        raise ValueError(f"character {c.get('name', cid)!r} has no reference sheet to edit")
    name = c.get("name", cid)
    edit = prompt.strip() or (
        f"Edit this character reference sheet: keep the SAME character — identical face, hairstyle, "
        f"body type, age, skin tone and art style — but change {change}. Keep the front and 3/4 "
        f"views on a neutral background. Change ONLY {change}; leave everything else identical.")
    out = _gen_dir(user_id, project_id)
    data, mime = imagegen.compose_image(edit, [ref])
    newpath = imagegen.save_bytes(data, out, f"char_{name.lower().replace(' ', '_')}_upd", mime)
    desc = (c.get("desc", "") + f"; {change}").strip("; ")
    store.update_character(user_id, project_id, cid, {"refs": [str(newpath)], "desc": desc})
    return {"char_id": cid, "name": name, "change": change, "ref_uri": str(newpath),
            "resource_uri": _asset_uri(user_id, project_id, str(newpath)),
            "note": "reference sheet restyled — re-run generate_microshot for scenes with this character"}


def mv_import_prop(user_id: str, project_id: str, name: str, description: str = "",
                   image_name: str = "") -> dict:
    """Register a USER-UPLOADED prop image (no generation). Props are added as extra reference
    images to scene micro-shots so the model includes them; they don't need a char slot."""
    src = _gen_dir(user_id, project_id) / _safe_name(image_name.split("/")[-1])
    if not src.exists():
        raise ValueError(f"uploaded image not found for import: {image_name}")
    b = store.get_project(user_id, project_id)
    low = name.strip().lower()
    existing = next((pid for pid, p in b.get("props", {}).items()
                     if (p.get("name") or "").strip().lower() == low), None)
    if existing:   # REPLACE the existing same-name prop's reference (no duplicate)
        store.update_prop(user_id, project_id, existing, {"refs": [str(src)]})
        return {"prop_id": existing, "name": name, "ref_uri": str(src), "uploaded": True,
                "replaced": True, "resource_uri": _asset_uri(user_id, project_id, str(src))}
    pid = store.add_prop(user_id, project_id, name,
                         description or f"user-uploaded prop: {name}", refs=[str(src)])
    return {"prop_id": pid, "name": name, "ref_uri": str(src), "uploaded": True, "replaced": False,
            "resource_uri": _asset_uri(user_id, project_id, str(src))}


@mcp.tool()
def import_prop(user_id: str, project_id: str, name: str, description: str = "",
               image_name: str = "") -> dict:
    """Register a USER-UPLOADED prop image (no AI generation). The prop is fed as a reference into
    scene micro-shots so the model places it; reference it by its returned prop_id."""
    return mv_import_prop(user_id, project_id, name, description, image_name)


def _prop_refs(bible: dict) -> list[tuple[str, str]]:
    """(ref_path, name) for every uploaded prop with an on-disk reference image."""
    out: list[tuple[str, str]] = []
    for p in bible.get("props", {}).values():
        ref = (p.get("refs") or [None])[0]
        if ref and os.path.exists(ref):
            out.append((ref, p.get("name", "prop")))
    return out


@mcp.tool()
def list_project_assets(user_id: str, project_id: str) -> list[ResourceLink]:
    """List a project's generated images as MCP resource_link blocks the client can read back
    (via resources/read on each movie:// URI) — enumerate + fetch without leaking file paths."""
    d = _gen_dir(user_id, project_id)
    mimes = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav"}
    return [
        ResourceLink(
            type="resource_link",
            uri=f"movie://{user_id}/{project_id}/{p.name}",
            name=p.name,
            mimeType=mimes.get(p.suffix.lower(), "application/octet-stream"),
        )
        for p in sorted(d.iterdir())
        if p.is_file() and p.suffix.lower() in mimes
    ]


# --------------------------------------------------------------------------------------
# HELP — how to use the system (for when the user is stuck)
# --------------------------------------------------------------------------------------
# The canonical help content lives HERE (not just in the agent prompt) so every client —
# the ADK web chat, run.py, or a raw MCP client — gets the same guidance from one place.
HELP_COMMANDS = [
    {"command": "/help [topic]",
     "does": "Show help. Topics: modes, style, characters, scenes, video, music, errors, commands."},
    {"command": "/commands", "does": "List everything you can type."},
    {"command": "/status",
     "does": "Show your current project — style, cast, scenes, and what has been rendered so far."},
    {"command": "/modes", "does": "Explain the two build modes (AUTO vs INTERACTIVE)."},
    {"command": "/redo [what to change]",
     "does": "Regenerate the most recent image / scene / video (optionally say what to change)."},
    {"command": "/restart", "does": "Start over with a fresh idea / new project."},
]

HELP_TOPICS = {
    "overview": (
        "I'm an AI film director. You give me an idea; I build a movie from it — a visual style, "
        "a cast of character sheets, scenes, storyboard 'micro-shots', and (on request) short "
        "video clips and music. You can talk to me in plain language OR use slash-commands like "
        "/help. If you're ever unsure what to do next, type /status or /help."),
    "modes": (
        "Two ways to build:\n"
        "• AUTO — I make every creative decision and build straight through to finished frames for "
        "every scene, no stops. Fastest; least control.\n"
        "• INTERACTIVE — I stop for your approval at each stage (cast → scenes → art), so you can "
        "adjust anything before I move on. I always ask which mode you want first."),
    "style": (
        "The VISUAL STYLE (e.g. photorealistic, 2D cartoon, storybook illustration, 3D animation, "
        "anime, watercolour) is locked once as a global style reference, and every scene matches "
        "it. Tell me the style up front; say /redo on the style image to change the whole look."),
    "characters": (
        "Each character gets a canonical reference sheet that keeps them consistent across every "
        "shot. Use original, clearly-adult, generic descriptions — no copyrighted/IP names (they "
        "get blocked by the image filter); animals and robots are fine. Ask me to redo any sheet."),
    "scenes": (
        "Per scene I make a people-free 'establishing' set plate, then a 3-frame micro-shot "
        "storyboard (the default deliverable). Approve or /redo each before the next scene."),
    "video": (
        "On request I animate a scene's micro-shot into one short clip — capped at 10 SECONDS. The "
        "scene's FRAMES (micro-shot panels/beats) divide those 10s (~3s each; 3 frames ≈ 10s), so I "
        "recommend a frame count per scene and you can agree or edit it — more action means more "
        "scenes, not a longer clip. Before rendering I also ask: audio (spoken dialogue) or silent, "
        "and to approve the beats. Rendering takes up to ~2.5 min; I'll share a link when it's done."),
    "music": (
        "I can generate a standalone INSTRUMENTAL score themed to your project (it isn't mixed "
        "into the video). Ask for the mood/instruments/tempo you want."),
    "errors": (
        "If a render fails I tell you WHY (e.g. a safety/content filter, a quota limit, or a bad "
        "input) — I don't hide it. Most failures are fixed by rewording a description, choosing an "
        "original name, or shortening a clip. Say /redo to try again, optionally with a change."),
}


@mcp.tool()
def get_help(topic: str = "") -> dict:
    """Usage help for the movie director. Call this whenever the user types /help (or another
    slash-command) or seems stuck, and present the result conversationally. `topic` is one of:
    overview, commands, modes, style, characters, scenes, video, music, errors. Empty = overview
    plus the full command list."""
    t = (topic or "").strip().lstrip("/").lower()
    if t in ("", "help", "start", "overview"):
        return {"overview": HELP_TOPICS["overview"], "commands": HELP_COMMANDS,
                "topics": list(HELP_TOPICS)}
    if t in ("commands", "command"):
        return {"commands": HELP_COMMANDS}
    if t in HELP_TOPICS:
        return {"topic": t, "help": HELP_TOPICS[t], "commands": HELP_COMMANDS}
    return {"unknown_topic": t, "available_topics": list(HELP_TOPICS),
            "overview": HELP_TOPICS["overview"], "commands": HELP_COMMANDS}


# --------------------------------------------------------------------------------------
# RESOURCE — read a generated image back on demand (links, not bytes)
# --------------------------------------------------------------------------------------
@mcp.resource("movie://{user_id}/{project_id}/{name}", mime_type="image/png")
def read_asset(user_id: str, project_id: str, name: str) -> bytes:
    """Templated resource: return the bytes of a generated image by (user, project, filename).

    This is what lets a client (local OR remote) actually display/save a keyframe: the tool
    result carried only the movie:// URI, and the bytes travel here only when asked for.
    """
    path = GEN_ROOT / _safe_component(user_id) / _safe_component(project_id) / _safe_name(name)
    if not path.exists():
        raise ValueError(f"No such asset: {name}")
    return path.read_bytes()


def main() -> None:
    # Emit our warnings (video failures, retries) to stderr / the server console so a failed
    # render is never silent. Set LOG_LEVEL=DEBUG for more.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="movie-mcp server")
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 9100)))
    args = ap.parse_args()
    if args.http or "PORT" in os.environ:
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
