#!/usr/bin/env python3
r"""Compensation layer for a confirmed libass Thai double-mark shaping bug.

ROOT CAUSE (see output/THAI_SHAPING_ROOT_CAUSE.md for full evidence):
libass, when it shapes a Thai cluster consisting of BASE + UPPER_VOWEL + TONE/OTHER
mark, fails to trigger the font's chaining-contextual GSUB substitution (LookupType 6,
'ccmp' feature) that swaps in the smaller, correctly-raised mark-glyph variant
(e.g. uni0E48.small) needed when a tone mark stacks on top of an upper vowel. libass
instead draws the default-size glyph at the same vertical slot as the vowel, so the two
marks visually collide.

This was verified directly against Prompt-Bold.ttf:
- A standalone HarfBuzz shape() call on the same font selects the correct
  ".small"/".narrow" glyph variants and positions them correctly (proven with uharfbuzz).
- The font's own glyf table shows the substituted small variant is drawn safely above
  the vowel's bounding box (see measurements below) -- i.e. the font is correct.
- The exact ffmpeg+libass+fontsdir pipeline used by this project reproduces the
  collision at both debug scale and real V4 production scale (46pt / 464x848), for both
  static and `\t(...)\fscx\fscy` animated text -- ruling out the animation as the cause.
- The SAME pipeline renders the identical double-mark text correctly with a different
  Thai font (Tahoma) -- ruling out a general "libass has no harfbuzz" explanation.
- Isolating the cluster into its own shaping run (a `{\r}` boundary) does not change the
  outcome -- ruling out run-splitting/style-boundary interference as the cause.

Font is NOT modified. Prompt-Bold.ttf is unchanged and still required/used.

2026-09-24 RETIREMENT (C6 QA evidence, Lip-approved plan):
Frame-level QA of 30 burned C6 cues + isolated render tests proved the
overlay layer itself CAUSES the defects it was built to fix:
- An override tag `{...}` splits libass shaping runs, so the overlay's
  "single" mark is shaped with NO base consonant in its run. Prompt Bold
  renders detached MAI EK (U+0E48) as a floating square ("ที่" tofu,
  f02/f28-29) and detached+raised MAI THO (U+0E49) as a floating tone
  ("น้ำ/นี้", f05-06/f11).
- Plain unsplit Prompt Bold text renders stacked marks CORRECTLY in the
  current ffmpeg+libass build (verified: ที่/น้ำ/นี้/ท่/น่า all correct,
  no tofu) — the old "collision" no longer reproduces.
So `build_fixed_cue` is now a NO-OP returning `(colored_text, [])`:
no hidden-mark tags in the main line (runs stay intact), no Layer1
overlays. Detection (`find_stacked_clusters`) and `clearance_px` are
kept for QA reporting. Do NOT re-enable overlays without re-verifying
on real burned frames first.
FIX STRATEGY (no font substitution, no timing change, no random cue repositioning):
For each BASE+VOWEL+MARK cluster in a cue's plain text:
  1. Hide the broken inline mark glyph in the main flowed line (`\alpha&HFF&` /
     `\alpha&H00&` around just that character -- the glyph has zero advance width, so
     hiding it does not shift any other glyph's position or break flow/kerning).
  2. Emit a second ("overlay") Dialogue event, on a higher layer, with the SAME cue
     text but with the upper-vowel-mark character removed (also zero advance width, so
     removing it does not change the shaped line's total width or its `\an2` centering)
     and every character made transparent except the target mark. Because the target
     mark is no longer preceded by a vowel mark in this shaping run, libass falls back to
     plain mark-to-base positioning -- which this project's own tests confirm libass
     renders correctly for single-mark Thai clusters (e.g. "เก่ง", "เป็น").
     For TONE+SARA_AM clusters (e.g. "น้ำ"): the overlay instead replaces SARA_AM
     (U+0E33) with SARA_AA (U+0E32) — verified byte-width-identical (109.72px both
     at 104pt) so centering is unchanged — leaving the tone as a single-mark on
     the base, likewise proven correct, then raised by the same clearance.
  3. The overlay's `\pos` Y is shifted up from the main cue's `\pos` Y by a constant,
     font-metrics-derived pixel offset (NOT a guess): the measured difference between
     where the default-size mark glyph sits and where the font's own substituted small
     variant (plus its own mkmk anchor delta) is drawn, scaled by fontsize/unitsPerEm.
     X is untouched, since it is already correct (both marks share the base's cluster
     with zero x_offset).
  4. The overlay shares the exact same `\pos`/`\fscx`/`\fscy`/`\t(...)` animation tags as
     the main cue (only `\pos` Y differs), so the corrected mark scales/pops in lockstep
     with the rest of the animated cue. It also carries the keyword `{\c...}` color
     active at the mark in the main line (header placement, so the shaping run is
     untouched) — otherwise a corrected mark inside a yellow keyword would render white.

Font metrics used (Prompt-Bold.ttf, unitsPerEm=1000, measured via fontTools):
  uni0E35 (SARA II)        glyf bbox yMin=595
  uni0E48 (MAI EK, default) glyf bbox yMin=595   <- collides with the vowel above
  uni0E48.small (substituted) glyf bbox yMin=840, plus a further +25-unit mkmk nudge
  => a ~270 font-unit upward correction clears the default glyph of the vowel glyph.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Layer retired 2026-09-24 (see module docstring): plain unsplit text
#: shapes stacked marks correctly in the current ffmpeg+libass build,
#: while overlay isolation causes tofu/float. Flag-gated (not deleted)
#: so the mechanism survives for audit; re-enabling requires frame proof.
#: When False, `build_fixed_cue` returns `(colored_text, [])` — no
#: hidden-mark tags, no Layer1 overlays.
ENABLE_OVERLAYS = False

# Upper-position Thai vowels that a tone/other mark can stack on top of.
UPPER_VOWELS = set("ัิีึืํ")
# Marks that stack a second time above an upper vowel (tone marks + maitaikhu/thanthakhat).
STACK_MARKS = set("็่้๊๋์")
# SARA AM (U+0E33, visual "ำ") canonically decomposes via GSUB MultipleSubst
# to NIKHAHIT (U+0E4D, upper circle) + SARA AA (U+0E32). When a tone precedes
# it (e.g. "น้ำ" = N + MAI THO + SARA AM), HarfBuzz shapes NIKHAHIT +
# tone.small (+55 y-offset) — i.e. the same double-mark stacking as
# UPPER+TONE, requiring the same .small variant libass fails to select.
# The detector therefore treats TONE+AM (and AM+TONE, defensive) as stacked.
SARA_AM = "ำ"
SARA_AA = "า"

# Measured on fonts/Prompt-Bold.ttf, unitsPerEm=1000 (see docstring above).
# Conservative constant clearance shared by all vowel+mark pairs in this font.
CLEARANCE_FONT_UNITS = 270
FONT_UNITS_PER_EM = 1000

_TAG_RE = re.compile(r"\{[^}]*\}")
_COLOR_RE = re.compile(r"\\c&H[0-9A-Fa-f]+&")
# Style default PrimaryColour (white). Overlays always carry an explicit
# color so a corrected mark never inherits an unintended one.
DEFAULT_COLOR_INNER = r"\c&HFFFFFF&"


@dataclass
class StackedCluster:
    vowel_index: int  # index of the upper vowel char in the plain text
    mark_index: int    # index of the stacking mark char in the plain text (vowel_index+1)
    kind: str = "vowel_stack"  # "vowel_stack" (UPPER+TONE) or "tone_am" (TONE+SARA_AM)


def find_stacked_clusters(plain_text: str) -> list[StackedCluster]:
    """Locate sequences that trigger the libass shaping bug.

    - "vowel_stack": BASE + UPPER_VOWEL + STACK_MARK (e.g. ที่, นี้, กี่).
    - "tone_am": TONE + SARA_AM or SARA_AM + TONE (e.g. น้ำ, ค้ำ, ซ้ำ).
      After GSUB decomposition AM -> NIKHAHIT + AA this is the same
      NIKHAHIT + TONE stacking (HarfBuzz selects tone.small, libass does
      not). Generic: any STACK_MARK in STACK_MARKS adjacent to SARA_AM.
    """
    clusters = []
    for i in range(len(plain_text) - 1):
        a, b = plain_text[i], plain_text[i + 1]
        if a in UPPER_VOWELS and b in STACK_MARKS:
            clusters.append(StackedCluster(vowel_index=i, mark_index=i + 1, kind="vowel_stack"))
        elif (a in STACK_MARKS and b == SARA_AM) or (a == SARA_AM and b in STACK_MARKS):
            # mark is the tone side; vowel side is the AM side.
            if a in STACK_MARKS:
                clusters.append(StackedCluster(vowel_index=i + 1, mark_index=i, kind="tone_am"))
            else:
                clusters.append(StackedCluster(vowel_index=i, mark_index=i + 1, kind="tone_am"))
    return clusters


def clearance_px(fontsize: float) -> float:
    return CLEARANCE_FONT_UNITS / FONT_UNITS_PER_EM * fontsize


def active_color_inner(colored_text: str, char_index: int) -> str:
    """The `\\c` color override in effect at a character position of the
    keyword-colored cue text (e.g. `\\c&H00F0FF&` inside a yellow keyword),
    or the style-default white when no color run covers it.

    Returned without braces so callers can place it inside an event header
    block. Header placement (not inline before the glyph) keeps the overlay
    shaping run identical to the proven layout — only its color changes.
    """
    color_inner = DEFAULT_COLOR_INNER
    for m in _TAG_RE.finditer(colored_text):
        if m.start() >= char_index:
            break
        cm = _COLOR_RE.search(m.group(0))
        if cm:
            color_inner = cm.group(0)
    return color_inner


def build_fixed_cue(
    plain_text: str,
    colored_text: str,
    pos_x: float,
    pos_y: float,
    fontsize: float,
    an: str,
    scale_tags: str,
) -> tuple[str, list[str]]:
    """Given a cue's plain text (for cluster detection) and its already
    keyword-colored ASS text (identical character sequence, just with extra
    `{\\c...}` runs woven in), return:
      - the main-line text with each broken mark hidden in place
      - a list of extra ASS Text payloads (without Dialogue prefix/timing) for
        overlay events that draw the corrected marks, to be emitted on a
        higher Layer with the same timing as the main cue.

    `an`/`scale_tags` are the alignment (`\\an2`) and scale/animation tags
    (`\\fscx..\\fscy..\\t(...)`) used by the main cue's `{...}` header, reused
    verbatim on the overlay so the correction tracks the pop animation.

    RETIRED 2026-09-24 (`ENABLE_OVERLAYS = False`): returns
    `(colored_text, [])` — plain text shapes correctly, overlays cause
    tofu/float (see module docstring).
    """
    if not ENABLE_OVERLAYS:
        return colored_text, []
    clusters = find_stacked_clusters(plain_text)
    if not clusters:
        return colored_text, []
    # Process in mark order so the main-line hide spans stay sequential
    # (tone_am clusters store mark before the AM vowel).
    clusters = sorted(clusters, key=lambda c: c.mark_index)

    # Map plain-text index -> index in colored_text. colored_text is plain_text
    # with extra {\c...} runs inserted at word boundaries, so walk both in
    # lockstep, skipping override blocks in colored_text.
    plain_to_colored: dict[int, int] = {}
    ci = 0
    pi = 0
    while pi < len(plain_text):
        while ci < len(colored_text) and colored_text[ci] == "{":
            end = colored_text.index("}", ci) + 1
            ci = end
        if ci < len(colored_text) and colored_text[ci] == plain_text[pi]:
            plain_to_colored[pi] = ci
            ci += 1
            pi += 1
        else:
            raise ValueError("plain/colored text desynced during mark-fix mapping")

    main_out = []
    last = 0
    overlays: list[str] = []
    for cluster in clusters:
        mark_ci = plain_to_colored[cluster.mark_index]
        main_out.append(colored_text[last:mark_ci])
        main_out.append(r"{\alpha&HFF&}" + colored_text[mark_ci] + r"{\alpha&H00&}")
        last = mark_ci + 1

        if cluster.kind == "tone_am":
            # AM (U+0E33) -> AA (U+0E32): same advance width (verified
            # 109.72px for น้ำ vs น้า at 104pt), so line width/centering
            # is unchanged. The tone becomes a single-mark on the base,
            # which libass shapes correctly, then raised like other marks.
            am_pos = cluster.vowel_index
            overlay_plain = (
                plain_text[:am_pos] + SARA_AA + plain_text[am_pos + 1:]
            )
            target_index_in_overlay = cluster.mark_index  # length preserved
        else:
            overlay_plain = plain_text[: cluster.vowel_index] + plain_text[cluster.vowel_index + 1:]
            target_index_in_overlay = cluster.mark_index - 1  # vowel removed, shifts left by 1
        overlay_chars = []
        for idx, ch in enumerate(overlay_plain):
            if idx == target_index_in_overlay:
                overlay_chars.append(ch)
            else:
                overlay_chars.append(r"{\alpha&HFF&}" + ch + r"{\alpha&H00&}")
        overlay_y = pos_y - clearance_px(fontsize)
        # The corrected mark must keep the keyword color it has in the main
        # line (a mark inside a yellow keyword used to come out white).
        # One overlay per cluster, so each header carries its own mark color.
        # Default-white marks emit no extra tag, keeping approved outputs
        # byte-identical.
        color_inner = active_color_inner(colored_text, mark_ci)
        color_suffix = "" if color_inner == DEFAULT_COLOR_INNER else color_inner
        overlay_tag = rf"{{{an}\pos({pos_x:g},{overlay_y:g}){scale_tags}{color_suffix}}}"
        overlays.append(overlay_tag + "".join(overlay_chars))
    main_out.append(colored_text[last:])
    return "".join(main_out), overlays
