"""Lyria 3 (Gemini music) helper — instrumental score for a project.

Text -> music, same shape as the image model: models.generate_content with
response_modalities=["AUDIO","TEXT"]; the audio bytes come back in
candidates[0].content.parts[].inline_data.data (audio/mpeg).  Region: GLOBAL.
Ref: GCP generative-ai/audio/music/getting-started/lyria3_music_generation.ipynb.
"""

from __future__ import annotations

import os
from pathlib import Path

from google import genai
from google.genai import types as genai_types

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
MUSIC_MODEL = os.environ.get("LYRIA_MODEL", "lyria-3-pro-preview")

_client_singleton: genai.Client | None = None


def _client() -> genai.Client:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client_singleton


def generate_music(prompt: str, model: str | None = None) -> tuple[bytes, str]:
    """Text -> instrumental music. Returns (bytes, mime_type)."""
    resp = _client().models.generate_content(
        model=model or MUSIC_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(response_modalities=["AUDIO", "TEXT"]),
    )
    for cand in resp.candidates or []:
        for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                return inline.data, (inline.mime_type or "audio/mpeg")
    raise ValueError("Lyria returned no audio")


def save_music(data: bytes, out_dir: str | Path, stem: str, mime: str = "audio/mpeg") -> Path:
    ext = "mp3" if "mpeg" in mime or "mp3" in mime else ("wav" if "wav" in mime else "audio")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stem}.{ext}"
    n = 1
    while path.exists():
        path = out / f"{stem}_{n}.{ext}"
        n += 1
    path.write_bytes(data)
    return path


if __name__ == "__main__":  # smoke test (needs creds)
    b, m = generate_music("Warm cinematic instrumental, solo piano and soft strings, slow, hopeful.")
    p = save_music(b, "generated/samples", "music_smoke", m)
    print("PASS", p, len(b), "bytes", m)
