# Continuity Rules (Film Grammar)

Each rule is marked `[ENFORCED]` (checkable programmatically on the shot plan
JSON before rendering) or `[GUIDANCE]` (directorial judgment; reviewed after
render). `[ENFORCED-ish]` means partially checkable from plan metadata.

## A. Spatial continuity

- **180-degree rule / axis of action** `[ENFORCED]` — Once the line of action
  between two subjects is set, keep all cameras on the same side of it so screen
  directions stay consistent. Validator: every shot's camera position is on the
  established side of `line_of_action_deg`.
- **Legitimate line crossing** `[ENFORCED]` — The line may only be crossed via a
  neutral (on-the-line) shot or a cutaway that re-establishes geography.
  Validator: a side flip must be preceded by a neutral/cutaway shot.
- **Eyeline match** `[ENFORCED]` — A subject looking off-screen must be answered
  by the object/subject on the matching side and consistent height. Validator:
  reciprocal eyeline directions across a shot/reverse pair.
- **30-degree rule** `[ENFORCED]` — Consecutive shots of the same subject must
  differ in camera angle by at least ~30 degrees (or change shot size) to avoid a
  jump cut. Validator: angular delta between same-subject adjacent shots.
- **Screen-direction continuity** `[ENFORCED]` — A subject moving/facing a
  direction keeps that direction across the cut unless a neutral shot resets it.
  Validator: `screen_side` and motion vectors preserved.
- **Match on action** `[GUIDANCE]` — Cut during a motion so the movement carries
  across the edit; smooths the transition.

## B. Coverage

- **Establish first** `[ENFORCED]` — A scene must open with (or early-provide) an
  establishing frame before tighter coverage. Validator: an establishing shot
  precedes the first close/medium shot.
- **Shot-size progression / avoid jump cuts** `[ENFORCED]` — Adjacent shots of
  one subject change size or angle meaningfully; no near-identical repeats.
  Validator: size/angle step between adjacent same-subject shots.
- **Shot/reverse-shot** `[pattern]` — See `shot-patterns.md`.
- **Motivated cut** `[GUIDANCE]` — Each cut should be driven by content (a look,
  a line, an action), not arbitrary.

## C. Framing

- **Lead room / nose room** `[ENFORCED-ish]` — Leave space in the frame ahead of
  a subject's gaze or motion. Checkable when framing/eyeline metadata is present.
- **Headroom** `[GUIDANCE]` — Appropriate space above the head; not too tight,
  not floating.
- **Rule of thirds / eye level** `[GUIDANCE]` — Place key elements on thirds;
  default eye placement in the upper third.
- **Reciprocal eyeline height in reverses** `[ENFORCED]` — In a reverse, the
  camera height/eyeline must mirror the sibling shot so the two subjects appear
  to share a look. Validator: `reverse_of` shots have matching eyeline height.

## D. Temporal continuity

- **Prop / wardrobe / hair continuity** `[ENFORCED via shared anchors]` — Held by
  re-conditioning every shot on the canonical character/prop reference images.
  Validator: required anchor refs present on each shot.
- **Lighting & time-of-day continuity** `[ENFORCED-ish]` — Consistent light
  direction, quality, and time-of-day across a scene. Validator: `time_of_day`
  matches the scene; establishing frame reused as lighting anchor.
- **Blocking / performance continuity** `[ENFORCED]` — Subject positions and
  screen sides remain consistent shot to shot. Validator: `screen_side` per
  character constant within the scene.
- **Graphic / match cut** `[pattern]` — See `shot-patterns.md`.

## E. Camera / lens

- **Lens / perspective consistency within a scene** `[ENFORCED]` — Keep a coherent
  lens set (especially matched focal length across a shot/reverse pair) so
  perspective doesn't jump. Validator: `lens_mm` consistent across reverse pairs.
- **Angle semantics** `[GUIDANCE]` — low angle = power/threat, high angle =
  vulnerability, eye level = neutral/parity. Choose to match the beat's status.

## Review checklist (run after render)

1. Are all `[ENFORCED]` rules still visually true in the rendered frames?
2. Do wardrobe/props/hair match the canonical references?
3. Is lighting/time-of-day consistent across the scene?
4. Do eyelines and screen sides read correctly across cuts?
5. Regenerate any failing shot; escalate only genuine creative forks to the human.
