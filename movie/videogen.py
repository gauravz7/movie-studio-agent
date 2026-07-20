"""Async video generation — Omni (default) and Veo, both verified on this project.

Async-job pattern (stateless server, state upstream):
  start_video(...) -> {job_name, backend, status:"running"}
  poll_video(name) -> re-query by name; when done returns the saved mp4 URI.

Verified APIs:
  * Omni: genai.Client(location="global").interactions.create(model="gemini-omni-flash-preview",
    input=[{"type":"text","text":...}, {"type":"image","mime_type":...,"data":<b64>}],
    generation_config=interactions.GenerationConfig(
        video_config=interactions.VideoConfig(task="image_to_video")),   # <- routes to video
    response_format=interactions.VideoResponseFormat(aspect_ratio, duration="Ns"),
    background=True)
    → poll interactions.get(id); when status=="completed" the mp4 is base64 in
    interaction.steps[].content[] where mime_type startswith "video".  Region: GLOBAL.
    Storyboard->video: include the image content + task="image_to_video" (verified).
  * Veo: genai.Client(location="us-central1").models.generate_videos(...) long-running op,
    polled via operations.get(GenerateVideosOperation(name=...)).  Region: US-CENTRAL1.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from google import genai
from google.genai import interactions as I
from google.genai import types

_log = logging.getLogger("movie.videogen")

VPROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
VEO_LOCATION = os.environ.get("VEO_LOCATION", "us-central1")
OMNI_LOCATION = os.environ.get("OMNI_LOCATION", "global")
OMNI_MODEL = os.environ.get("OMNI_MODEL", "gemini-omni-flash-preview")
VEO_MODEL = os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-001")
OMNI_MAX_DURATION = 10  # Omni rejects >10s (verified: 10s ok, 12s errors)


def _omni_duration(duration_seconds: int) -> int:
    return max(1, min(int(duration_seconds), OMNI_MAX_DURATION))

_clients: dict[str, genai.Client] = {}


def _client(location: str) -> genai.Client:
    if location not in _clients:
        _clients[location] = genai.Client(vertexai=True, project=VPROJECT, location=location)
    return _clients[location]


# --------------------------------------------------------------------------- Veo (us-central1)
def _veo_start(prompt: str, model: str, image_path: str | None,
               aspect_ratio: str, duration_seconds: int) -> dict:
    cfg = types.GenerateVideosConfig(
        aspect_ratio=aspect_ratio, number_of_videos=1,
        duration_seconds=duration_seconds, resolution="720p", generate_audio=True)
    kwargs: dict = {"model": model, "prompt": prompt, "config": cfg}
    if image_path:
        kwargs["image"] = types.Image.from_file(location=image_path)
    op = _client(VEO_LOCATION).models.generate_videos(**kwargs)
    return {"job_name": op.name, "backend": "veo", "status": "running"}


def _veo_poll(job_name: str, save_path: str | None) -> dict:
    op = _client(VEO_LOCATION).operations.get(types.GenerateVideosOperation(name=job_name))
    if not op.done:
        return {"job_name": job_name, "backend": "veo", "status": "running"}
    if getattr(op, "error", None):  # long-running op failed upstream
        err = op.error
        reason = str(getattr(err, "message", None) or err)[:300]
        _log.warning("veo job %s failed: %s", job_name, reason)
        return {"job_name": job_name, "backend": "veo", "status": "error", "error": reason}
    vids = (getattr(op.result, "generated_videos", None) or []) if op.result else []
    if not vids:
        reason = "veo returned no video (likely a safety/content filter)"
        _log.warning("veo job %s: %s", job_name, reason)
        return {"job_name": job_name, "backend": "veo", "status": "error", "error": reason}
    v = vids[0].video
    if getattr(v, "uri", None):
        return {"job_name": job_name, "backend": "veo", "status": "done", "video_uri": v.uri}
    data = v.video_bytes
    p = Path(save_path or f"veo_{job_name.split('/')[-1]}.mp4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {"job_name": job_name, "backend": "veo", "status": "done",
            "video_uri": str(p), "size_bytes": len(data)}


# --------------------------------------------------------------------------- Omni (global)
def _img_part(path: str) -> dict:
    """Base64 image content item for an interactions input list."""
    mime = "image/png" if path.lower().endswith("png") else "image/jpeg"
    return {"type": "image", "mime_type": mime,
            "data": base64.b64encode(Path(path).read_bytes()).decode()}


def _omni_start(prompt: str, image_path: str | None,
                aspect_ratio: str, duration_seconds: int) -> dict:
    # Omni interactions do STORYBOARD->VIDEO (image_to_video): pass a multimodal input LIST
    # ([text, image]) PLUS generation_config.video_config.task="image_to_video". The task field
    # is what routes it to model (not agent) video generation — omitting it is what made a bare
    # list 400 before. aspect_ratio/duration still go via VideoResponseFormat. Ref: GCP
    # generative-ai/vision/getting-started/gemini_omni_flash_video_gen.ipynb.
    duration_seconds = _omni_duration(duration_seconds)
    inp: list = [{"type": "text", "text": prompt}]
    task = "text_to_video"
    if image_path and Path(image_path).exists():
        b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        mime = "image/png" if image_path.lower().endswith("png") else "image/jpeg"
        inp.append({"type": "image", "mime_type": mime, "data": b64})
        task = "image_to_video"
    it = _client(OMNI_LOCATION).interactions.create(
        model=OMNI_MODEL, input=inp, background=True,
        generation_config=I.GenerationConfig(video_config=I.VideoConfig(task=task)),
        response_format=I.VideoResponseFormat(aspect_ratio=aspect_ratio, duration=f"{duration_seconds}s"))
    return {"job_name": it.id, "backend": "omni", "status": "running", "task": task}


def _omni_start_refs(prompt: str, ref_image_paths: list[str],
                     aspect_ratio: str, duration_seconds: int) -> dict:
    """REFERENCE->VIDEO: the images are creative GUIDES (subjects/style/mood), not a literal
    first frame. Pass several ([micro-shot storyboard, character sheets, background plate]) plus
    task="reference_to_video". Verified: GCP gemini_omni_flash_video_gen.ipynb."""
    duration_seconds = _omni_duration(duration_seconds)
    parts = [_img_part(p) for p in ref_image_paths if p and Path(p).exists()]
    it = _client(OMNI_LOCATION).interactions.create(
        model=OMNI_MODEL, input=[{"type": "text", "text": prompt}, *parts], background=True,
        generation_config=I.GenerationConfig(video_config=I.VideoConfig(task="reference_to_video")),
        response_format=I.VideoResponseFormat(aspect_ratio=aspect_ratio, duration=f"{duration_seconds}s"))
    return {"job_name": it.id, "backend": "omni", "status": "running",
            "task": "reference_to_video", "refs": len(parts)}


def _omni_reason(it) -> str:
    """Best-effort extraction of WHY an Omni interaction failed (the SDK puts it in a few
    different places depending on the failure type). Returns '' if nothing usable is found."""
    for attr in ("error", "failure_reason", "status_message", "message"):
        v = getattr(it, attr, None)
        if v:
            return str(getattr(v, "message", None) or getattr(v, "reason", None) or v)[:300]
    # a content/safety block often comes back as a text part instead of a video part
    for step in (getattr(it, "steps", None) or []):
        for c in (getattr(step, "content", None) or []):
            t = getattr(c, "text", None)
            if t:
                return str(t)[:300]
    return ""


def _omni_poll(job_name: str, save_path: str | None) -> dict:
    it = _client(OMNI_LOCATION).interactions.get(id=job_name)
    st = str(getattr(it, "status", "")).lower()
    if st in ("failed", "error"):
        reason = _omni_reason(it) or "upstream reported failure (no reason given)"
        _log.warning("omni job %s failed: %s", job_name, reason)
        return {"job_name": job_name, "backend": "omni", "status": "error", "error": reason}
    if st not in ("completed", "succeeded", "done"):
        return {"job_name": job_name, "backend": "omni", "status": "running"}
    for step in (getattr(it, "steps", None) or []):
        for c in (getattr(step, "content", None) or []):
            mt = getattr(c, "mime_type", "") or ""
            raw = getattr(c, "data", None)
            if mt.startswith("video") and raw:
                data = raw if isinstance(raw, (bytes, bytearray)) else base64.b64decode(raw)
                p = Path(save_path or f"omni_{job_name}.mp4")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                return {"job_name": job_name, "backend": "omni", "status": "done",
                        "video_uri": str(p), "size_bytes": len(data)}
    # completed but no video part came back — almost always a safety/content filter
    reason = _omni_reason(it) or "completed but returned no video (likely a safety/content filter)"
    _log.warning("omni job %s: %s", job_name, reason)
    return {"job_name": job_name, "backend": "omni", "status": "error", "error": reason}


# --------------------------------------------------------------------------- dispatch
def start_video(prompt: str, model: str = OMNI_MODEL, image_path: str | None = None,
                aspect_ratio: str = "16:9", duration_seconds: int = 6) -> dict:
    """Start an async video job (Omni by default; Veo if model starts with 'veo')."""
    if model.startswith("veo"):
        return _veo_start(prompt, model, image_path, aspect_ratio, duration_seconds)
    return _omni_start(prompt, image_path, aspect_ratio, duration_seconds)


def start_reference_video(prompt: str, ref_image_paths: list[str],
                          aspect_ratio: str = "16:9", duration_seconds: int = 10) -> dict:
    """Start a REFERENCE->VIDEO job (Omni, global): the reference images guide subjects/style
    (e.g. a 3-panel micro-shot + character sheets + background). Poll with poll_video(backend
    ='omni'). Omni-only — Veo has no reference-to-video mode."""
    return _omni_start_refs(prompt, ref_image_paths, aspect_ratio, duration_seconds)


def poll_video(job_name: str, backend: str = "omni", save_path: str | None = None) -> dict:
    if backend == "veo":
        return _veo_poll(job_name, save_path)
    return _omni_poll(job_name, save_path)
