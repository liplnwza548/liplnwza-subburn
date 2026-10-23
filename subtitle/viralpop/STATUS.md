# Animated Subtitle Renderer — STATUS

## Current Status
**2026-10-10 IRWIN VISE-GRIP batch (10CR, 10WR, 10R): PASS — delivered to Drive**

- **Audio/Script Sync Policy Update (Lip 2026-10-10)**:
  - Previously, rendering was blocked under old strict constraints:
    1. Readability floor refusal on fast-speech cues (`MIN_CUE_FLOOR 0.5s/0.6s`, `_SNAP_MIN_DUR 0.6s`).
    2. Alignment gate refusal (`exit 2`) on draft scripts when voiceover artist improvised words or omitted script lines during actual recording (e.g. skipped "ตามสเปกที่ผู้จ้างให้มา" in 10WR; skipped "ตามสเปกในบรีฟ" & "หรือ 1-7/8 นิ้ว" in 10R).
  - **Resolution**: Lip clarified that strict duration floors (0.5s/0.6s) were never intended to block natural fast-spoken speech. Speech sync is primary; subtitles can appear and disappear quickly when spoken quickly.
  - `vo` profile updated: `min_cue_floor = 0.2s`, `max_syl = 10`, `refuse_rate = 20.0syl/s`, and `_SNAP_MIN_DUR = 0.2s`.
  - Scripts aligned to verbatim voiceover delivery (omitting unrecorded lines, capturing natural improvisations).
- **Video Re-render & Delivery**:
  - Client repositioned IRWIN and VISE-GRIP overlay logos to the bottom frame.
  - All 3 videos burned with Viral Pop locked styling (Prompt Bold 104, Outline 3, Shadow 1, pos (540, 1580), Margins 47/47/158, yellow highlight `\c&H00F0FF&`).
  - Output files (`10CR_sub.mp4`, `10WR_sub.mp4`, `10R_sub.mp4`) uploaded to Google Drive.

**V6 generalized pipeline: PASS (metal clip, 2026-09-17)**

V5 remains the tes2 golden output (untouched). V6 (`render/build_clip.py` +
`render/align.py`, same DP timing math, no tes2 hardcodes) is proven on a
second real video: 16.18s / 1080×1920 / HEVC+AAC, 11 cues, all 3–5 spoken
syllables, match ratio 1.0, QA passed on 6 rendered frames.

## V6 Proven Working

- Input: Metal Card Holder clip (16.18s, 1080×1920, HEVC+AAC — not tes2)
- Transcript: Groq `whisper-large-v3-turbo`, `verbose_json` +
  `timestamp_granularities=["word","segment"]` (6 segments, 102 sub-word
  timed tokens; words[] gate enforced — segments-only responses FAIL)
- Build: `render/build_clip.py --config jobs/metal_card_holder.json`
  (writes `output_metal/metal.ass` + `output_metal/metal_transcript.json`)
- Cue grouping: 3–5 spoken syllables (`count_syllables`: loanword whitelist
  overrides, digits by Thai pronunciation); tail rebalance, no orphans
- Normalization: `loanword_dict.json` variants→canonical on both STT source
  (timestamp-preserving) and corrected phrases (e.g. `Methan Guard Holder`
  → `เมทัลการ์ดโฮลเดอร์`, `สแตร์เล็ด` → `สแตนเลส`)
- Geometry: PlayRes/fontsize/margins/pos auto-derived from the video
  (104pt / 47px margins at 1080×1920); worst cue 862.7/926.8px, 0 forced splits
- Render: `render_subtitles.py` (default `subtitles=` filter) → 16.18s
  h264+aac MP4, 6 QA frames spot-checked (timing, color, clipping, position)

## A/B `subtitles=` vs `ass=` (ffmpeg 9.0.1, 2026-09-17)

- 7-cue test (`output_metal/ab/`): single-mark control, 8 double-mark pairs,
  loanword `โฮลเดอร์`, animated pop tags, yellow keyword over a stacked
  cluster, real Layer-0+1 overlay pair — rendered 4 ways (fix ON/OFF × filters)
- Result: filters are **pixel-identical** (diff 0.00) with and without the fix;
  `ass=` gives no benefit on this build. Default stays `subtitles=` +
  `thai_mark_fix.py`. `--filter` flag kept for future builds.
- mark-fix OFF is visibly broken; ON is cleanly separated — layer stays required.
  (SUPERSEDED 2026-09-24: C6 30-frame QA + isolated render tests proved the
  overlay layer itself caused tofu/float boxes on the CURRENT ffmpeg+libass
  build; plain unsplit text shapes correctly. Layer retired,
  ENABLE_OVERLAYS=False. Old evidence kept above for audit.)
- Fixed during review: overlay keyword color-loss (corrected mark inside a
  yellow keyword rendered white) — each overlay header now carries its mark's
  active color; `metal.ass` rebuild is byte-identical.

## Known Issues → RESOLVED 2026-09-17 (post-process in build_clip.py)

Owner eyes-on review found overlapping cues + residual desync. Root cause
verified from raw STT data (not guessed): Groq **back-dates segment-first
words** — 2 segments show first-word exactly 500ms before the segment's own
start (cut1 seg3, cut2 seg4; normal band everywhere else is -100..+240ms).
Correction to the first theory: it is not one straddling word, it is
non-monotonic word timestamps from STT itself (one true backward START:
cut1 `บ` 8.18 → `แล` 7.86; the rest is end/start jitter).

Fix (post_process, AFTER align, BEFORE ASS so Layer-1 overlays follow
automatically — cue times stay ASR-derived, never redistributed):
- SEG_SNAP (>0.30s, contested only): rigid-shift the segment-first cue onto
  seg.start (start AND end, durations preserved), flagged REVIEW.
- GAP_WITNESS (>=0.25s preceding silence): skips the snap — a segmenter can
  place a boundary late, but sound starts when it starts
  (`timing_source=asr_word_anchored`). Decided cut2 seg4 (0.52s silence).
- OVERLAP: trim prev.end to next.start - 1cs (onsets untouched); cascade-push
  next.start forward when trimming would invert, but push > 0.10s is a
  SystemExit (CASCADE-REFUSED) — never silently wreck a healthy neighbour.
- RATE_GUARD (>8 syl/s): flag only, times untouched. `--strict-overlap`
  fails the build on any REVIEW flag. Every cue carries review_reason.
- Results: metal 2 trims (60/20ms, no review), cut1 1 snap + 1 trim,
  cut2 0 snap + 0 cascade + 2 trims + 1 anchored REVIEW. ASS-level assert
  next.start >= prev.end passes on all 3 clips. Test suite: 41 tests OK.
- Results: metal 2 trims (60/20ms, no review), cut1 1 snap + 1 trim,
  cut2 1 snap + 1 cascade + 2 trims. ASS-level assert next.start >= prev.end
  passes on all 3 clips. Owner re-checks flagged frames only
  (cut1 ~8.1s, cut2 ~7.5/8.3s) instead of the whole video.

## Residual Minor Issues (owner-reported 2026-09-17, needs Grok follow-up)

Overall ~95%, metal perfect. Two spots still slightly off (owner eyes, exact
symptom descriptions pending — do NOT guess the symptom, ask or measure):

1. **cut1 ~10.8–11.7s, cue `ยิ่งวันไหนรีบๆ` (seg4 cue 0, 10.84–11.74, r=1.0)**
   - Text fixed this round (`hoy`→`ยิ่ง`, verified on frame).
   - Timing untouched by post-process (seg4 first-word diff +0ms, no snap,
     no trim; words look normal). If the issue is sync (not text), suspects:
     word-level jitter inside the cue, or preceding cue tail. Check
     `output_cut1/cut1_transcript.json` seg 4 word spans vs the cue window.
2. **cut2 ~7.2–7.8s, cue `ตัวกระเป๋าน้ำหนัก` (seg4 cue 0, 7.24–7.68, r=1.0)**
   - Unsnapped by gap-witness rule (0.52s preceding silence, onset kept 7.24,
     `timing_source=asr_word_anchored` + REVIEW for 11.4 syl/s interior).
   - Known-bad interior: 14 leading tokens on quantized 20–40ms runs.
     If onset still feels off, next steps: faster-whisper second witness on
     7.0–8.5s, or owner A/B (snap vs no-snap) — do NOT silently re-tune.

For Grok: both cues have match ratio 1.0 and full metadata in
`output_cut1/cut1_transcript.json` / `output_cut2/cut2_transcript.json`
(`timing_source`, `review_reason`, `speech_rate_syl_per_sec`). Start there,
then ask the owner for the precise symptom before changing timing code.

## V5 Proven Working (golden, untouched)

## V5 Proven Working

- Input: `C:\Users\Administrator\Downloads\เทส2.MP4`
- Output: `output\เทส2_animated_subtitles_v5.mp4`
- Build: `render\build_v5.py` (reads `output\tes2_transcript_v2.json`, writes
  `output\tes2_v5.ass` + `output\tes2_v5_transcript.json`)
- Alignment level: **MICRO-PHRASE**, same method as V4 (see below) — V5 only changes cue
  *segmentation* (splitting overlong phrases) and *rendering* (mark-shaping compensation),
  never the underlying timing method.
- Timing source: Whisper token/character timestamps mapped to corrected transcript spans,
  via `build_v4.py`'s unmodified `align_maps`/phrase-span DP alignment (imported by
  `build_v5.py`, not reimplemented)
- **No weighted/proportional timing**
- **No VAD/silence/audio-energy fake word timing**
- 1 line per cue, no `\N`
- Prompt Bold font used (unmodified, no substitution)
- Keyword emphasis by color, same font size as normal text
- Subtle cue-level pop animation
- Every cue's rendered width measured to fit the 464px frame (see "Overflow fix" below)
- Every Thai double-combining-mark cluster corrected (see "Typography fix" below)
- Video + audio preserved
- V5 duration: 27.236009s (identical to V4 — timing untouched)
- V5 resolution: 464x848
- V5 cue count: 37 (up from V4's 20 — 10 of 13 authored phrase groups were split for width; see below)

## History

### V3 → V4: timing fix

V3 used segment timing and then distributed each segment's duration across multiple
phrases by text length — approximate timing, not audio-grounded.

V4 derives micro-phrase start/end from the Whisper timestamped token stream and maps the
corrected Thai text monotonically onto that timed source. Confirmed **Sync 100%** by the
project owner. `output\tes2_v4.ass` / `output\tes2_v4_transcript.json` /
`output\เทส2_animated_subtitles_v4.mp4` are kept as-is, untouched, as the timing reference.

### V4 → V5, fix 1: Thai combining-mark shaping ("วรรณยุกต์ลอย/ทับ")

**Root cause** (full evidence: `output/THAI_SHAPING_ROOT_CAUSE.md`): libass (this ffmpeg
build) does not trigger Prompt-Bold's chaining-contextual GSUB substitution (`ccmp`) that
selects the correctly-sized/positioned combining-mark glyph when a Thai tone/other mark
stacks on top of an upper vowel (e.g. `กี่`, `นี้`, `ที่`, `หนึ่ง`). Confirmed via:
standalone HarfBuzz shaping of the same font is correct; the font's own glyph metrics are
correct; the same pipeline renders the same words correctly with a different font
(Tahoma); single-mark Thai clusters render correctly in Prompt Bold through this same
pipeline. Not a font defect, not a timing/animation defect. Affected 12 of V4's 20 cues.

**Fix**: `render/thai_mark_fix.py` — a shaping/preprocessing compensation layer that hides
the broken inline mark and redraws it via a position-corrected overlay event, using a
font-metrics-derived pixel offset (not a guess). No font substitution, no ASS/FFmpeg
pipeline rewrite.

### V4 → V5, fix 2: cue overflow ("ตกขอบ")

After the typography fix was confirmed in sync, the project owner flagged a second,
separate bug: several V4 phrases are too wide for the 464px frame at fontsize 46 (no
`\N`, `WrapStyle: 2` = no auto-wrap), so long/fast-spoken cues ran off both edges.
Measured with real font shaping (uharfbuzz): **10 of V4's 13 authored phrase groups
overflowed the safe width**, some by up to ~74% (e.g. "ผมเลือกเป็นตัวอย่างจากนั้นกดปุ่มแชร์"
measured 739px against a 424px budget).

**Fix**: `render/thai_line_split.py` — a width-aware phrase splitter. Uses `pythainlp`
word-tokenization so a cut only ever falls between real Thai words (a character-class-only
heuristic was tried first and produced broken mid-word cuts, e.g. splitting "ถึง"/"ลง" in
half — replaced for that reason). Every resulting sub-cue's start/end still comes from
real Whisper per-character timestamps via V4's own unmodified alignment code — no
invented/weighted/proportional timing was introduced by splitting.

All 37 final V5 cues measured under the width budget (max 406.6px / 424px budget,
including the animation's 103% pop peak).

## Do Not Regress

- Do not revert to V3 weighted timing.
- Do not call token/character timing WORD-LEVEL timing.
- Do not use system-font substitution.
- Do not enlarge keywords.
- Do not remove the Thai mark-shaping compensation layer (`thai_mark_fix.py`) or the
  width-aware split (`thai_line_split.py`) without re-verifying both bugs stay fixed.
- 3–5 words was V4's per-cue target; V5 cues split for width can be shorter (1–3 words) —
  this is intentional (project owner asked for fast flicker over strict word count), not a
  regression.

## Key Files

- `SKILL.md`
- `render\render_subtitles.py` — unmodified since V3; executes ASS + FFmpeg, verifies output
- `render\build_v4.py` — unmodified; golden V4 timing/alignment, kept as the timing reference
- `render\build_v5.py` — current build script; imports build_v4.py's alignment, adds width-aware splitting + mark-shaping fix
- `render\thai_mark_fix.py` — Thai combining-mark shaping compensation layer
- `render\thai_line_split.py` — width-aware, word-boundary-safe phrase splitter
- `output\tes2_v5.ass`, `output\tes2_v5_transcript.json`, `output\เทส2_animated_subtitles_v5.mp4` — current golden output
- `output\tes2_v4.ass`, `output\tes2_v4_transcript.json`, `output\เทส2_animated_subtitles_v4.mp4` — kept as the V4 timing reference (untouched)
- `output\THAI_SHAPING_ROOT_CAUSE.md` — full root-cause evidence for the mark-shaping bug
- `fonts\Prompt-Bold.ttf`
