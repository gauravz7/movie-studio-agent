# Film-editor review rubric

The per-dimension checklist behind the `film-editor` skill and the `review_asset` / QC critic. Each
dimension lists what to check and the corrective action to feed back into a regeneration.

| Dimension | Fails when… | Fix to feed back |
|---|---|---|
| **prompt_adherence** | the described action/subject/beat isn't shown | restate the missing action concretely: "she is POURING the tea, mid-motion" |
| **character_identity** | face, hair, wardrobe, colours or species differ from the sheet | "make <name>'s face/hair/wardrobe EXACTLY match their reference sheet; keep everything else" |
| **style_consistency** | art style, palette or rendering drifts from the style ref | "match the art style, palette and brushwork of the style reference EXACTLY" |
| **framing** | shot size/angle/crop is wrong (a "CU" rendered wide, bad headroom) | "reframe as a <shot type> at <angle>; <subject> fills the frame as specified" |
| **anatomy** | malformed hands, faces, limbs; extra/missing fingers | "fix the malformed <hands/face>; natural, correct anatomy" |
| **extra_or_missing** | unwanted extra people/creatures, or a required one absent | "remove the extra <thing>" / "add the missing <name>; no one else" |
| **text** | gibberish/garbled text baked into the image | "no text anywhere" (micro-shot SHOT labels are the only allowed text) |
| **lighting** | time-of-day / mood inconsistent with the scene plate | "match the <dusk/warm lamp> lighting of the establishing plate" |
| **continuity** | inconsistent with sibling frames (screen side, wardrobe, props) | "keep <name> on screen-<side> and in <wardrobe>, consistent with the other shots" |

## Severity → action
- **Blocking** (identity drift, missing/extra characters, gibberish text, broken continuity): must be
  fixed or escalated — never ship silently.
- **Degrading** (minor anatomy, soft style drift, imperfect framing): fix if a retry is cheap;
  otherwise flag to the user and let them decide.

## Video-specific (for start_scene_video / clips)
The image critic doesn't watch video. When reviewing a clip, judge by eye:
- **motion quality** — natural, no morphing/flicker; camera move matches the framing.
- **beat order** — the panels play in order across the duration.
- **lip-sync** — when `audio=true`, mouth movement matches the spoken line.
- **temporal identity** — the character stays the same person across the whole clip.
Fixes usually mean adjusting the beats (shorter, clearer actions) or regenerating the micro-shot
first, then re-animating.

## Escalation template
> "Attempt N still has: <qc_issues>. Best version: <resource_uri>. Want me to (a) try again with a
> tweak, (b) adjust the description/style, or (c) keep this one?"
