---
name: script-developer
description: Turn a vague movie idea OR an uploaded script into an approved cast and scene breakdown, iterating with the user, BEFORE any art is generated. Use at the start of every movie request.
---

# Script Developer

You develop a film **with the user, in the loop** — clarifying, proposing, and getting explicit
approval at each stage. **Never generate art until the user approves the characters and scenes.**

## Workflow (stop and WAIT for the user at every ⏸)

1. **Intake — vague idea or a full script?**
   - *Vague idea:* ask 3–5 clarifying questions (see `references/clarifying-questions.md`): premise,
     tone/genre, length (number of scenes), audience, and any must-have characters. ⏸ Wait.
   - *Uploaded script/story:* parse it — extract the logline, the characters, and a scene-by-scene
     breakdown (see `references/script-parsing.md`). Summarize what you understood. ⏸ Wait for
     confirmation/corrections.

2. **Treatment.** Present a short treatment: **logline**, **visual style**, a **cast list**
   (name + one-line description each), and a **numbered scene list** (one line per beat).

3. **Approve CHARACTERS.** ⏸ Ask: "Happy with this cast, or change anything?" Revise on feedback.
   Do **not** call any art tool yet.

4. **Approve SCENES + PACING.** ⏸ Ask: "Happy with these scenes/order, or change anything?" Revise on
   feedback. **Video is capped at 10 seconds per clip**, and each scene renders to ONE clip — so size
   scenes to ≤~10s of action (split a long beat into separate scenes rather than overstuffing one),
   and for each scene **recommend a frame count** (default 3 frames ≈ 10s, ~3s per frame; 2 for a
   simple beat, 4 for a busy one) for the user to confirm or edit before rendering.

5. **Hand off to rendering** (the `film-director` skill + movie tools):
   - `create_project` → `generate_style_ref` → `add_character` per approved character.
   - After the character **reference sheets** are generated, ⏸ **show them and ask for approval**
     before rendering scenes; regenerate any the user rejects.
   - Then per scene: `establish_scene` → `plan_scene` (validated) → `generate_shot`. Show the
     keyframe(s) and ⏸ ask for approval; regenerate on feedback before moving on.

6. **Iterate** freely on any stage the user wants to change; only advance on explicit approval.

## Hard rules
- **Human-in-the-loop:** never skip an approval ⏸. One stage at a time.
- **Art constraints (or the image model blocks it):** original, generic, clearly-**adult** human
  descriptions; **no copyrighted/IP names** (e.g. don't name a character after a known film
  character — use an original name); animals are fine.
- Keep the story public-domain/original; you may retell a classic tale with original names.
