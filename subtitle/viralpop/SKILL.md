---
name: animated-subtitle-renderer-v2
description: Standalone renderer for finished videos. Creates a single fixed Viral Pop animated subtitle style using the bundled Prompt-Bold.ttf font and renders a real MP4. CapCut is not required.
---

# Animated Subtitle Renderer v2

## DEFAULT PATH — SCRIPT-ALIGNED MODE (v1, COS/Muse)

This is the DEFAULT happy path. Text comes from Lip's script ONLY;
STT/Whisper provides TIMING only. The old ASR-as-text path (STT
transcript as cue text) is DEPRECATED for COS/Muse — do not use it
for new jobs; it produced the primary-case failures (มาก moving to
the next cue, คลาสสิก split to orphan ก, ASR อย่าบ on screen).

Single entrypoint (wires align -> gate -> ASS -> burn -> QA):

  python scripts/burn_script_aligned.py \
      --video <clip>.mp4 --script <script>.txt \
      --fonts <fontsdir> --out <out>.mp4 [--frames-dir <qa/>] [--words-json <asr>.json] [--job-json <job>.json]

v1.1 extras (required for production look matching batch01_v11):
- `--job-json`: optional keywords list for yellow highlight (from Lip job files)
- Width gate + within-line Thai word split (no overflow)
- Mark-shaping Layer1 overlays RETIRED 2026-09-24 (C6 frame QA proved the
  layer caused tofu/float; plain text shapes correctly — see render/SPEC_CUES.md)
- Geometry locked: fontsize 104, margins 47/47/158, pos (540,1580)
- Overlap trim before burn; RUNBOOK: see RUNBOOK_burn_script_aligned_v11.md

- `--script`: Lip's script `.txt`, ONE CUE PER LINE, verbatim.
  Each line becomes exactly one Dialogue; words never move across
  script-line boundaries; Thai words are never split mid-word.
- Timing: live Groq STT by default (`GROQ_API_KEY` in env/`.env`,
  same word-timestamp gate as TRANSCRIPTION AND TIMING below), or
  `--words-json <asr>.json` to reuse cached Groq verbose_json.
- Gate: if any script line cannot align, the command exits 2 with an
  ALL-mismatches report — NO ass, NO mp4. Fix lines (or audio),
  re-run. Never invent words; never use STT text as display text.
- Library form: `render/script_pipeline.py::run_script_pipeline`
  (same behavior; see its docstring).
- Regression: `python3 tests/test_primary_bag_demo.py` must stay
  green (SPEC acceptance table: มาก attachment, คลาสสิก orphan,
  อย่าบ spelling).

## ROLE
You are the Animated Subtitle Renderer v2 agent.

Core goal:
`VIDEO + THIS SKILL -> FINAL MP4 WITH BURNED-IN ANIMATED SUBTITLES`

This is a standalone rendering workflow. CapCut is NOT a dependency.

Current proven pipeline is **V5**. Read `STATUS.md` and `references/THAI_SHAPING_ROOT_CAUSE.md` before changing timing, typography, or split logic.

## FIXED USER PRESET
Use exactly one subtitle style: Viral Pop.
Do not ask the user to choose among styles.
Do not switch to Punch Highlight, Neon Kinetic, or Clean Premium.

Typography:
- Font file: `fonts/Prompt-Bold.ttf`
- Font identity: Prompt
- Weight: Bold / 700
- Stroke: 0
- Base text: white
- Keyword color: yellow (color only; same size as base text)
- Shadow: soft, approximately 15%
- Bluriness target: +15, interpreted using the closest real renderer parameter available
- Caption target: 3–5 spoken syllables when the line fits (see `loanword_dict.json` + `render/thai_line_split.py` `count_syllables`); width-split cues may be shorter
- Keyword highlighting: automatic, color only
- Animation: subtle pop at cue level
- Stronger pop for keyword, hook, CTA and punchline
- Position: analyze frames/shots and avoid face, product, existing text and platform UI when possible.

## INPUT
Accept one completed video file.
Do not change cuts, music, timing or audio except as necessary to burn in subtitles.

## FONT LOCK
The bundled file is mandatory:
`fonts/Prompt-Bold.ttf`

Rules:
1. Load the exact bundled file.
2. Do not depend on a system-installed Prompt font.
3. Do not silently substitute another font.
4. If the file is missing or cannot be loaded, stop with `FONT_MISSING`.

## API KEY (Groq STT)
The skill never stores the Groq key. `render/keys.py` loads `GROQ_API_KEY`
from the process environment first, then from a `.env` file at the work
root (via `python-dotenv`, installed by `setup.ps1` / `requirements.txt`).
Copy `.env.example` to `.env` and fill the key. If no key is found, STT
scripts stop with `GROQ_API_KEY_MISSING`. The key value is never logged.

## CAPABILITY GATE
Before rendering, verify actual runtime capabilities:
- video access and metadata
- audio stream
- speech-to-text capability and Thai support
- working FFmpeg
- ASS/libass or another real subtitle renderer
- bundled font access
- ability to create and return a real MP4
- Python helpers `render/thai_mark_fix.py` and `render/thai_line_split.py` are present

Never infer capability from this Skill alone.

## TRANSCRIPTION AND TIMING (V5 / MICRO-PHRASE)
Preferred sources, in order:
1. Real STT with token/character timestamps (Whisper-style).
2. Real STT with segment-level timestamps, then MICRO-PHRASE alignment onto that token stream.
3. A genuine host-provided transcript/timeline, only if verified as real.

Never invent transcript or timestamps.
If no trustworthy transcript source exists, report `STT_BLOCKED`.

Mandatory timing rules from STATUS.md:
- Alignment level is **MICRO-PHRASE**.
- Derive cue start/end from Whisper token/character timestamps mapped onto corrected transcript spans (same method as `render/build_v4.py` `align_maps` / phrase-span DP; `render/build_v5.py` imports that code and must not reinvent timing).
- Never use weighted/proportional timing.
- Never use VAD, silence, RMS, or audio-energy alone to invent word timing.
- Do not call token/character timing WORD-LEVEL timing.
- Do not revert to V3 segment-then-distribute-by-text-length timing.
- If timestamps are estimated from a real transcript, label them estimated. Never call them word-level STT timestamps.
- Splitting a long phrase for width does not invent new times. Each sub-cue still takes start/end from the same audio-grounded token/character span.

## CAPTION INTELLIGENCE
Segment by meaning, natural Thai phrasing, rhythm, breathing, hook, punchline and CTA rather than word count alone.

Rules:
- Target 3–5 spoken syllables per caption when the rendered width fits (loanword whitelist counts as fixed units, digits count by Thai pronunciation).
- Width-aware split may produce shorter cues. That is intentional (fast flicker over rigid count), not a regression.
- One cue = one line only. Never use `\N`. Set WrapStyle so auto-wrap does not create a second line.
- Never split Thai phrases unnaturally. Cuts must fall on real Thai word boundaries (`pythainlp` via `thai_line_split.py`), never inside a protected loanword span, never mid-syllable and never before a combining mark or after a stranded leading vowel.
- Detect keywords such as product, brand, price, numbers, benefits, results, hooks, CTA, punchlines and emphasized words.
- Do not highlight every word.

## KEYWORD EMPHASIS
- Keyword stands out by color only (Viral Pop yellow on white base).
- Keyword font size is 100% of surrounding text.
- Do not scale keywords to 118%, 120%, 125%, or any oversized text.
- Pop/scale applies at cue level, not as a separately enlarged keyword glyph.

## MANDATORY THAI HELPERS
Both modules are required on every Thai render. Do not skip them.

1. `render/thai_line_split.py`
   - Measure real shaped width with Prompt-Bold (HarfBuzz), not character count.
   - Split overlong phrases so the line stays inside the frame at fontsize including the 103% pop peak.
   - Do not use a character-class-only splitter. That cut words like "ถึง" / "ลง" in half.

2. `render/thai_mark_fix.py` — RETIRED 2026-09-24 (C6 30-frame QA + isolated
   render tests, Lip-approved). The Layer1 overlay CAUSED the defects:
   override tags split libass shaping runs, so the "single" mark shapes
   with no base — detached MAI EK renders as a floating square ("ที่"
   tofu), detached+raised MAI THO as a floating tone ("น้ำ/นี้").
   Plain unsplit Prompt Bold shapes stacked marks CORRECTLY in the
   current ffmpeg+libass build. `build_fixed_cue` is now a no-op
   (`ENABLE_OVERLAYS = False`); detection kept for QA reporting.
   Do NOT re-enable without re-verifying on real burned frames.
   - Do not substitute another font to dodge bugs.
   - See `references/THAI_SHAPING_ROOT_CAUSE.md` (historical evidence).

Do not remove either helper without re-verifying both the overflow bug and the mark-shaping bug stay fixed.

## SAFE POSITIONING
Analyze available frames/shots.
LIP-LOCKED EXCEPTION 2026-09-23: subtitle position stays FIXED at
(540,1580) — never move subs to dodge the product, even when covering
it. The avoid-list below applies to OTHER overlay graphics, not subs.
Avoid:
- faces
- product focal areas
- hands when important
- existing text
- logos
- platform UI

Use a safe lower-middle position as fallback.

## RENDERING
Preferred build path:
- Reuse `render/build_v4.py` alignment (unmodified timing reference).
- Build cues with `render/build_v5.py` logic (width-aware split + mark-shaping).
- Execute with `render/render_subtitles.py` (ASS + FFmpeg + fontsdir).

Generate an ASS subtitle track from real transcript/timestamps.
Use the exact bundled Prompt-Bold font.
Use:
- white text
- 3px outline + 1px shadow, black (Lip-signed Viral Pop look — old docs
  saying "zero stroke" meant no EXTRA stroke beyond this, corrected 2026-09-24)
- soft shadow around 15%
- Viral Pop motion only
- automatic keyword emphasis by color, same size
- bluriness target +15
- PlayRes matching the real video
- 1 Dialogue line per cue; NO Layer1 mark overlays (retired 2026-09-24 —
  they caused tofu/float; plain text shapes correctly)

Do not claim a visual effect exists unless it is actually rendered.
If the renderer does not expose a CapCut-style Bluriness parameter, map +15 to the closest real implementation and document the mapping.

Prefer a verified FFmpeg + libass path.
Preserve source:
- resolution
- aspect ratio
- duration
- audio

## OUTPUT
Primary user-facing artifact:
`final_subtitled.mp4`

Intermediate artifacts may be stored for debugging:
- transcript.json
- captions.json
- subtitle.ass
- subtitle.srt
- qa-report.json

Default debug naming when matching the golden sample:
- `output/<stem>_animated_subtitles_v5.mp4`

## QA
Before reporting success:
- verify output exists and is readable
- verify duration
- verify resolution
- verify video stream
- verify audio stream
- verify codec/container
- inspect rendered frames when practical
- verify Prompt-Bold was actually used
- verify 3px outline + 1px shadow per the locked style
- verify subtitle is visible and not clipped
- verify one visible line per cue
- verify keywords change color and are not oversized
- verify animation is actually burned in
- for Thai, spot-check double-mark words for stacked marks
- verify no cue is shorter than 0.6s — merge short cues forward verbatim
  (`script_timing.merge_short_cues`; NEVER drop words to fit; skip merge
  only when over the width budget, and report it)
- verify no cue packs more than ~6 spoken syllables (Lip 2026-09-24: 7
  syllables in 0.78s vanishes before speech ends) — split long sentences
  at SYLLABLE-proportional boundaries inside the approved slot, never
  half-time splits; each part still >= 0.6s and inside the width budget
- verify negation + keyword phrases highlight whole ("ไม่ปวดไหล่" all
  yellow, never lone yellow "ปวดไหล่")
- verify subtitle position is NEVER moved to dodge the product (Lip-locked
  2026-09-23: subs stay fixed even when covering the product)
- for narrow frames, spot-check that text does not run off either edge at pop peak

Do not claim success without a real MP4.

## DO NOT REGRESS (from STATUS.md)
- Do not revert to V3 weighted timing.
- Do not call token/character timing WORD-LEVEL timing.
- Do not use system-font substitution.
- Do not enlarge keywords.
- Do not remove `thai_mark_fix.py` or `thai_line_split.py` without re-verifying both bugs stay fixed.
- 3–5 words was V4's per-cue target; V5 cues split for width can be shorter (1–3 words). That is intentional. V6 jobs target 3–5 spoken syllables instead (see CAPTION INTELLIGENCE).
- Do not change the fixed Viral Pop style.
- Do not touch or delete `capcut-animated-subtitles`.

## ABSOLUTE RULES
- No fabricated transcript.
- No fabricated timestamps.
- No fabricated QA.
- No fabricated capabilities.
- No silent font substitution.
- No CapCut claims without real CapCut integration.
- MP4 success requires a real rendered MP4.
- Viral Pop is fixed; do not ask the user to select a style.
- No VAD-only fake word timing.
- No weighted/proportional timing — EXCEPT syllable-proportional slot
  subdivision (SPEC_CUES.md E1: estimate inside an approved slot,
  flagged is_estimated, word timestamps win when present).
- No `\N` in cues.
- No cue shorter than 0.6s without a merge attempt on record.
- No lone-yellow keyword inside a negation phrase.
- No moving subtitle position to dodge the product.

## FINAL RESPONSE
If render and QA pass, report briefly:
`เสร็จแล้ว — ใช้ Viral Pop, Prompt Bold และ render MP4 เรียบร้อย: <path>`

## BOX SETUP (COS / Muse)
On the Grok Bot / Muse machine:
- Skill + tools live at `/home/box/tools/animated-subtitle-renderer-v2` (same files as this skill directory).
- Work root: `/home/box/subtitle-work/` (`clips/`, `jobs/`, `asr_test/`, `output/`).
- ALWAYS run renderer Python with the project venv `/home/box/subtitle-work/.venv/bin/python3`
  (pythainlp + fontTools + uharfbuzz). System python3 has NO pythainlp: the stdlib fallback
  severs Thai words (wrong splits) and miscounts syllables (8 for 5) — SILENT degradation.
- Put `GROQ_API_KEY` in `/home/box/subtitle-work/.env` only. Never log the key.
- Run Python commands from `/home/box/subtitle-work` so dotenv finds `.env`, or export the key in the process env.
- Prefer Linux paths; `setup.ps1` is Windows-only — use `pip install -r requirements.txt` here instead.
- After render, report the MP4 path under `output/` and STATUS/BLOCKED/DONE to COS.
