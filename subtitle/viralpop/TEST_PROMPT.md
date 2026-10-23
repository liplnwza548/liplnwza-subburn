# Animated Subtitle Renderer v2 — Real Render Test

Read `SKILL.md` first.

Use the attached video as the only video input.
Use the attached project bundle's `fonts/Prompt-Bold.ttf` as the ONLY allowed font.

This is a real execution test, not a design discussion.

## GOAL
Perform:
`VIDEO -> REAL TRANSCRIPT -> VIRAL POP ANIMATED SUBTITLE -> BURN-IN -> REAL MP4`

## HARD LOCKS
- Style: Viral Pop only. Do not ask for a style choice.
- Font: `fonts/Prompt-Bold.ttf` only.
- Weight: Bold / 700.
- Stroke: 0. No stroke.
- Base text: white.
- Soft shadow: approximately 15%.
- Bluriness target: +15.
- Keyword highlighting: automatic.
- Caption target: 3–5 words.
- Position: choose automatically from visual analysis.
- Output: real `final_subtitled.mp4`.

## FONT VERIFICATION
Before render:
1. Locate `fonts/Prompt-Bold.ttf`.
2. Load that exact file.
3. Confirm it is usable by the actual renderer.
4. Do not use a system font or another fallback.
If unavailable, stop with `FONT_MISSING`.

## STT VERIFICATION
Use a real Thai-capable STT source if available.
Prefer word-level timestamps, then segment-level timestamps.
A genuine host-provided transcript may be used only when verified as real.

Never invent transcript or timestamps.
If no trustworthy transcript exists, report `STT_BLOCKED`.

## BLURINESS +15
I tested CapCut and found that `Bluriness +15` gives the subtitle a more pleasing appearance.

Implement the closest real equivalent supported by the actual renderer.
Do NOT claim that a CapCut `Bluriness` parameter exists if it does not.
State exactly what real renderer parameter/technique was used to approximate +15.
The final glyphs must remain crisp and readable.

## RENDER
Prefer a real ASS + libass + FFmpeg path.
Burn the subtitles into the source video and create:
`final_subtitled.mp4`

Preserve the source resolution, aspect ratio, duration and audio unless technically necessary.

## QA
Verify the actual output file.
Check duration, resolution, video/audio streams and codec.
When practical, inspect several rendered frames for:
- Prompt font
- zero stroke
- soft shadow
- bluriness treatment
- readable captions
- animation actually visible
- clipping
- major face/product overlap

## FINAL RESPONSE
Return only:

### STATUS
`PASS / PARTIAL / FAIL`

### FONT
Whether `Prompt-Bold.ttf` was loaded and actually used.

### STT
Where the transcript came from and whether timing is word-level or segment-level.

### RENDER
Whether Animated Subtitle was actually burned into a real MP4.

### BLURINESS
How +15 was implemented in the real renderer.

### OUTPUT
Exact path of the real MP4, if created.

### BLOCKER
Exact blocker, if any.

Do not claim success without a real MP4.
