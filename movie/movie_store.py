"""JSON-file-backed "story bible" store for a movie-generation system.

Multi-user system. Each project is owned by EXACTLY ONE user; projects are
NOT shared. All data is partitioned strictly by ``user_id``. Every public
function takes ``user_id`` as its first argument and only ever touches that
user's data. A user can never see or modify another user's projects.

Storage layout
--------------
Each bible is stored as JSON at::

    /home/user/MCPTutorial/movie/data/<user_id>/<project_id>.json

Both ``user_id`` and ``project_id`` are sanitized to safe filename characters
(alphanumerics, dash, underscore) before being used as path components, which
prevents path traversal (e.g. ``../`` or absolute paths).

Concurrency / durability
-------------------------
A module-level ``threading.Lock`` guards every read-modify-write cycle. Writes
are atomic: data is written to a temp file in the same directory and then moved
into place with ``os.replace``.

Bible JSON schema
-----------------
{
  "project_id": str,
  "user_id": str,
  "title": str,
  "style_guide": str,
  "updated": float,  # epoch seconds
  "characters": {
      char_id: {"name": str, "desc": str, "refs": [uri, ...], "seed": int | null}
  },
  "locations": {
      loc_id: {"name": str, "desc": str, "refs": [uri, ...]}
  },
  "scenes": {
      scene_id: {
          "location": loc_id | null,
          "establish_uri": uri | null,
          "lighting": str,
          "blocking": {
              char_id: {"side": "left|center|right", "faces": "left|right|center"}
          }
      }
  },
  "shots": [
      {
          "shot_id": str,
          "scene": scene_id,
          "subject": char_id,
          "camera": { ...arbitrary dict... },
          "anchors": [str, ...],
          "intent": str,
          "keyframe_uri": uri | null,
          "video_uri": uri | null,
          "status": "planned|running|done|error"
      }
  ],
  "final_movie": uri | null
}
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

# Root directory for all user data.
DATA_ROOT = Path(__file__).resolve().parent / "data"

# Guards every read-modify-write cycle across all users/projects.
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _sanitize(value: str, *, kind: str) -> str:
    """Reduce ``value`` to safe filename characters (alnum, dash, underscore).

    Raises ValueError if nothing safe remains (prevents empty / traversal
    components such as ``..`` collapsing to nothing usable).
    """
    if not isinstance(value, str):
        raise TypeError(f"{kind} must be a str, got {type(value).__name__}")
    safe = "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_"))
    if not safe:
        raise ValueError(f"{kind} {value!r} contains no safe filename characters")
    return safe


def _user_dir(user_id: str) -> Path:
    return DATA_ROOT / _sanitize(user_id, kind="user_id")


def _bible_path(user_id: str, project_id: str) -> Path:
    return _user_dir(user_id) / f"{_sanitize(project_id, kind='project_id')}.json"


def _new_id(length: int) -> str:
    return uuid.uuid4().hex[:length]


def _atomic_write(path: Path, bible: dict) -> None:
    """Write ``bible`` as JSON to ``path`` atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{_new_id(8)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(bible, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _load(user_id: str, project_id: str) -> dict:
    """Load a bible for ``user_id``. Raises KeyError if not found for that user."""
    path = _bible_path(user_id, project_id)
    if not path.is_file():
        raise KeyError(
            f"project {project_id!r} not found for user {user_id!r}"
        )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _persist(bible: dict) -> dict:
    """Stamp ``updated`` and atomically persist the bible; returns the bible."""
    bible["updated"] = time.time()
    _atomic_write(_bible_path(bible["user_id"], bible["project_id"]), bible)
    return bible


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def create_project(user_id: str, title: str, style_guide: str = "") -> dict:
    """Create + persist a new bible; returns the full bible."""
    with _LOCK:
        project_id = _new_id(8)
        # Extremely unlikely, but avoid clobbering an existing project id.
        while _bible_path(user_id, project_id).exists():
            project_id = _new_id(8)
        bible = {
            "project_id": project_id,
            "user_id": user_id,
            "title": title,
            "style_guide": style_guide,
            "updated": time.time(),
            "characters": {},
            "locations": {},
            "scenes": {},
            "shots": [],
            "final_movie": None,
        }
        return _persist(bible)


def get_project(user_id: str, project_id: str) -> dict:
    """Return the full bible; raises KeyError if not found for that user."""
    with _LOCK:
        return _load(user_id, project_id)


def list_projects(user_id: str) -> list[dict]:
    """Return summaries for this user's projects; [] if none.

    Summary shape: {"project_id", "title", "shots": int, "updated"}.
    """
    with _LOCK:
        user_dir = _user_dir(user_id)
        if not user_dir.is_dir():
            return []
        summaries: list[dict] = []
        for path in sorted(user_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    bible = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            summaries.append(
                {
                    "project_id": bible.get("project_id", path.stem),
                    "title": bible.get("title", ""),
                    "shots": len(bible.get("shots", [])),
                    "updated": bible.get("updated", 0.0),
                }
            )
        return summaries


def update_style(user_id: str, project_id: str, style_guide: str) -> dict:
    """Set the style guide; returns the bible."""
    with _LOCK:
        bible = _load(user_id, project_id)
        bible["style_guide"] = style_guide
        return _persist(bible)


def set_style_ref(user_id: str, project_id: str, uri: str) -> dict:
    """Set the global style-reference image URI (art style/palette anchor); returns the bible."""
    with _LOCK:
        bible = _load(user_id, project_id)
        bible["style_ref"] = uri
        return _persist(bible)


def set_music(user_id: str, project_id: str, uri: str) -> dict:
    """Set the project's instrumental score URI; returns the bible."""
    with _LOCK:
        bible = _load(user_id, project_id)
        bible["music_uri"] = uri
        return _persist(bible)


def add_character(
    user_id: str,
    project_id: str,
    name: str,
    desc: str,
    refs: list | None = None,
    seed: int | None = None,
) -> str:
    """Add a character; returns the generated char_id (uuid hex[:6])."""
    with _LOCK:
        bible = _load(user_id, project_id)
        char_id = _new_id(6)
        while char_id in bible["characters"]:
            char_id = _new_id(6)
        bible["characters"][char_id] = {
            "name": name,
            "desc": desc,
            "refs": list(refs) if refs is not None else [],
            "seed": seed,
        }
        _persist(bible)
        return char_id


def update_character(user_id: str, project_id: str, char_id: str, patch: dict) -> dict:
    """Merge ``patch`` into a character (e.g. new ``refs`` after a wardrobe re-style, or ``desc``);
    returns the updated character. Raises KeyError if the char_id doesn't exist for that user."""
    with _LOCK:
        bible = _load(user_id, project_id)
        c = bible.get("characters", {}).get(char_id)
        if c is None:
            raise KeyError(f"character {char_id!r} not found in project {project_id!r}")
        c.update(patch)
        _persist(bible)
        return c


def add_location(
    user_id: str,
    project_id: str,
    name: str,
    desc: str,
    refs: list | None = None,
) -> str:
    """Add a location; returns the generated loc_id (uuid hex[:6])."""
    with _LOCK:
        bible = _load(user_id, project_id)
        loc_id = _new_id(6)
        while loc_id in bible["locations"]:
            loc_id = _new_id(6)
        bible["locations"][loc_id] = {
            "name": name,
            "desc": desc,
            "refs": list(refs) if refs is not None else [],
        }
        _persist(bible)
        return loc_id


def update_prop(user_id: str, project_id: str, prop_id: str, patch: dict) -> dict:
    """Merge ``patch`` into a prop (e.g. new ``refs``); returns the updated prop."""
    with _LOCK:
        bible = _load(user_id, project_id)
        p = bible.get("props", {}).get(prop_id)
        if p is None:
            raise KeyError(f"prop {prop_id!r} not found in project {project_id!r}")
        p.update(patch)
        _persist(bible)
        return p


def add_prop(
    user_id: str,
    project_id: str,
    name: str,
    desc: str,
    refs: list | None = None,
) -> str:
    """Add a prop (user-uploaded object reference); returns the generated prop_id (uuid hex[:6]).
    Stored under a ``props`` map, created lazily so older bibles keep working."""
    with _LOCK:
        bible = _load(user_id, project_id)
        props = bible.setdefault("props", {})
        prop_id = _new_id(6)
        while prop_id in props:
            prop_id = _new_id(6)
        props[prop_id] = {
            "name": name,
            "desc": desc,
            "refs": list(refs) if refs is not None else [],
        }
        _persist(bible)
        return prop_id


def set_scene(
    user_id: str,
    project_id: str,
    scene_id: str,
    location_id: str | None = None,
    lighting: str = "",
    blocking: dict | None = None,
    establish_uri: str | None = None,
) -> dict:
    """Upsert a scene (blocking defaults to {}); returns the bible."""
    with _LOCK:
        bible = _load(user_id, project_id)
        bible["scenes"][scene_id] = {
            "location": location_id,
            "establish_uri": establish_uri,
            "lighting": lighting,
            "blocking": dict(blocking) if blocking is not None else {},
        }
        return _persist(bible)


def add_shot(user_id: str, project_id: str, shot: dict) -> str:
    """Append a shot (assigning shot_id if missing); returns the shot_id."""
    with _LOCK:
        bible = _load(user_id, project_id)
        shot = dict(shot)
        shot_id = shot.get("shot_id")
        if not shot_id:
            existing = {s.get("shot_id") for s in bible["shots"]}
            shot_id = _new_id(6)
            while shot_id in existing:
                shot_id = _new_id(6)
            shot["shot_id"] = shot_id
        bible["shots"].append(shot)
        _persist(bible)
        return shot_id


def update_shot(user_id: str, project_id: str, shot_id: str, patch: dict) -> dict:
    """Merge ``patch`` into the matching shot; returns the shot."""
    with _LOCK:
        bible = _load(user_id, project_id)
        for shot in bible["shots"]:
            if shot.get("shot_id") == shot_id:
                shot.update(patch)
                shot["shot_id"] = shot_id  # never let a patch clobber the id
                _persist(bible)
                return shot
        raise KeyError(
            f"shot {shot_id!r} not found in project {project_id!r} for user {user_id!r}"
        )


def update_scene(user_id: str, project_id: str, scene_id: str, patch: dict) -> dict:
    """Merge ``patch`` into an existing scene (creating a bare scene if missing); returns the
    scene. Unlike set_scene this preserves untouched fields (e.g. establish_uri) — used to
    attach micro-shot / scene-video state without clobbering the scene."""
    with _LOCK:
        bible = _load(user_id, project_id)
        scene = bible["scenes"].get(scene_id, {})
        scene.update(patch)
        bible["scenes"][scene_id] = scene
        _persist(bible)
        return scene


def set_final_movie(user_id: str, project_id: str, uri: str) -> dict:
    """Set the final movie URI; returns the bible."""
    with _LOCK:
        bible = _load(user_id, project_id)
        bible["final_movie"] = uri
        return _persist(bible)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    passed = True

    def check(label: str, condition: bool) -> None:
        global passed
        status = "PASS" if condition else "FAIL"
        if not condition:
            passed = False
        print(f"[{status}] {label}")

    # Create a project for u1.
    proj = create_project("u1", "My First Movie", style_guide="noir, high contrast")
    pid = proj["project_id"]
    check("create_project returns a bible with project_id", bool(pid))
    check("create_project owned by u1", proj["user_id"] == "u1")

    # Add a character.
    char_id = add_character(
        "u1", pid, "Ada", "a curious inventor", refs=["gs://ref/ada.png"], seed=42
    )
    check("add_character returns a char_id", bool(char_id))

    # Add a shot.
    shot_id = add_shot(
        "u1",
        pid,
        {
            "scene": "s1",
            "subject": char_id,
            "camera": {"lens": "35mm", "angle": "low"},
            "anchors": ["establish"],
            "intent": "reveal the workshop",
            "keyframe_uri": None,
            "video_uri": None,
            "status": "planned",
        },
    )
    check("add_shot returns a shot_id", bool(shot_id))

    # Read it back.
    readback = get_project("u1", pid)
    print("\n--- read-back bible for u1 ---")
    print(json.dumps(readback, indent=2))
    print("--- end read-back ---\n")

    check("character persisted", char_id in readback["characters"])
    check(
        "character data intact",
        readback["characters"][char_id]["name"] == "Ada"
        and readback["characters"][char_id]["seed"] == 42,
    )
    check("shot persisted", len(readback["shots"]) == 1)
    check("shot id matches", readback["shots"][0]["shot_id"] == shot_id)

    # list_projects for u1 should show exactly 1.
    u1_projects = list_projects("u1")
    print(f"list_projects('u1') -> {u1_projects}")
    check("list_projects('u1') shows 1 project", len(u1_projects) == 1)
    check(
        "list_projects('u1') summary has shots count",
        u1_projects[0]["shots"] == 1 and u1_projects[0]["project_id"] == pid,
    )

    # list_projects for u2 must be empty (isolation).
    u2_projects = list_projects("u2")
    print(f"list_projects('u2') -> {u2_projects}")
    check("ISOLATION: list_projects('u2') is empty", u2_projects == [])

    # u2 must not be able to read u1's project.
    try:
        get_project("u2", pid)
        check("ISOLATION: u2 cannot read u1's project", False)
    except KeyError:
        check("ISOLATION: u2 cannot read u1's project (KeyError raised)", True)

    print()
    if passed:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
        raise SystemExit(1)
