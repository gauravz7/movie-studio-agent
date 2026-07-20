"""Local end-to-end test of the movie MCP pipeline (drives the core functions directly).

Proves: user-scoped projects, character reference sheets, establishing frame + blocking,
film-grammar validation GATE (valid plan persists, invalid plan is rejected with violations),
keyframe composition from anchors, and cross-user isolation. Uses real nano-banana.

Run:  cd movie && GOOGLE_CLOUD_PROJECT=<proj> uv run python test_local.py
"""

from __future__ import annotations

import os
from pathlib import Path

import movie_server as m
import movie_store as store

U = "creator1"          # our solo creator
OTHER = "creator2"      # a different user (must not see U's project)


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    assert cond, label


print("== 1. create project ==")
proj = m.mv_create_project(U, "The Last Lighthouse", "35mm, teal-orange, anamorphic, moody")
pid = proj["project_id"]
check(f"project created {pid}", bool(pid))

print("\n== 2. add characters (real reference sheets) ==")
maya = m.mv_add_character(U, pid, "Maya", "keeper, 60s, grey braid, weathered coat")
ben = m.mv_add_character(U, pid, "Ben", "young deckhand, 20s, yellow raincoat")
check("maya sheet", os.path.exists(maya["ref_uri"]))
check("ben sheet", os.path.exists(ben["ref_uri"]))
mid, bid = maya["char_id"], ben["char_id"]
print("   maya:", maya["ref_uri"]); print("   ben :", ben["ref_uri"])

print("\n== 3. establish scene (frame + blocking) ==")
scene = "kitchen"
blocking = {mid: {"side": "left", "faces": "right"}, bid: {"side": "right", "faces": "left"}}
est = m.mv_establish_scene(U, pid, scene, "lighthouse kitchen at dusk, Maya and Ben",
                           lighting="dusk, warm lamp", blocking=blocking)
check("establishing frame", os.path.exists(est["establish_uri"]))
print("   establish:", est["establish_uri"])

anchors = [f"establish:{scene}", f"char:{mid}", f"char:{bid}"]

print("\n== 4a. plan scene — VALID shot/reverse-shot (should PASS + persist) ==")
valid = [
    {"id": "s0", "subject": mid, "side": "center",
     "camera": {"type": "wide", "lens": "35mm"}, "anchors": anchors, "intent": "establish"},
    {"id": "s1", "subject": mid, "side": "left", "faces": "right",
     "camera": {"type": "ots", "over": bid, "height": "eye", "lens": "50mm",
                "angle_deg": 0, "vertical": "down"}, "anchors": anchors, "intent": "Maya speaks"},
    {"id": "s2", "subject": bid, "side": "right", "faces": "left",
     "camera": {"type": "ots", "over": mid, "height": "eye", "lens": "50mm",
                "angle_deg": 180, "vertical": "up"}, "anchors": anchors, "intent": "Ben replies"},
]
r = m.mv_plan_scene(U, pid, scene, valid)
print("   errors:", r["errors"], "persisted:", r["persisted"],
      "warns:", sum(1 for v in r['violations'] if v['severity'] == 'warn'))
check("valid plan has 0 errors", r["errors"] == 0)
check("valid plan persisted", r["persisted"] is True)

print("\n== 4b. plan scene — INVALID (no establish-first + bad anchor) → REJECTED ==")
invalid = [
    {"id": "x1", "subject": mid, "side": "left", "faces": "left",   # eyeline wrong too
     "camera": {"type": "ots", "over": bid, "lens": "85mm", "angle_deg": 0},
     "anchors": [f"char:{mid}", "char:zoe"], "intent": "bad"},       # char:zoe doesn't exist
]
ri = m.mv_plan_scene(U, pid, scene, invalid)
print("   errors:", ri["errors"], "persisted:", ri["persisted"])
for v in ri["violations"]:
    print(f"     [{v['severity']}] {v['rule']} {v['shot_ids']}: {v['message']}")
check("invalid plan rejected (errors>0)", ri["errors"] > 0)
check("invalid plan NOT persisted", ri["persisted"] is False)

print("\n== 5. generate keyframes for the reverse pair (real nano-banana compose) ==")
k1 = m.mv_generate_shot(U, pid, "s1")
k2 = m.mv_generate_shot(U, pid, "s2")
check("s1 keyframe", os.path.exists(k1["keyframe_uri"]))
check("s2 keyframe", os.path.exists(k2["keyframe_uri"]))
print(f"   s1 keyframe (refs_used={k1['refs_used']}):", k1["keyframe_uri"])
print(f"   s2 keyframe (refs_used={k2['refs_used']}):", k2["keyframe_uri"])
check("keyframes composed from anchors", k1["refs_used"] >= 2 and k2["refs_used"] >= 2)

print("\n== 6. cross-user isolation ==")
check("other user sees no projects", m.mv_list_projects(OTHER) == [])
try:
    store.get_project(OTHER, pid)
    check("other user cannot read project", False)
except KeyError:
    check("other user cannot read project", True)

print("\n== 7. final bible snapshot ==")
b = m.mv_get_project(U, pid)
print("   characters:", list(b["characters"]))
print("   scenes:", list(b["scenes"]))
print("   shots:", [(s["shot_id"], s["status"]) for s in b["shots"]])

print("\nALL LOCAL TESTS PASSED ✅")
