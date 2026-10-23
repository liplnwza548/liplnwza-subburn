# Thai Combining-Mark Shaping Bug — Root Cause & Fix

## Environment (verified, not assumed)

- ffmpeg `N-124445-g22d06b39ce-20260513` at `C:\Users\Administrator\ffmpeg\bin\ffmpeg.exe`,
  built with `--enable-libass --enable-libharfbuzz --enable-libfreetype --enable-libfribidi --enable-fontconfig`.
- `render_subtitles.py` invokes `subtitles=<ass>:fontsdir=<fonts/>` — the only font libass
  can see is `fonts/Prompt-Bold.ttf` (family `Prompt`, subfamily `Bold`, confirmed via the
  font's own `name` table).
- Python `fontTools` 4.62.1 and `uharfbuzz` 0.56.1 were installed in this session to inspect
  the font and shape text independently of libass/ffmpeg.

## Font audit (fonts/Prompt-Bold.ttf)

`GSUB`/`GPOS` both declare a `thai` script (in addition to `DFLT`/`latn`):

- `GSUB` thai features: `aalt, ccmp, frac, liga, ordn, subs, sups` — `ccmp` carries chaining
  **contextual substitution** lookups (LookupType 6) that swap in alternate combining-mark
  glyphs, e.g. `uni0E48` (MAI EK) → `uni0E48.small`, `uni0E49` → `uni0E49.small`, and
  base-triggered `uni0E47` → `uni0E47.narrow`, `uni0E4C` → `uni0E4C.narrow`.
- `GPOS` thai features: `kern, mark, mkmk` — real mark-to-base and mark-to-mark anchor tables
  are present.
- None of the `.small`/`.narrow` glyphs have a cmap entry — they are reachable **only**
  through the GSUB chaining-contextual lookups above (confirmed with fontTools).
- Glyph geometry (unitsPerEm = 1000): `uni0E35` (SARA II) bbox yMin=595; default `uni0E48`
  bbox yMin=595 (same slot as the vowel — a collision if both are drawn here); the
  substituted `uni0E48.small` bbox yMin=840, plus a further measured `mkmk` offset of
  +25 units when shaped standalone with HarfBuzz. The font is correctly engineered for
  Thai; the substituted variant is drawn safely clear of the vowel.

**Conclusion: the font is not the defect.**

## Independent HarfBuzz shaping (ground truth)

Shaping the same font directly with `uharfbuzz` (bypassing libass/ffmpeg entirely) for
`กี่`, `นี้`, `ที่`, `เก่ง`, `เป็น`, etc. correctly selects the `.small`/`.narrow`
glyph variants and positions them with no collision. This proves a fully-featured
HarfBuzz shape() call on this exact font file produces correct output.

## Reproduction through the real pipeline (ffmpeg + libass + fontsdir)

Rendered via the *same* `subtitles=...:fontsdir=...` filter `render_subtitles.py` uses,
at both a large debug scale (1400x700, 220pt) and the real V4 production scale
(464x848, 46pt), both static and with V4's exact `\pos\fscx\fscy\t(...)` pop animation:

- Single-mark Thai clusters (mark directly on a consonant, e.g. `เก่ง`, `เป็น`) render
  **correctly** — confirms libass *does* apply basic GPOS `mark` (mark-to-base) anchoring
  for this font.
- Double-mark clusters (an upper vowel followed immediately by a tone/other mark, e.g.
  `กี่`, `นี้`, `ที่`, `หนึ่ง`) render **broken**: the default-size mark glyph is used and
  drawn in the same vertical slot as the vowel, producing the reported
  "overlapping/floating" look — at *both* scales, static and animated identically
  (ruling out the pop animation as the cause).
- The identical text through the identical pipeline, using a different Thai-capable font
  (`Tahoma`, from `C:\Windows\Fonts`), renders the *same* double-mark words correctly —
  ruling out a pipeline-wide problem (missing HarfBuzz, PlayRes, fontconfig, etc.).
- Isolating the cluster into its own shaping run (`{\r}` boundary before/after it) does
  not change the outcome — ruling out ASS-run/style-boundary interference.

**Conclusion: libass, in this build, does not trigger Prompt-Bold's chaining-contextual
GSUB substitution (`ccmp`, LookupType 6) that selects the correctly-sized/positioned
combining-mark glyph when a Thai tone/other mark stacks on top of an upper vowel. This is
a libass Thai complex-shaping limitation for this specific glyph-substitution scenario —
not a font defect, not a V4 timing/animation defect, and not a general "no HarfBuzz"
problem (single-mark clusters and the base-triggered narrow-variant substitution both work).**

This pattern affected **12 of the 20 real cues** in `output/tes2_v4_transcript.json`
(checked programmatically) — this is a high-impact, frequent defect, not a rare edge case.

## Fix implemented (no font change, no timing change)

Text-level substitution is impossible (the corrected glyphs have no Unicode/cmap access).
A shaping/preprocessing compensation layer was added instead — see
`render/thai_mark_fix.py` (library) and `render/build_typofix_test.py` (candidate ASS
builder, reads the existing, unmodified `tes2_v4_transcript.json`):

1. Detect every `BASE + UPPER_VOWEL + STACK_MARK` cluster in a cue's plain text.
2. In the normal flowed line, hide just the broken mark glyph (`\alpha&HFF&`/`\alpha&H00&`
   around that one character — zero advance width, so nothing else shifts).
3. Emit a second ("overlay") `Dialogue` event, same timing, one Layer higher, with the
   vowel character removed (also zero advance width, so the shaped line's total width and
   `\an2` centering are unchanged) and everything but the target mark made transparent.
   With no vowel mark preceding it, libass shapes the mark as a plain single-mark case —
   proven correct above.
4. The overlay's `\pos` Y is raised from the main cue's `\pos` Y by a constant,
   font-metrics-derived pixel offset: `270 font units / 1000 upem * fontsize` (270 =
   measured clearance between the default glyph's slot and the font's own substituted
   variant + its mkmk anchor). X is untouched (already correct). The overlay carries the
   exact same `\fscx\fscy\t(...)` tags as the main cue, so the correction pops in lockstep
   with the animation.

## Verification performed

- `render/build_typofix_test.py` → `output/tes2_v4_typofix.ass` (12/20 cues fixed),
  rendered end-to-end through the **unmodified** `render_subtitles.py` against the real
  source video → `output/เทส2_animated_subtitles_v4_typofix.mp4`.
- Duration: `27.236009s`, identical to the golden V4 output — timing untouched (same
  source JSON, no timing fields read or written by the fix).
- ffprobe confirms both video (h264) and audio (aac) streams present.
- Visual QA on real footage, before vs. after, at multiple timestamps
  (`output/cmp_0.6.png`, `cmp_3.1.png`, `cmp_11.0.png`): tone marks that were previously
  merged/invisible on top of vowels are now clearly separated, matching correct Thai
  typography (cross-checked against the Tahoma control).
- Checked mid-animation (t=6.52s, during the `\t(0,120,...)` scale-up ramp): the corrected
  marks track the pop animation with no visible seam/lag.
- Regression check: an unaffected cue (no double-mark cluster, e.g. "เป็นอันเสร็จเรียบร้อยนะ")
  diffed pixel-by-pixel between the V4 output and the typofix output — mean channel diff
  1.05/255, differences spread uniformly across the whole frame including the background
  video (not concentrated on text) — consistent with normal H.264 re-encode noise, not a
  content regression. Keyword coloring and the pop animation are visibly intact in every
  QA frame.

## Status

Fix is implemented and verified against the real video and the real, golden V4 timing.
`tes2_v4.ass` / `tes2_v4_transcript.json` / `เทส2_animated_subtitles_v4.mp4` were not
modified. Per instructions, the candidate output has **not** been named/promoted to V5 —
that naming decision is left to the project owner once they've reviewed
`output/เทส2_animated_subtitles_v4_typofix.mp4`.

### Addendum (2026-09-04): a simpler fix exists — `ass=` filter instead of `subtitles=`

While cross-checking a sibling project (`phoenix-cut`) that hit the *same* libass Thai
mark-stacking bug independently and earlier (2026-08-27), its renderer's code contained a
verified comment: switching the ffmpeg filter from `subtitles=<file>` to `ass=<file>` (same
libass, same font, same everything else) fixes the mark-stacking for every font they tried
on real footage — not just one.

Re-tested here directly against this project's own repro case (`กี่`, `นี้`, `ทั้ง`,
Prompt-Bold.ttf, both on a flat color background and on the real `เทส2.MP4` footage):
**confirmed.** `ass=test.ass:fontsdir=...` renders the double-mark clusters correctly;
`subtitles=test.ass:fontsdir=...` (what `render_subtitles.py` and V5 use) reproduces the
exact same broken/merged mark this document describes.

Likely mechanism (not fully verified against libavfilter source, but consistent with known
behavior): `subtitles=` routes the file through FFmpeg's own subtitle demux/decode layer
before handing events to libass, while `ass=` reads the .ass file directly via libass's own
file API, bypassing that extra layer. Something in that extra layer likely perturbs the
text/Unicode sequence libass's HarfBuzz shaper receives, in a way that breaks the
chaining-contextual GSUB lookup this document identifies as the root cause.

**This means the `thai_mark_fix.py` overlay-compensation layer built for V5 was not the
minimal fix** — a one-word filter change (`subtitles=` → `ass=`) appears to fix the same
bug more simply, for more fonts, with no extra Dialogue events. V5 was not rebuilt on this
finding (it already shipped and was approved); this is recorded here so a future session
does not have to rediscover it, and so a simpler V6 pass can consider verifying `ass=` end
to end (including animation tags and `fontsdir` behavior) instead of extending the overlay
approach further.

### Known limitation

The fix was validated against every double-mark combination that actually occurs in this
video's real transcript (5 distinct vowel+mark pairs, listed below). The detection rule
(`UPPER_VOWELS × STACK_MARKS` in `render/thai_mark_fix.py`) is general and should cover
other Thai videos using the same font, but combinations that don't occur in this
transcript were not individually eyeballed on real footage — only reasoned about from the
font's measured metrics. Pairs actually verified on real footage: `ิ+่`, `ึ+่`, `ั+้`,
`ี+่`, `ื+่`, `ี+๋`, `ึ+้`, `ี+้`.

### Addendum (2026-09-17): A/B `subtitles=` vs `ass=` on ffmpeg 9.0.1 — `ass=` does NOT fix it here

Tested on this machine (ffmpeg 9.0.1-full_build, metal clip 1080x1920, Prompt-Bold 104pt)
with `output_metal/ab/ab_build.py`: 7 cues (single-mark control, 8 double-mark pairs,
loanword `โฮลเดอร์`, animated pop tags, yellow keyword over a stacked cluster, and a
real Layer-0+1 overlay pair from `metal.ass`), rendered 4 ways
(mark-fix ON/OFF × `subtitles=`/`ass=` with explicit `original_size=1080x1920`).

- `subtitles=` vs `ass=` frames are **pixel-identical** (mean diff 0.00, max 0) at all
  7 cue timestamps, with AND without the fix. The Addendum-2026-09-04 "confirmed"
  result does not reproduce on this build — the fix-through-`ass=` effect appears
  build-specific (observed on `N-124445`, not on 9.0.1), or specific to conditions
  not present here.
- mark-fix OFF is visibly broken (merged/floating tone marks); mark-fix ON is
  clearly separated. The compensation layer stays **required** on this build.
  (SUPERSEDED 2026-09-24 per evidence above being build-specific: C6 QA on the
  current build proved the overlay CAUSES tofu/float; layer retired,
  ENABLE_OVERLAYS=False. Kept for audit — do not re-enable without frame proof.)
- Decision: default render path stays `subtitles=` + `thai_mark_fix.py`.
  `render_subtitles.py --filter ass|subtitles` is kept for future builds/machines.
- Latent bug found during the review (not yet triggered on real footage): overlay
  events from `build_fixed_cue()` did not propagate keyword `{\c...}` color, so a
  corrected mark inside a yellow keyword rendered white. FIXED 2026-09-17:
  each overlay header now carries the `{\c...}` color active at its mark
  (`active_color_inner()` in `thai_mark_fix.py`; header placement leaves the
  shaping run untouched, default-white marks emit no extra tag). Verified on
  A/B cue C6 (`ซื้อ` yellow): before = white floating mark, after = yellow
  separated mark; `metal.ass` rebuild is byte-identical (no cue hits both).
