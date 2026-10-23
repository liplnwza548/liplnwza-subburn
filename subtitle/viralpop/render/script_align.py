#!/usr/bin/env python3
"""Ticket 01: align script lines to an STT word timeline (timing only).

Single entrypoint:

    align_script_to_timeline(script_lines, words) -> Alignment

- ``script_lines``: list[str] — Lip's script, one cue per line, verbatim.
- ``words``: list[dict] (Groq ``verbose_json`` top-level ``words[]`` shape:
  ``{"word": str, "start": float, "end": float}``) — or the full
  verbose_json dict containing a ``"words"`` key.  Only ``start``/``end``
  are used; STT strings are NEVER used as subtitle text.
- Returns ``{"matched": [...], "mismatches": [...]}`` where each matched
  entry carries ``start``/``end`` derived from the STT span and
  ``script_line``/``text``/``display_text`` fields that equal the script
  line EXACTLY.

Matching mirrors ``render/align.py`` ``align_cues`` (same search window,
same length penalty, same 0.42 acceptance threshold, same min/max span
timing, same monotonic cursor) so timing stays consistent with the
existing pipeline. The difference: failures are collected into
``mismatches[]`` instead of raising SystemExit, and this module never
imports or touches the numeric/loanword fix files.

Writes nothing: no ASS, no MP4 (tickets 02-05).
"""
from __future__ import annotations

import difflib
import re

#: Same acceptance threshold as render/align.py align_cues.
MATCH_THRESHOLD = 0.42

#: Heard-snippet window (chars) around the cursor for mismatch reports.
_SNIPPET_BEFORE = 12
_SNIPPET_AFTER = 30


def _compact(s: str) -> str:
    """Strip all whitespace (identical to build_v4.compact)."""
    return re.sub(r"\s+", "", s)


def _build_char_stream(words: list[dict]) -> tuple[str, list[dict]]:
    """Expand word timestamps to one timed entry per compacted char.

    Returns (stream_text, spans) with spans parallel to stream_text.
    Each span is {"start", "end", "word_index"}. Times are never created
    or altered — every char inherits its word's real timestamps.
    """
    chars: list[str] = []
    spans: list[dict] = []
    for wi, w in enumerate(words):
        token = w.get("word", w.get("text", ""))
        start = float(w["start"])
        end = float(w["end"])
        for ch in _compact(token):
            chars.append(ch)
            spans.append({"start": start, "end": end, "word_index": wi})
    return "".join(chars), spans


def _cursor_time(src_spans: list[dict], cursor: int,
                 matched: list[dict]) -> float | None:
    """Best-known audio time at the cursor for mismatch time hints."""
    if cursor < len(src_spans):
        return float(src_spans[cursor]["start"])
    if matched:
        return float(matched[-1]["end"])
    return None


def _heard_snippet(src_text: str, cursor: int) -> str:
    a = max(0, cursor - _SNIPPET_BEFORE)
    b = min(len(src_text), cursor + _SNIPPET_AFTER)
    return src_text[a:b]


#: Thai numeral words -> Arabic digits for match SCORING only (VO
#: 2026-09-24). STT writes digits ("4") where the script writes words
#: ("สี่"): without this the fuzzy matcher skips the digit-word and the
#: line aligns from the NEXT word (first cue pops late). Applied to a
#: COPY of the target for ratio computation only — coordinates and
#: display text are never touched. Single-syllable 0-9 only: compounds
#: (สิบสี่) convert partially, which still beats zero overlap; common
#: non-numeral hosts (ห้าม) shift all candidates near-equally, and the
#: mismatch gate still guards garbage.
_NUMERAL_WORDS = (
    ("ศูนย์", "0"), ("หนึ่ง", "1"), ("สอง", "2"), ("สาม", "3"),
    ("สี่", "4"), ("ห้า", "5"), ("หก", "6"), ("เจ็ด", "7"),
    ("แปด", "8"), ("เก้า", "9"),
)


def _norm_numerals(s: str) -> str:
    for word, digit in _NUMERAL_WORDS:
        s = s.replace(word, digit)
    return s


def _best_span(src_text: str, target: str, cursor: int,
               n_remaining_after: int,
               threshold: float = MATCH_THRESHOLD,
               ) -> tuple[float, int, int] | None:
    """Fuzzy window search from cursor; same shape as align_cues."""
    min_remaining = 3 * n_remaining_after
    best = None
    # Numeral-aware scoring copy: 'สี่' matches STT '4' (coordinates and
    # the original target string are untouched — only the ratio changes).
    norm_target = _norm_numerals(target)
    max_start = max(cursor, len(src_text) - min_remaining - 1)
    for start_idx in range(cursor, max_start + 1):
        max_end = min(len(src_text), start_idx + len(target) + 15)
        min_end = min(max_end, start_idx + max(1, len(target) - 15))
        for end_idx in range(min_end, max_end + 1):
            if len(src_text) - end_idx < min_remaining:
                continue
            ratio = difflib.SequenceMatcher(
                None, norm_target, src_text[start_idx:end_idx]).ratio()
            length_penalty = (
                0.03 * abs((end_idx - start_idx) - len(target))
                / max(1, len(target))
            )
            score = ratio - length_penalty
            if best is None or score > best[0]:
                best = (score, start_idx, end_idx)
    if best is None or best[0] < threshold:
        return None
    return best


def align_script_to_timeline(
    script_lines: list[str],
    words: list[dict] | dict,
    *,
    threshold: float = MATCH_THRESHOLD,
) -> dict:
    """Align each script line to the STT word timeline.

    Returns {"matched": [...], "mismatches": [...]}:
    - matched[i]: {"line_index", "script_line", "text", "display_text",
      "start", "end", "source_char_start", "source_char_end",
      "text_match_ratio"}. ``text``/``display_text`` ALWAYS equal
      ``script_line`` exactly — STT strings never become cue text.
    - mismatches[i]: {"line_index", "script_line", "time_hint",
      "heard_snippet", "reason"}. ``heard_snippet`` is a raw STT span
      for humans only, never for display.
    """
    global MATCH_THRESHOLD
    if isinstance(words, dict):
        words = words.get("words") or []
    words = list(words or [])

    matched: list[dict] = []
    mismatches: list[dict] = []

    if not words:
        for i, line in enumerate(script_lines):
            mismatches.append({
                "line_index": i,
                "script_line": line,
                "time_hint": None,
                "heard_snippet": "",
                "reason": "empty_word_timeline",
            })
        return {"matched": matched, "mismatches": mismatches}

    src_text, src_spans = _build_char_stream(words)
    if not src_text:
        for i, line in enumerate(script_lines):
            mismatches.append({
                "line_index": i,
                "script_line": line,
                "time_hint": None,
                "heard_snippet": "",
                "reason": "empty_word_timeline",
            })
        return {"matched": matched, "mismatches": mismatches}

    # Monotonic cursor: each line matches at/after the previous match end.
    cursor = 0
    total = len(script_lines)
    for i, line in enumerate(script_lines):
        target = _compact(line)
        if not target:
            mismatches.append({
                "line_index": i,
                "script_line": line,
                "time_hint": _cursor_time(src_spans, cursor, matched),
                "heard_snippet": _heard_snippet(src_text, cursor),
                "reason": "empty_script_line",
            })
            continue
        found = _best_span(src_text, target, cursor, total - i - 1,
                           threshold=threshold)
        if found is None:
            mismatches.append({
                "line_index": i,
                "script_line": line,
                "time_hint": _cursor_time(src_spans, cursor, matched),
                "heard_snippet": _heard_snippet(src_text, cursor),
                "reason": "no_span_above_threshold",
            })
            continue
        score, src_a, src_b = found
        span = src_spans[src_a:src_b]
        start = min(float(s["start"]) for s in span)
        end = max(float(s["end"]) for s in span)
        if not start < end:
            mismatches.append({
                "line_index": i,
                "script_line": line,
                "time_hint": _cursor_time(src_spans, cursor, matched),
                "heard_snippet": _heard_snippet(src_text, cursor),
                "reason": "invalid_timing",
            })
            continue
        cursor = src_b
        matched.append({
            "line_index": i,
            "script_line": line,
            # Cue/display text is the script line verbatim. STT
            # strings are used only for timing, never for text.
            "text": line,
            "display_text": line,
            "start": start,
            "end": end,
            "source_char_start": src_a,
            "source_char_end": src_b,
            "text_match_ratio": round(score, 4),
        })
    # Repeat-latch heuristic (2026-09-29, C4): the matcher is greedy, so an
    # opening line that repeats as the closing line can latch onto the
    # LATER span, starving every line after it. Flag when line 0 lands far
    # from the timeline start — a human verifies the order, nothing auto.
    warnings = []
    if matched:
        total = max(float(s["end"]) for s in src_spans) if src_spans else 0.0
        first_start = float(matched[0]["start"])
        if total > 0 and first_start > 0.3 * total:
            warnings.append(
                f"line 0 matched at {first_start:.2f}s of {total:.2f}s "
                f"timeline — opening line may have latched onto a repeated "
                f"later span; verify cue order before burning")
    return {"matched": matched, "mismatches": mismatches,
            "warnings": warnings}
