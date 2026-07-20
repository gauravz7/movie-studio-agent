"""Nano-banana (Gemini image) helper for the movie pipeline.

Two operations the film pipeline needs:
  * generate_image(prompt)          — text -> image   (character sheets, establishing frames)
  * compose_image(prompt, refs)     — refs+text -> image (KEYFRAMES conditioned on anchors:
                                       establishing frame + character sheets + sibling frame)

Project/location/model come from env (set GOOGLE_CLOUD_PROJECT at runtime, or rely on ADC).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from google import genai
from google.genai import types as genai_types

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
IMAGE_MODEL = os.environ.get("NANO_BANANA_MODEL", "gemini-3.1-flash-lite-image")

_client_singleton: genai.Client | None = None


def _client() -> genai.Client:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client_singleton


def _extract(resp) -> tuple[bytes, str]:
    text = ""
    reason = ""
    for cand in resp.candidates or []:
        reason = str(getattr(cand, "finish_reason", "") or reason)
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                return inline.data, (inline.mime_type or "image/png")
            if getattr(part, "text", None):
                text += part.text
    raise ValueError(f"No image (finish_reason={reason!r}). Text: {text[:200]!r}")


def save_bytes(data: bytes, out_dir: str | Path, stem: str, mime: str = "image/png") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ext = "png" if "png" in mime else ("jpg" if "jp" in mime else "bin")
    path = out / f"{stem}.{ext}"
    n = 1
    while path.exists():
        path = out / f"{stem}_{n}.{ext}"
        n += 1
    path.write_bytes(data)
    return path


def _call(contents, model: str | None, attempts: int = 2) -> tuple[bytes, str]:
    """Call the image model, with ONE transient retry (empty/blocked responses rarely clear
    after more, so we avoid burning calls). QC-driven regeneration is handled separately."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = _client().models.generate_content(model=model or IMAGE_MODEL, contents=contents)
            return _extract(resp)
        except ValueError as e:  # no image in response — often transient
            last = e
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def generate_image(prompt: str, aspect_ratio: str = "16:9", model: str | None = None) -> tuple[bytes, str]:
    """Text -> image (character sheet, establishing frame)."""
    return _call(f"{prompt}. Aspect ratio {aspect_ratio}.", model)


QC_MODEL = os.environ.get("QC_MODEL", "gemini-3.5-flash")


# The dimensions the vision critic scores. `character_identity` and `style_consistency` are
# judged by COMPARISON against reference images (character sheets / style plate), which is why
# review_image accepts `refs` — a single-image check can't tell "same person" from "a person".
REVIEW_DIMS = ("prompt_adherence", "character_identity", "style_consistency", "framing",
               "anatomy", "extra_or_missing", "text", "lighting")


def review_image(image_path: str, expects: str, refs: list[str] | None = None) -> dict:
    """Vision film-editor critic. Returns {ok, score, issues, dims}.

    `refs` are canonical reference images (character sheets, style ref, establishing plate). When
    given, the critic judges IDENTITY and STYLE consistency of the render AGAINST them, not on the
    single image alone. `issues` is a short, ACTIONABLE fix instruction meant to be fed straight
    back into the next regeneration. Best-effort — returns ok on any parse/model failure so it can
    never crash generation."""
    import json

    ref_paths = [p for p in (refs or []) if p and Path(p).exists()]
    parts: list = [genai_types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/png")
                   for p in ref_paths]
    parts.append(genai_types.Part.from_bytes(data=Path(image_path).read_bytes(), mime_type="image/png"))
    ref_note = (f"The FIRST {len(ref_paths)} image(s) are canonical REFERENCES (characters, art "
                "style, set); the LAST image is the RENDER to review — judge character_identity and "
                "style_consistency of the render AGAINST those references. ") if ref_paths else ""
    instruction = (
        "You are a strict film-continuity editor reviewing a generated frame. " + ref_note +
        "The render SHOULD show: " + expects + "\n"
        "Fail (ok=false) if any dimension is clearly wrong: "
        "prompt_adherence (the described action/subject is shown); "
        "character_identity (same face, hair, wardrobe, colours and species as the reference sheets); "
        "style_consistency (same art style/palette/lighting as the references); "
        "framing (matches the requested shot type/angle/crop); "
        "anatomy (no malformed hands/faces/limbs); "
        "extra_or_missing (no unwanted extra, and no missing, people/creatures); "
        "text (no gibberish/garbled text); "
        "lighting (consistent time-of-day and mood).\n"
        'Reply ONLY compact JSON: {"ok":true|false,"score":0.0-1.0,'
        '"dims":{"prompt_adherence":true,"character_identity":true,...},'
        '"issues":"<short ACTIONABLE fix for the next attempt, or empty if ok>"}.'
    )
    parts.append(instruction)
    try:
        resp = _client().models.generate_content(model=QC_MODEL, contents=parts)
        txt = (resp.text or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        v = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return {"ok": bool(v.get("ok", True)), "score": float(v.get("score", 1.0) or 1.0),
                "issues": str(v.get("issues", "")), "dims": dict(v.get("dims", {}))}
    except Exception as e:  # never let the critic crash generation
        return {"ok": True, "score": 1.0, "issues": "", "dims": {}, "error": f"(review skipped: {e})"}


def qc_check(image_path: str, expects: str) -> tuple[bool, str]:
    """Back-compat shim over review_image -> (ok, issues)."""
    r = review_image(image_path, expects)
    return r["ok"], r["issues"]


def non_speaking_characters(cast: list[tuple[str, str]], style_guide: str = "") -> list[str]:
    """QC-critique which cast members CANNOT speak human dialogue — i.e. realistic (non-
    anthropomorphic) ANIMALS. `cast` is [(name, description), …]. Uses the QC/critic model.
    Respects the style: a talking-animal / anthropomorphic cartoon → animals CAN speak → returns [].
    Fail-open (returns [] on any error) so it never blocks rendering."""
    import json
    cast = [(n, d) for n, d in (cast or []) if n]
    if not cast:
        return []
    listing = "\n".join(f"- {n}: {d}" for n, d in cast)
    instr = (
        f"Film visual style: {style_guide or 'unspecified'}.\nCharacters:\n{listing}\n\n"
        "Which of these characters are REALISTIC ANIMALS that therefore CANNOT speak human dialogue? "
        "A human, robot, or an ANTHROPOMORPHIC / talking-animal cartoon character CAN speak. Only a "
        "realistic, non-anthropomorphic animal cannot. If the style is clearly a talking-animal or "
        "anthropomorphic cartoon, then animals CAN speak — return an empty list.\n"
        'Reply ONLY compact JSON: {"cannot_speak": ["<name>", ...]}.'
    )
    try:
        resp = _client().models.generate_content(model=QC_MODEL, contents=instr)
        txt = (resp.text or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        v = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        names = {n.strip().lower() for n, _ in cast}
        return [str(x) for x in v.get("cannot_speak", []) if str(x).strip().lower() in names]
    except Exception:
        return []


def compose_image(prompt: str, ref_paths: list[str], model: str | None = None) -> tuple[bytes, str]:
    """Reference images + text -> image. Used to build a KEYFRAME from the scene's anchors
    (establishing frame + character sheets + optional sibling frame) so a new camera angle
    keeps the same set, characters, wardrobe and lighting."""
    parts: list = []
    for p in ref_paths:
        data = Path(p).read_bytes()
        parts.append(genai_types.Part.from_bytes(data=data, mime_type="image/png"))
    parts.append(prompt)
    return _call(parts, model)
