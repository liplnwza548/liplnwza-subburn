#!/usr/bin/env python3
"""v1.1 T5-fix: overlap trim for STT-aligned script (sub-)cues.

Real STT-aligned adjacent script lines / T2 sub-cues can slightly
overlap (word-timestamp jitter at boundaries). The old batch01 path
handled this with a post_process overlap trim; T5's ``assert_no_overlap``
refused loudly instead, which blocked real burns (batch01_v11 clip01:
line 5 starts 11.04 < prev end 11.42).

This module restores the trim, WITHOUT touching display text:

- ``trim_overlaps(matched)``: for each adjacent pair with
  ``cue[i].end > cue[i+1].start``, set
  ``cue[i].end = cue[i+1].start - eps`` (onsets untouched, text
  untouched). Runs on COPIES — the input list is never mutated.
- Fail-loud is KEPT: when a trim would leave zero/negative (below
  ``MIN_DURATION``) duration, the pair is unresolvable and a
  ``ValueError`` listing EVERY bad pair is raised — same spirit as the
  old cascade-refuse. Zero-duration input also raises.
- ``assert_no_overlap`` / ``assert_no_zero_duration`` stay strict and
  run AFTER the trim (defence in depth in ``build_ass``).

Wire order: align -> gate -> T2 split -> ``trim_overlaps`` ->
colorize/overlays/write. ``build_ass`` also trims defensively at entry
so direct callers (tests, old code) get clean spans too.
"""
from __future__ import annotations

#: ASS timestamp resolution guard (fmt rounds to centiseconds); mirrors
#: script_marks.OVERLAP_EPS so trimmed spans always pass the assert.
OVERLAP_EPS = 0.01

#: Minimum surviving cue duration after a trim (seconds). ~1 frame at
#: 25fps; guarantees distinct ASS centisecond timestamps on screen.
#: A trim that would leave less than this FAILS LOUD instead of emitting
#: a sliver/zero-duration cue.
MIN_DURATION = 0.04

#: Floor for readable cue duration (seconds). A cue flashing shorter than
#: this reads as a blink and breaks phrase rhythm (C6 QA 2026-09-24:
#: "แต่ยัง" 0.38s, "ก็เข้ากัน"/"เข้ากัน" 0.52+0.58s with position jitter).
#: Short cues must merge with a neighbour — wording concatenated VERBATIM
#: (Thai: no space), span = first.start → last.end. Merging NEVER drops
#: words to fit.
MIN_CUE_DURATION = 0.6


def merge_short_cues(
    matched: list[dict],
    min_duration: float = MIN_CUE_DURATION,
    max_width_px: float | None = None,
    max_syl: int = 6,
    allow_cross_line: bool = False,
) -> tuple[list[dict], dict]:
    """Merge cues shorter than *min_duration* into a neighbour.

    Rule (Lip-locked 2026-09-25, client-batch1 C2 post-mortem): cross-line
    merges show the next sentence BEFORE it is spoken (~0.5s early on
    'ก็ดูโปรใส่กับขาสั้น'), which reads as a wrong sentence. Default is
    now False — a short cue that cannot merge inside its own line is
    SKIPPED (reported) so the gate refuses and Lip signs a short-flash
    exception instead. Pass allow_cross_line=True ONLY as an explicit
    legacy opt-in, never in production burns.

    Guards (Codex review 2026-09-24 — all enforced, nothing forced):
    - width: merged text must fit *max_width_px* (defaults to the
      skill width budget — a merge that overflows is SKIPPED, never
      emitted, replacing the old default of no width check at all).
    - syllables: merged text must stay within *max_syl* spoken
      syllables (count_syllables, loanword/digit/ๆ aware).
    - direction: forward preferred; when forward overflows/fails and a
      previous cue exists, backward is tried before skipping (old code
      only folded backward for the last cue).
    - line integrity: with allow_cross_line=False (the default since
      2026-09-25), cues from different script lines (line_index) are
      never merged — the cue planner passes False explicitly; True is
      an explicit legacy opt-in only.
    - provenance: merged cue keeps first.start→last.end AND the joined
      source_char_start/end span, so coverage proof survives merges.
    - pause honesty (limitation, documented): no audio-gap input in v1 —
      merges stay inside one parent line when allow_cross_line=False;
      cross-line merges assume contiguity the caller verified.

    Text/timing of untouched cues is byte-identical. Returns
    ``(new_list, report)``; input not mutated. Report:
    ``{"merged": [...], "skipped": [(text, reason)]}``.
    """
    from script_width import budget_px, measure
    from thai_line_split import count_syllables

    if max_width_px is None:
        max_width_px = budget_px()
    max_width_px = float(max_width_px)

    def _line_id(entry: dict):
        return entry.get("line_index")

    def _try_merge(first: dict, second: dict) -> tuple[dict | None, str]:
        if (not allow_cross_line
                and _line_id(first) is not None
                and _line_id(second) is not None
                and _line_id(first) != _line_id(second)):
            return None, "cross-line merge refused"
        new_text = _cue_merge_text(first, second)
        width = measure(new_text)
        if width > max_width_px:
            return None, (f"over width budget "
                          f"({width:.1f} > {max_width_px:.1f}px)")
        syl = count_syllables(new_text)
        if syl > int(max_syl):
            return None, f"over syllable cap ({syl} > {max_syl})"
        merged_cue = dict(first)
        merged_cue["end"] = second["end"]
        for key in ("script_line", "text", "display_text"):
            if key in merged_cue:
                merged_cue[key] = new_text
        if (isinstance(first.get("source_char_start"), int)
                and isinstance(second.get("source_char_end"), int)):
            merged_cue["source_char_start"] = first["source_char_start"]
            merged_cue["source_char_end"] = second["source_char_end"]
        if first.get("is_estimated") or second.get("is_estimated"):
            merged_cue["is_estimated"] = True
            merged_cue["timing_method"] = "merge-of-estimates"
        return merged_cue, ""

    out = [dict(entry) for entry in matched]
    merged: list[str] = []
    skipped: list[tuple[str, str]] = []
    i = 0
    while i < len(out):
        # 1e-6 tolerance: ASS resolves centiseconds, so float dust on an
        # exact-floor span (0.5999999s) must not force a merge.
        dur = float(out[i]["end"]) - float(out[i]["start"])
        if dur >= float(min_duration) - 1e-6:
            i += 1
            continue
        if i + 1 < len(out):
            new_cue, reason = _try_merge(out[i], out[i + 1])
            if new_cue is not None:
                out[i] = new_cue
                del out[i + 1]
                merged.append(new_cue.get("script_line", ""))
            elif i > 0:
                # forward failed: try folding backward before skipping
                back, back_reason = _try_merge(out[i - 1], out[i])
                if back is not None:
                    out[i - 1] = back
                    del out[i]
                    merged.append(back.get("script_line", ""))
                    i -= 1  # re-check the merged cue
                else:
                    skipped.append((
                        _cue_merge_text(out[i], out[i + 1]),
                        f"forward: {reason}; backward: {back_reason}"))
                    i += 1
            else:
                skipped.append((_cue_merge_text(out[i], out[i + 1]),
                                reason))
                i += 1
        else:
            # short last cue: fold backward into previous
            if i == 0:
                skipped.append((_cue_merge_text(out[i], out[i]),
                                "single short cue, nothing to merge"))
                i += 1
                continue
            new_cue, reason = _try_merge(out[i - 1], out[i])
            if new_cue is not None:
                out[i - 1] = new_cue
                del out[i]
                merged.append(new_cue.get("script_line", ""))
                i -= 1  # re-check merged cue
            else:
                skipped.append((_cue_merge_text(out[i - 1], out[i]),
                                reason))
                i += 1
        # re-check a forward-merged cue (may still be short → merge again)
    return out, {"merged": merged, "skipped": skipped}


def _cue_merge_text(first: dict, second: dict) -> str:
    """Concatenate two cue wordings verbatim (Thai: no separator space)."""
    from script_ass import _cue_text

    return _cue_text(first) + _cue_text(second)


def trim_overlaps(
    matched: list[dict],
    eps: float = OVERLAP_EPS,
    min_duration: float = MIN_DURATION,
) -> list[dict]:
    """Trim overlapping adjacent cue ends; fail loud when unresolvable.

    Returns a NEW list (shallow-copied dicts); input is not mutated.
    Raises ``ValueError`` listing every pair the trim cannot save
    (zero/negative-duration input, or a trim that would leave
    ``< min_duration`` seconds).
    """
    out = [dict(entry) for entry in matched]
    bad: list[str] = []
    for i in range(len(out) - 1):
        prev, cur = out[i], out[i + 1]
        try:
            start_p, end_p = float(prev["start"]), float(prev["end"])
            start_c = float(cur["start"])
        except (KeyError, TypeError, ValueError):
            bad.append(
                f"line {cur.get('line_index')} has non-numeric timing "
                f"(prev={prev.get('start')}/{prev.get('end')} "
                f"cur={cur.get('start')}/{cur.get('end')})"
            )
            continue
        if not start_p < end_p:
            bad.append(
                f"line {prev.get('line_index')} zero-duration "
                f"(start={start_p} end={end_p}) — refusing to trim"
            )
            continue
        if end_p > start_c:
            trimmed = float(start_c) - float(eps)
            if trimmed - start_p >= float(min_duration):
                prev["end"] = trimmed
                # audio-driven trim (SPEC_CUES.md): a trim-shortened cue
                # below the readability floor warns at final validation
                # instead of refusing — blocking burns on STT jitter
                # would be worse; the warning is always reported.
                prev["trimmed_end"] = True
            else:
                bad.append(
                    f"line {cur.get('line_index')} starts {start_c:.2f} "
                    f"< prev end {end_p:.2f} and trim would leave "
                    f"{trimmed - start_p:.2f}s "
                    f"(min {float(min_duration):.2f}s) — needs human review"
                )
    if bad:
        raise ValueError(
            f"overlap-trim refused ({len(bad)} unresolvable overlap(s), "
            f"eps={eps}s, min_duration={min_duration}s): " + "; ".join(bad)
        )
    return out


def assert_no_speech_overrun(
    matched: list[dict],
    segments: list[dict],
    line_bounds: dict[int, tuple[float, float]] | None = None,
    gap_tol: float = 0.08,
) -> None:
    """Refuse display-holds that overrun neighbouring lines' speech.

    A repair hold may only consume SILENCE. Any part of a line's burned
    span that sticks out past the line's ORIGINAL aligned bounds
    (``line_bounds``, pre-repair) must not intersect Groq SEGMENTS beyond
    ``gap_tol`` — otherwise the cue reads as a wrong sentence
    (client-batch1 C2 post-mortem 2026-09-25: 'สะพายปุ๊บ' held to 7.18
    while L4 speech started 6.8 per segments; the word snaps claimed
    7.26, so the hold looked safe until segments proved otherwise).

    Speech bounds come from Groq SEGMENTS (robust in fast regions where
    word snaps are fiction). Internal rebalances inside a line's own
    bounds always pass. Raises ``ValueError`` listing every overrun;
    returns None when clean. Repair tables must pass this before
    burning (pass the aligner's pre-repair per-line spans).
    """
    bounds: dict[int, list[float]] = {}
    for cue in matched:
        li = cue.get("line_index")
        if not isinstance(li, int):
            continue
        try:
            s, e = float(cue["start"]), float(cue["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if li in bounds:
            bounds[li][0] = min(bounds[li][0], s)
            bounds[li][1] = max(bounds[li][1], e)
        else:
            bounds[li] = [s, e]
    if line_bounds is None:
        line_bounds = {li: (b[0], b[1]) for li, b in bounds.items()}

    def _overlap_speech(a: float, b: float) -> float:
        """Length of [a, b] covered by segments (0 when a >= b)."""
        if b <= a:
            return 0.0
        total = 0.0
        for seg in segments:
            try:
                ss, se = float(seg["start"]), float(seg["end"])
            except (KeyError, TypeError, ValueError):
                continue
            lo, hi = max(a, ss), min(b, se)
            if hi > lo:
                total += hi - lo
        return total

    bad: list[str] = []
    for li, (cs, ce) in bounds.items():
        orig = line_bounds.get(li)
        if orig is None:
            continue
        os_, oe = float(orig[0]), float(orig[1])
        fwd = _overlap_speech(oe, ce)
        if fwd > gap_tol:
            bad.append(
                f"line {li} holds {fwd:.2f}s into speech after its "
                f"own {oe:.2f}s bound (visible until {ce:.2f}s) — "
                f"shorten the hold or take a short-flash exception"
            )
        bwd = _overlap_speech(cs, os_)
        if bwd > gap_tol:
            bad.append(
                f"line {li} holds {bwd:.2f}s into speech before its "
                f"own {os_:.2f}s bound (visible from {cs:.2f}s) — "
                f"shorten the hold or take a short-flash exception"
            )
    if bad:
        raise ValueError(
            f"speech-overrun refused ({len(bad)} overrun(s), "
            f"gap_tol={gap_tol}s): " + "; ".join(bad)
        )
