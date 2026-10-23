#!/usr/bin/env python3
"""Ticket 02: build Viral Pop ASS from aligned script lines.

Single entrypoint:

    build_ass(alignment, out_path=None) -> str (ASS text)

- ``alignment``: full ``{"matched": [...], "mismatches": [...]}`` dict
  from ``script_align.align_script_to_timeline`` (preferred — the
  ticket-03 gate runs first and REFUSES to write when mismatches are
  non-empty), or a bare ``matched`` list for tests.
- Each Dialogue's on-screen text EQUALS the script line exactly: one
  Dialogue per matched entry, no moving words across script-line
  boundaries, no mid-word split of Thai (no re-wrapping at all — one
  cue = one Dialogue, ``WrapStyle: 2`` so libass never auto-wraps a
  second line, never ``\\N``).
- STT / loanword / ASR-rewrite helpers are NEVER applied to cue text:
  this module never rewrites wording (ASS-escaped only). Width
  MEASUREMENT/split helpers (``script_width`` / ``thai_line_split`` via
  it) are allowed: they only measure pixels and choose cut points.
  Keyword color (``script_keywords``) only weaves ``{\\c...}`` runs
  around script substrings; mark overlays (``script_marks`` wrapping the
  proven ``thai_mark_fix`` helpers) only add a Layer1 correction layer
  — both keep the base on-screen wording script-verbatim.
- v1.1 T1: every Dialogue is FAIL-LOUD width-checked against
  ``script_width.budget_px`` (HarfBuzz, Prompt Bold @ FONTSIZE) before
  any file is written. Over-budget input raises ``WidthOverflowError``
  — run it through ``script_split.split_matched`` first (the pipeline
  does this automatically).
- Style: Viral Pop only, ``FontName`` ``Prompt Bold`` (Lip-locked),
  ``ass=`` filter compatible, 1080x1920 PlayRes.

Writes the ``.ass`` file only when the gate passes. Returns the ASS
text. Raises ``MismatchGateError`` (no file written) when mismatches
are non-empty, ``ValueError`` on empty matched input or bad timing.
"""
from __future__ import annotations

from pathlib import Path

from script_gate import MismatchGateError, assert_gate
from script_keywords import colorize
from script_marks import (
    assert_no_overlap,
    assert_no_zero_duration,
    build_overlays,
    validate_final_cues,
)
from script_timing import trim_overlaps
from script_width import WidthOverflowError, assert_fit

#: Locked style identity.
STYLE_NAME = "ViralPop"
FONT_NAME = "Prompt Bold"

#: TikTok portrait frame (spec-locked).
PLAYRES_X = 1080
PLAYRES_Y = 1920

#: Proven batch01 Viral Pop geometry (T5 lock — Lip-signed look).
#: Fontsize 104, margins 47/47/158, pos (540,1580)  # Lip-signed 2026-09-20 TikTok UI clear (was 1721), Outline 3 Shadow 1.
#: script_width.py MUST mirror these (a test pins them equal); the width
#: budget is (1080-47-47)*0.94 = 926.84px.
FONTSIZE = 104

#: Bottom-centre caption anchor (proven batch01 composition).
POS_X = 540
POS_Y = 1580
MARGIN_L = 47
MARGIN_R = 47
MARGIN_V = 158

#: Proven batch01 border/shadow weights.
OUTLINE = 3
SHADOW = 1

#: Alignment tag + pop animation tags (shared verbatim by Layer1 mark
#: overlays so the correction scales/pops in lockstep with the base).
AN_TAG = r"\an2"
SCALE_TAGS = (
    r"\fscx97\fscy97\t(0,120,\fscx103\fscy103)"
    r"\t(120,210,\fscx100\fscy100)"
)


def fmt(sec: float) -> str:
    """ASS H:MM:SS.cc timestamp (same shape as build_v4.fmt)."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    """Escape ASS control chars. Whitespace/wording is untouched."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _coerce_matched(alignment) -> list[dict]:
    """Accept the full alignment dict (gated) or a bare matched list."""
    if isinstance(alignment, dict):
        # Ticket-03 gate: non-empty mismatches -> raise BEFORE any write.
        assert_gate(alignment)
        matched = alignment.get("matched", [])
    elif isinstance(alignment, list):
        matched = alignment
    else:
        raise ValueError(
            "build_ass expects an alignment dict or a matched list, "
            f"got {type(alignment).__name__}"
        )
    if not matched:
        raise ValueError("build_ass refuses empty matched input (nothing to show)")
    return matched


def _cue_text(entry: dict) -> str:
    """The one true cue text: the script line, verbatim.

    ``script_line`` is authoritative; ``text``/``display_text`` from
    ticket 01 always equal it, but we read the script line itself so no
    STT-derived string can ever become on-screen text.
    """
    for key in ("script_line", "text", "display_text"):
        value = entry.get(key)
        if isinstance(value, str) and value != "":
            if key != "script_line" and entry.get("script_line") not in (None, value):
                raise ValueError(
                    f"cue text mismatch for line {entry.get('line_index')}: "
                    "script_line and derived text disagree — refusing to guess"
                )
            if key == "script_line":
                return value
    # Fall here only when script_line itself is missing/empty.
    raise ValueError(
        f"matched entry for line {entry.get('line_index')} has no script_line"
    )


def _dialogue_tag() -> str:
    return f"{{{AN_TAG}\\pos({POS_X},{POS_Y}){SCALE_TAGS}}}"


def build_ass(alignment, out_path=None, keywords=None, exceptions=None,
              profile: str = "general") -> str:
    """Build Viral Pop ASS text from aligned script lines.

    *keywords*: optional ``[...]`` list — matching substrings of each
    (sub-)cue's SCRIPT text render same-size yellow (T3). Empty/None =
    no yellow. Colorize runs AFTER T2 split per piece, so yellow spans
    survive cuts without merging pieces.

    *exceptions*: optional ``[ids...]`` — readability-floor waivers.
    A cue below the duration/syllable floor burns ONLY when it carries
    "exception": <id> listed here (Lip-approved, recorded). Without it,
    final validation REFUSES (fail-loud). See SPEC_CUES.md.

    *profile*: readability profile for the final gate ("general"
    strict default; "vo" voiceover profile for full-auto VO burning).

    Stacked Thai-mark clusters NO LONGER emit Layer1 overlays (retired
    2026-09-24 — plain text shapes correctly; overlays caused tofu/float).
    build_overlays is now a no-op returning (colored, []).

    Returns the ASS document. Writes ``out_path`` only after the gate
    passes and every cue validates. Raises ``MismatchGateError`` (no
    file written) when mismatches are non-empty.
    """
    matched = _coerce_matched(alignment)
    # T5-fix: trim STT-jitter overlaps (text untouched, onsets untouched)
    # BEFORE content QA — unresolvable pairs still refuse loudly inside
    # trim_overlaps. Then T5 content QA: zero-duration + overlap refuse
    # BEFORE write.
    matched = trim_overlaps(matched)
    assert_no_zero_duration(matched)
    assert_no_overlap(matched)
    # Codex review 2026-09-24: final gate on the EXACT burning list,
    # AFTER the last timing change — post-rounding spans, width,
    # duration/syllable/rate floors, negation integrity. Warnings are
    # printed (pipeline log) and returned by validate_final_cues.
    final = validate_final_cues(matched, keywords, exceptions,
                                profile=profile)
    for warning in final["warnings"]:
        print(f"CUE_WARN: {warning}")
    tag = _dialogue_tag()
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {PLAYRES_X}",
        f"PlayResY: {PLAYRES_Y}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,"
        "Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: {STYLE_NAME},{FONT_NAME},{FONTSIZE},"
        f"&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{OUTLINE},{SHADOW},2,"
        f"{MARGIN_L},{MARGIN_R},{MARGIN_V},1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    body = []
    for entry in matched:
        text = _cue_text(entry)
        if "\n" in text or "\r" in text or r"\N" in text:
            raise ValueError(
                f"script line {entry.get('line_index')} spans lines — "
                "one script line must be exactly one cue"
            )
        start = float(entry["start"])
        end = float(entry["end"])
        if not start < end:
            raise ValueError(
                f"invalid timing for line {entry.get('line_index')}: "
                f"start={start} end={end}"
            )
        # T3: colorize the SCRIPT piece (same fontsize, no \\N). Runs
        # AFTER T2 split per piece — yellow survives cuts, pieces never
        # merge for color.
        colored = colorize(ass_escape(text), keywords)
        # T4: Layer1 mark-fix overlays share anim tags + active keyword
        # color with this base cue; no-op for single-mark text.
        main_text, overlays = build_overlays(
            text, colored, POS_X, POS_Y, FONTSIZE, AN_TAG, SCALE_TAGS)
        body.append(
            f"Dialogue: 0,{fmt(start)},{fmt(end)},{STYLE_NAME},,0,0,0,,"
            f"{tag}{main_text}"
        )
        for overlay in overlays:
            body.append(
                f"Dialogue: 1,{fmt(start)},{fmt(end)},{STYLE_NAME},,0,0,0,,"
                f"{overlay}"
            )
    ass_text = "\n".join(header + body) + "\n"
    # v1.1 T1: FAIL-LOUD width gate on the exact on-screen texts, BEFORE
    # any file is written. Over-budget input must go through
    # script_split.split_matched first (the pipeline wires this).
    assert_fit([_cue_text(e) for e in matched])
    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(ass_text, encoding="utf-8")
    return ass_text
