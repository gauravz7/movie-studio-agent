"""Film-grammar validator for AI movie shot plans.

Pure logic (no I/O, no external services). Validates a ShotPlan against a set
of classical film-grammar / continuity rules and returns a list of Violations.

Run:
    uv run --with pydantic --no-project python movie/film_grammar.py
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Camera(BaseModel):
    type: str  # "establishing","wide","ls","ms","mcu","cu","ots","pov","insert","neutral","cutaway",...
    over: str | None = None  # char_id the camera shoots over (for OTS)
    height: Literal["low", "eye", "high"] = "eye"
    lens: str = "50mm"
    angle_deg: int = 0  # horizontal camera bearing, 0-359
    move: str = "static"
    vertical: Literal["up", "level", "down"] = "level"


class Shot(BaseModel):
    id: str
    scene: str
    subject: str  # char_id
    camera: Camera
    anchors: list[str] = Field(default_factory=list)
    side: Literal["left", "center", "right"] = "center"  # subject's screen side
    faces: Literal["left", "right", "center"] = "center"  # direction subject faces
    movement: Literal["left", "right", "none"] = "none"
    intent: str = ""


class ShotPlan(BaseModel):
    scene: str
    shots: list[Shot]
    known_anchors: list[str] = Field(default_factory=list)  # anchors that exist in the bible


class Violation(BaseModel):
    rule: str
    severity: Literal["error", "warn"]
    shot_ids: list[str]
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ESTABLISHING_TYPES = {"establishing", "wide", "ls", "ews"}
_NEUTRAL_TYPES = {"neutral", "cutaway"}


def _circular_diff(a: int, b: int) -> int:
    """Smallest absolute difference between two bearings on a 360-degree circle."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_plan(plan: ShotPlan) -> list[Violation]:
    """Return ALL film-grammar violations found in ``plan`` (order not significant)."""
    violations: list[Violation] = []
    shots = plan.shots

    if not shots:
        return violations

    # -- R7 establish-first (error) -----------------------------------------
    if shots[0].camera.type not in _ESTABLISHING_TYPES:
        violations.append(
            Violation(
                rule="R7",
                severity="error",
                shot_ids=[shots[0].id],
                message="scene must open on an establishing/wide shot",
            )
        )

    # -- R1 180-degree line / consistent screen sides (error) ---------------
    # A subject must keep the same left/right side. A flip is allowed only if a
    # neutral/cutaway shot has appeared earlier in the list. "center" is exempt.
    established_side: dict[str, str] = {}
    cutaway_seen = False
    for shot in shots:
        if shot.camera.type in _NEUTRAL_TYPES:
            cutaway_seen = True
        if shot.side == "center":
            continue
        prev = established_side.get(shot.subject)
        if prev is None:
            established_side[shot.subject] = shot.side
        elif prev != shot.side:
            if not cutaway_seen:
                violations.append(
                    Violation(
                        rule="R1",
                        severity="error",
                        shot_ids=[shot.id],
                        message=(
                            f"crossing the line: subject '{shot.subject}' flips screen side "
                            f"'{prev}' -> '{shot.side}' without a neutral/cutaway"
                        ),
                    )
                )
            # Re-baseline on the new side either way.
            established_side[shot.subject] = shot.side

    # -- R3 eyeline (error) -------------------------------------------------
    for shot in shots:
        if shot.side == "left" and shot.faces != "right":
            violations.append(
                Violation(
                    rule="R3",
                    severity="error",
                    shot_ids=[shot.id],
                    message=(
                        f"eyeline: subject on screen-left should face right, faces '{shot.faces}'"
                    ),
                )
            )
        elif shot.side == "right" and shot.faces != "left":
            violations.append(
                Violation(
                    rule="R3",
                    severity="error",
                    shot_ids=[shot.id],
                    message=(
                        f"eyeline: subject on screen-right should face left, faces '{shot.faces}'"
                    ),
                )
            )

    # -- R4 30-degree rule (error) ------------------------------------------
    for a, b in zip(shots, shots[1:]):
        if a.subject != b.subject:
            continue
        diff = _circular_diff(a.camera.angle_deg, b.camera.angle_deg)
        if diff < 30 and a.camera.type == b.camera.type:
            violations.append(
                Violation(
                    rule="R4",
                    severity="error",
                    shot_ids=[a.id, b.id],
                    message="jump cut: change angle >=30 deg or shot size",
                )
            )

    # -- R14 reciprocal eyeline height (error) ------------------------------
    for a, b in zip(shots, shots[1:]):
        if a.camera.type != "ots" or b.camera.type != "ots":
            continue
        if a.camera.over == b.subject and b.camera.over == a.subject:
            va, vb = a.camera.vertical, b.camera.vertical
            ok = {va, vb} == {"up", "down"} or (va == "level" and vb == "level")
            if not ok:
                violations.append(
                    Violation(
                        rule="R14",
                        severity="error",
                        shot_ids=[a.id, b.id],
                        message=(
                            f"reciprocal eyeline height mismatch: verticals '{va}' / '{vb}' "
                            "should be opposite (up/down) or both level"
                        ),
                    )
                )

    # -- R5 screen-direction (warn) -----------------------------------------
    last_movement: dict[str, str] = {}
    cutaway_seen = False
    for shot in shots:
        if shot.camera.type in _NEUTRAL_TYPES:
            cutaway_seen = True
        if shot.movement == "none":
            continue
        prev = last_movement.get(shot.subject)
        if prev is not None and prev != shot.movement and not cutaway_seen:
            violations.append(
                Violation(
                    rule="R5",
                    severity="warn",
                    shot_ids=[shot.id],
                    message=(
                        f"screen direction reversed for '{shot.subject}': "
                        f"'{prev}' -> '{shot.movement}'"
                    ),
                )
            )
        last_movement[shot.subject] = shot.movement

    # -- R19 lens consistency (warn) ----------------------------------------
    lenses: list[str] = []
    for shot in shots:
        if shot.camera.lens not in lenses:
            lenses.append(shot.camera.lens)
    if len(lenses) > 1:
        violations.append(
            Violation(
                rule="R19",
                severity="warn",
                shot_ids=[s.id for s in shots],
                message=f"inconsistent lens within scene: {', '.join(lenses)}",
            )
        )

    # -- Anchors (error) ----------------------------------------------------
    known = set(plan.known_anchors)
    for shot in shots:
        missing = [a for a in shot.anchors if a not in known]
        if missing:
            violations.append(
                Violation(
                    rule="anchors",
                    severity="error",
                    shot_ids=[shot.id],
                    message=f"unknown anchors {missing} in shot '{shot.id}'",
                )
            )

    return violations


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def _valid_plan() -> ShotPlan:
    anchors = ["establish:kitchen", "char:maya", "char:ben"]
    return ShotPlan(
        scene="kitchen",
        known_anchors=anchors,
        shots=[
            Shot(
                id="s0",
                scene="kitchen",
                subject="maya",
                side="center",
                faces="center",
                anchors=["establish:kitchen"],
                camera=Camera(type="establishing", lens="50mm", angle_deg=0),
                intent="open on the room",
            ),
            Shot(
                id="s1",
                scene="kitchen",
                subject="maya",
                side="left",
                faces="right",
                anchors=anchors,
                camera=Camera(
                    type="ots", over="ben", lens="50mm", angle_deg=0, vertical="down"
                ),
                intent="over ben's shoulder onto maya",
            ),
            Shot(
                id="s2",
                scene="kitchen",
                subject="ben",
                side="right",
                faces="left",
                anchors=anchors,
                camera=Camera(
                    type="ots", over="maya", lens="50mm", angle_deg=180, vertical="up"
                ),
                intent="reverse: over maya's shoulder onto ben",
            ),
        ],
    )


def _invalid_plan() -> ShotPlan:
    known = ["char:maya", "char:ben"]
    return ShotPlan(
        scene="diner",
        known_anchors=known,
        shots=[
            # No establishing shot to open -> R7
            Shot(
                id="s0",
                scene="diner",
                subject="maya",
                side="left",
                faces="right",
                anchors=["char:maya"],
                camera=Camera(type="ots", over="ben", lens="50mm", angle_deg=0, vertical="down"),
            ),
            Shot(
                id="s1",
                scene="diner",
                subject="ben",
                side="right",
                faces="left",
                anchors=["char:ben"],
                camera=Camera(type="ots", over="maya", lens="50mm", angle_deg=180, vertical="up"),
            ),
            # ben was screen-right; now screen-left with no cutaway -> R1 line cross.
            # New lens 85mm -> R19.
            Shot(
                id="s2",
                scene="diner",
                subject="ben",
                side="left",
                faces="right",
                anchors=["char:ben"],
                camera=Camera(type="ms", lens="85mm", angle_deg=200),
            ),
            # Same subject, same shot type, angle diff 10 deg -> R4 jump cut.
            # Unknown anchor char:zoe -> anchors error.
            Shot(
                id="s3",
                scene="diner",
                subject="ben",
                side="left",
                faces="right",
                anchors=["char:zoe"],
                camera=Camera(type="ms", lens="85mm", angle_deg=210),
            ),
        ],
    )


def _summarize(name: str, plan: ShotPlan) -> None:
    violations = validate_plan(plan)
    errors = [v for v in violations if v.severity == "error"]
    warns = [v for v in violations if v.severity == "warn"]
    print(f"=== {name} (scene: {plan.scene}) ===")
    for v in violations:
        print(f"  [{v.severity}] {v.rule} {v.shot_ids}: {v.message}")
    if not violations:
        print("  (no violations)")
    print(f"  -> {len(errors)} errors, {len(warns)} warns")
    print()
    return errors, warns


if __name__ == "__main__":
    errors, warns = _summarize("PLAN 1", _valid_plan())
    print(f"VALID PLAN: {len(errors)} errors, {len(warns)} warns")
    print()

    _summarize("PLAN 2", _invalid_plan())
    print("INVALID PLAN violations listed above.")
