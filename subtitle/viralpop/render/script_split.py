#!/usr/bin/env python3
"""v1.1 T2: within-line width-safe split with subdivided timing.

When a script line exceeds the T1 budget, split it ONLY within that line
at Thai word boundaries (thai_line_split.split_phrase_tracked — the same
proven cutter as the old Viral Pop path, loanword/keyword protected, never
mid-word). Display text still comes from the script ONLY; STT is still
timing-only.

Sub-cue timings are subdivided from the line's OWN matched STT span
(proportional to rendered width, monotonic, never zero-duration), so each
sub-cue keeps real audio timing without inventing any.

Guarantees:
- concat(sub-cue texts) == original script line EXACTLY (char-for-char).
- one Dialogue per sub-cue; words never move across script lines
  (every piece keeps its parent line_index).
- after split every sub-cue <= budget_px (asserted, FAIL-LOUD).
- short lines that already fit pass through untouched (sub_count == 1).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import thai_line_split as tls  # noqa: E402
from script_timing import trim_overlaps  # noqa: E402  (T5-fix: STT-jitter trim)
from script_width import (  # noqa: E402
    WidthOverflowError,
    assert_fit,
    budget_px,
    ensure_init,
)

#: Minimum sub-cue duration guard floor (seconds). Proportional splits that
#: would round to zero are lifted to EPS apart — strictly increasing first,
#: duration realism is T5/post_process work, not this ticket.
_EPS = 1e-3

#: Floor used by the word-snap guard: a snapped internal boundary is kept
#: only when both neighbouring sub-cues stay at/above this duration.
#: Below it the proportional boundary is kept (fail-soft, never silent —
#: the final gate still validates every cue before burn).
_SNAP_MIN_DUR = 0.2

#: Maximum distance a boundary may move when snapping (seconds). Word
#: edges farther than this are ignored — a huge jump means the words
#: near this boundary are unreliable (STT mismatch), and moving the
#: boundary there would trade a small error for a big one.
_SNAP_MAX_MOVE = 0.35


def _token_spans(words: list[dict] | dict | None) -> list[tuple[float, float]]:
    """Sorted (start, end) of non-space STT tokens.

    Whitespace tokens are skipped: a space token's span is silence, not
    speech — treating it as a word puts boundaries mid-pause.
    """
    if isinstance(words, dict):
        words = words.get("words") or []
    spans: list[tuple[float, float]] = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        token = w.get("word", w.get("text", ""))
        if not isinstance(token, str) or not token.strip():
            continue
        try:
            s, e = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e > s:
            spans.append((s, e))
    spans.sort()
    return spans


def _word_char_ranges(
    words: list[dict] | dict | None,
) -> list[tuple[int, int, float, float]]:
    """Map each STT word to its char window + timestamps.

    Returns [(char_start, char_end_excl, t_start, t_end)] in the SAME
    compacted char coordinates that matched entries' source_char_start/
    source_char_end use (whitespace stripped, one entry per char, each
    char inheriting its word's real timestamps). Built on the aligner's
    own _build_char_stream so coordinates always agree.
    """
    from script_align import _build_char_stream  # noqa: E402 (no cycle)

    if isinstance(words, dict):
        words = words.get("words") or []
    words = list(words or [])
    if not words:
        return []
    _, cspans = _build_char_stream(words)
    ranges: dict[int, list] = {}
    for ci, sp in enumerate(cspans):
        wi = sp.get("word_index", -1)
        if wi not in ranges:
            ranges[wi] = [ci, ci + 1, float(sp["start"]), float(sp["end"])]
        else:
            ranges[wi][1] = ci + 1
            ranges[wi][3] = float(sp["end"])
    return [(cs, ce, ts, te) for cs, ce, ts, te in ranges.values()]


def _char_snap_target(bc: int,
                      ranges: list[tuple[int, int, float, float]]
                      ) -> float | None:
    """Best boundary time for STT-char position *bc* (word-aware).

    The proportional char position is our best guess of where piece A
    ends in STT coordinates. If it lands inside a word, that word
    belongs to whichever piece holds most of it: first half -> the
    word starts piece B (snap to its onset), second half (including
    the exact middle) -> the word ends piece A (snap to its end).
    A word is therefore never split across two cues. When the chosen
    edge faces a short pause (next word starts, or previous word ends,
    within a small gap), resolve to the gap middle instead (minimax:
    both sides share the error). In a char-gap with no word at all,
    same gap-middle rule for short gaps, else None (keep proportional).
    """
    order = sorted(ranges, key=lambda r: r[0])
    for cs, ce, ts, te in order:
        if cs <= bc < ce:
            if (bc - cs) < (ce - bc):
                # First half: the word starts piece B ...
                prev = [r for r in order if r[1] <= cs]
                if prev and 0 <= ts - prev[-1][3] <= 2 * _SNAP_MAX_MOVE:
                    return (prev[-1][3] + ts) / 2.0
                return ts
            # ... second half (incl. exact middle): the word ends piece A.
            nxt = [r for r in order if r[0] >= ce]
            if nxt and 0 < nxt[0][2] - te <= 2 * _SNAP_MAX_MOVE:
                return (te + nxt[0][2]) / 2.0
            return te
    prev = [r for r in order if r[1] <= bc]
    nxt = [r for r in order if r[0] >= bc]
    if prev and nxt:
        pe, ns = prev[-1][3], nxt[0][2]
        if 0 <= ns - pe <= 2 * _SNAP_MAX_MOVE:
            return (pe + ns) / 2.0
    return None


def _word_edges(words: list[dict] | dict | None) -> list[float]:
    """Collect sorted unique token start/end times from STT words."""
    if isinstance(words, dict):
        words = words.get("words") or []
    edges: set[float] = set()
    for w in words or []:
        if not isinstance(w, dict):
            continue
        try:
            s, e = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e > s:
            edges.add(s)
            edges.add(e)
    return sorted(edges)


def _snap_bounds_to_words(bounds: list[float], words: list[dict] | dict | None,
                           min_dur: float = _SNAP_MIN_DUR,
                           char_bounds: list[int] | None = None,
                           ranges: list[tuple[int, int, float, float]] | None = None
                           ) -> list[float]:
    """Snap internal sub-cue boundaries toward real word timing.

    Primary path (word-aware): when *char_bounds* (STT-char position of
    each boundary, same coordinates as *ranges*) is given, each boundary
    is resolved with _char_snap_target — a word is never split across
    two cues, pauses resolve to the gap middle.
    Fallback path (time-only): nearest speech onset logic from token
    time spans, for entries without char windows.
    A snap is kept only when the move is small and both neighbours stay
    >= min_dur and strictly increasing; otherwise the proportional
    boundary is kept (the final gate still validates everything).
    First/last bounds (the parent span) are never moved.
    """
    out = list(bounds)
    if len(out) < 3:
        return out
    use_char = (char_bounds is not None and ranges is not None
                and len(char_bounds) == len(out))
    tokens = None if use_char else _token_spans(words)
    if not use_char and not tokens:
        return out
    for i in range(1, len(out) - 1):
        t = out[i]
        target = None
        if use_char:
            target = _char_snap_target(char_bounds[i], ranges)
        else:
            assert tokens is not None
            if any(abs(t - s) < _EPS or abs(t - e) < _EPS for s, e in tokens):
                continue  # already on a word edge
            inside = [w for w in tokens if w[0] < t < w[1]]
            if inside:
                s, e = inside[0]
                target = s if t - s <= e - t else e
            else:
                prev_end = max((e for _, e in tokens if e <= t), default=None)
                next_start = min((s for s, _ in tokens if s >= t), default=None)
                if (prev_end is not None and next_start is not None
                        and next_start - prev_end <= 2 * _SNAP_MAX_MOVE):
                    target = (prev_end + next_start) / 2.0
        if target is None or abs(target - t) > _SNAP_MAX_MOVE:
            continue
        if target <= out[0] or target >= out[-1]:
            continue
        if target - out[0] < min_dur or out[-1] - target < min_dur:
            continue
        if target - out[i - 1] < min_dur or out[i + 1] - target < min_dur:
            continue
        if target > out[i - 1] + _EPS and target < out[i + 1] - _EPS:
            out[i] = target
    return out


def _cue_text(entry: dict) -> str:
    for key in ("script_line", "text", "display_text"):
        value = entry.get(key)
        if isinstance(value, str) and value != "":
            return value
    raise ValueError(
        f"split: matched entry for line {entry.get('line_index')} has no text"
    )


def _subdivide_span(start: float, end: float, weights: list[float]) -> list[tuple[float, float]]:
    """Split [start, end) into len(weights) monotonic spans ~ weights."""
    total = sum(weights)
    if total <= 0:
        raise ValueError("split: non-positive subdivision weights")
    bounds = [float(start)]
    acc = 0.0
    duration = float(end) - float(start)
    for w in weights[:-1]:
        acc += w
        bounds.append(float(start) + duration * acc / total)
    bounds.append(float(end))
    # Enforce strictly increasing (float-rounding safe, never zero-length).
    for i in range(1, len(bounds)):
        if bounds[i] <= bounds[i - 1]:
            bounds[i] = bounds[i - 1] + _EPS
    # Never run past the parent end because of epsilon nudges on tiny spans:
    # if the last nudge overflowed, pull intermediate bounds back evenly.
    if bounds[-1] > float(end):
        overflow = bounds[-1] - float(end)
        bounds[-1] = float(end)
        for i in range(len(bounds) - 2, 0, -1):
            bounds[i] = min(bounds[i], bounds[i + 1] - _EPS)
            if bounds[i] > bounds[i - 1]:
                break
        if bounds[0] >= bounds[1]:
            raise ValueError(
                f"split: parent span [{start}, {end}] too small for "
                f"{len(weights)} sub-cues — refusing zero-duration cues"
            )
        _ = overflow
    spans = list(zip(bounds[:-1], bounds[1:]))
    for s, e in spans:
        if not s < e:
            raise ValueError(
                f"split: zero-duration sub-cue [{s}, {e}] — refusing to emit"
            )
    return spans


def _keyword_spans(text: str,
                   keywords: list[str] | None) -> list[tuple[int, int]]:
    """Char spans of every keyword occurrence in *text* (may overlap).

    Empty/blank keywords are ignored. Overlapping occurrences are all
    returned; _safe_split_points treats any span covering i as protected.
    """
    spans: list[tuple[int, int]] = []
    for kw in keywords or []:
        if not isinstance(kw, str) or not kw.strip():
            continue
        start = 0
        while True:
            i = text.find(kw, start)
            if i < 0:
                break
            spans.append((i, i + len(kw)))
            start = i + 1
    return spans


def split_matched(
    matched: list[dict],
    budget: float | None = None,
    words: list[dict] | dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Expand over-budget matched entries into sub-cue entries.

    Entries that fit pass through with sub_index=0/sub_count=1 (same text,
    same timing, plus parent bookkeeping). Over-budget entries become N
    sub-entries whose texts concatenate to the original exactly.

    When *words* (STT word timeline) is given, internal sub-cue
    boundaries are snapped to the nearest real word edge (word-snap):
    proportional width-based boundaries land mid-word and make cues pop
    early or linger late by ~0.1-0.2s. Snaps that would break duration
    guards fall back to the proportional boundary; the final gate still
    validates every cue before burn.

    Text cuts come from thai_line_split.split_phrase_tracked on a
    word-boundary grid (newmm dictionary when pythainlp is installed;
    whitespace-only grid on offline boxes). A spaceless run with no safe
    cut raises WidthOverflowError (fail loud) instead of severing a word
    mid-grapheme (C6 2026-09-25: เซ็ต -> เซ็ | ต).

    When *keywords* is given, every occurrence of each keyword in a
    script line becomes a protected span (same mechanism as loanwords):
    the cutter may not sever it, so Lip never has to approve a split
    keyword by hand. The R4 gate in script_marks stays as the backstop.
    """
    ensure_init()
    budget = budget_px() if budget is None else float(budget)
    ranges = _word_char_ranges(words) if words is not None else []
    expanded: list[dict] = []
    for entry in matched:
        text = _cue_text(entry)
        if tls.width_px(text) <= budget or len(text) < 2:
            keep = dict(entry)
            keep.setdefault("sub_index", 0)
            keep.setdefault("sub_count", 1)
            keep.setdefault("parent_script_line", keep.get("script_line", text))
            expanded.append(keep)
            continue
        try:
            pieces, forced = tls.split_phrase_tracked(
                text, budget, protected_spans=_keyword_spans(text, keywords))
        except tls.NoSafeSplitError as exc:
            raise WidthOverflowError(
                f"splitter: no word-boundary-safe cut for script line "
                f"{entry.get('line_index')} ({text!r}): {exc}"
            ) from exc
        if "".join(pieces) != text:
            raise WidthOverflowError(
                f"splitter rewrote script line {entry.get('line_index')}: "
                f"{text!r} -> {pieces!r} — refusing to guess"
            )
        widths = [max(tls.width_px(p), _EPS) for p in pieces]
        start = float(entry["start"])
        end = float(entry["end"])
        if not start < end:
            raise ValueError(
                f"split: invalid parent timing for line {entry.get('line_index')}"
            )
        spans = _subdivide_span(start, end, widths)
        # Subdivide the source-char window proportionally to piece length
        # (traceability only; display text is still the script pieces).
        # Computed BEFORE the snap so the word-aware path can resolve
        # each boundary to the STT word holding most of it.
        char_a = entry.get("source_char_start")
        char_b = entry.get("source_char_end")
        char_bounds = None
        if isinstance(char_a, int) and isinstance(char_b, int) and char_b > char_a:
            lens = [max(len(p), 1) for p in pieces]
            tot = sum(lens)
            char_bounds = [char_a]
            acc = 0
            for ln in lens[:-1]:
                acc += ln
                char_bounds.append(char_a + round((char_b - char_a) * acc / tot))
            char_bounds.append(char_b)
        if words is not None:
            # Word-snap: resolve each internal boundary to the STT word
            # holding most of it (never split a spoken word across cues;
            # pauses resolve to the gap middle).
            bounds = [start] + [e for _, e in spans]
            bounds = _snap_bounds_to_words(bounds, words, _SNAP_MIN_DUR,
                                           char_bounds, ranges)
            spans = list(zip(bounds[:-1], bounds[1:]))
            for s, e in spans:
                if not s < e:
                    spans = _subdivide_span(start, end, widths)
                    break
        for sub_i, (piece, (s, e)) in enumerate(zip(pieces, spans)):
            sub = dict(entry)
            sub.update({
                "script_line": piece,
                "text": piece,
                "display_text": piece,
                "parent_script_line": text,
                "sub_index": sub_i,
                "sub_count": len(pieces),
                "is_split": True,
                "split_forced": bool(forced),
                "start": s,
                "end": e,
            })
            if char_bounds is not None:
                sub["source_char_start"] = char_bounds[sub_i]
                sub["source_char_end"] = char_bounds[sub_i + 1]
            expanded.append(sub)
    # Post-condition: every emitted cue fits (assert, FAIL-LOUD).
    assert_fit(expanded, budget=budget)
    # T5-fix: trim STT-jitter / subdivision-boundary overlaps so build_ass
    # receives clean spans (text untouched; unresolvable pairs fail loud).
    return trim_overlaps(expanded)


def split_alignment(alignment: dict, budget: float | None = None,
                      words: list[dict] | dict | None = None,
                      keywords: list[str] | None = None) -> dict:
    """Split an alignment's matched list in place shape (mismatches kept).

    Returns a NEW alignment dict; the input is not mutated.

    When *words* is given it is forwarded to split_matched for word-snap
    of internal sub-cue boundaries (None = legacy proportional timing).
    When *keywords* is given each occurrence becomes a protected span
    the cutter may not sever (None = loanwords only).
    """
    if not isinstance(alignment, dict) or not isinstance(
        alignment.get("matched"), list
    ):
        raise ValueError(
            "split_alignment expects {'matched': [...], 'mismatches': [...]}"
        )
    out = dict(alignment)
    out["matched"] = split_matched(alignment["matched"], budget=budget,
                                   words=words, keywords=keywords)
    return out
