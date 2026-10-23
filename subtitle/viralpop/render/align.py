#!/usr/bin/env python3
"""Audio-grounded cue alignment (verbatim extraction, no new timing math).

The two functions below implement exactly the alignment loop from
``build_v4.py`` ``main()`` (same window sizes, same length penalty, same
0.42 acceptance threshold, same min/max span timing). ``build_v4.py`` and
``build_v5.py`` are intentionally left untouched as the tes2 golden
reference; new generalized jobs import from here instead of duplicating
the loop a third time.

Rules preserved:
- cue start/end = min/max of the matched Whisper token/char span only;
- never weighted/proportional timing, never VAD/silence/energy timing.
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_v4 as v4  # noqa: E402  (reuse compact/fmt/ass_escape, unmodified)
import loanwords as loanwords  # noqa: E402


def _norm_stream(raw_text: str, raw_spans: list[dict]) -> tuple[str, list[dict]]:
    """Apply the loanword variant map to a compacted source char stream.

    Chars inside a variant occurrence are replaced by the canonical Thai
    chars as a group sharing the occurrence's min/max span (the loanword is
    an atomic timing unit — same min/max derivation the DP uses for any
    phrase, not a weighted redistribution). Returns (norm_text, norm_spans)
    with norm_spans parallel to norm_text. Idempotent on canonical text.

    Owner-verified contextual corrections (same table as normalize()) are
    applied afterwards so a homophone fix like
    'วันนี้เราบอกเลยว่าตอบโจทย์' -> 'ใบนี้เราบอกเลยว่าตอบโจทย์' maps the
    full ASR span (2.04-3.16, 11.74-12.82) instead of fuzzy-dropping the
    leading chars and shifting the onset late. Timing stays ASR-derived.
    """
    norm_text = raw_text
    # Occurrence search is shared with normalize() (longest-first locking +
    # fused-match guard), so both sides always agree on what was replaced.
    occs = loanwords._collect(norm_text)
    # Apply from the end so earlier indices stay valid.
    spans = list(raw_spans)
    for a, b, canonical in sorted(occs, reverse=True):
        group = spans[a:b]
        st = min(s["start"] for s in group)
        en = max(s["end"] for s in group)
        tok = group[0]["token"]
        spans[a:b] = [{"start": st, "end": en, "token": tok} for _ in canonical]
        norm_text = norm_text[:a] + canonical + norm_text[b:]
    for e in loanwords._load().get("contextual_corrections", []):
        pat, rep = e["pattern"], e["replacement"]
        # Minimal differing middle only (common prefix/suffix stay in place
        # with their own per-char timing). For วันนี้->ใบนี้ this replaces
        # only "วัน" (3 chars, 2.04-2.38) with "ใบ" (2 chars), preserving
        # the 2.04 onset and the rest of the sentence's timing granularity.
        pre = 0
        while pre < min(len(pat), len(rep)) and pat[pre] == rep[pre]:
            pre += 1
        suf = 0
        while (suf < min(len(pat) - pre, len(rep) - pre)
               and pat[len(pat) - 1 - suf] == rep[len(rep) - 1 - suf]):
            suf += 1
        mid_pat, mid_rep = pat[pre:len(pat) - suf], rep[pre:len(rep) - suf]
        start = 0
        while True:
            idx = norm_text.find(pat, start)
            if idx < 0:
                break
            a, b = idx + pre, idx + pre + len(mid_pat)
            group = spans[a:b]
            st = min(s["start"] for s in group)
            en = max(s["end"] for s in group)
            tok = group[0]["token"]
            spans[a:b] = [{"start": st, "end": en, "token": tok} for _ in mid_rep]
            norm_text = norm_text[:a] + mid_rep + norm_text[b:]
            start = idx + len(rep)
    return norm_text, spans


def _normalize_digit_runs_text(text: str) -> str:
    """Replace digit runs with Thai readings (text-only, no spans).

    Used to normalize both source and target so alignment matching is
    consistent.  Raises ValueError for unsupported formats (>999).
    """
    result = text
    for m in reversed(list(re.finditer(r"\d+", result))):
        run = m.group()
        n = int(run)
        reading = loanwords.thai_number_reading(n)
        if reading == run:
            continue
        a, b = m.start(), m.end()
        result = result[:a] + reading + result[b:]
    return result


def _numeric_canonicalize(
    text: str, spans: list[dict]
) -> tuple[str, list[dict]]:
    """Replace digit runs with Thai readings for alignment matching.

    Each alias char shares the original digit run's min/max timing
    (atomic timing unit, same derivation the DP uses for loanwords).
    Raises SystemExit for unsupported formats to prevent silent
    fallback to suffix-only matching.
    """
    norm_text = text
    norm_spans = list(spans)
    for m in reversed(list(re.finditer(r"\d+", norm_text))):
        run = m.group()
        try:
            n = int(run)
            reading = loanwords.thai_number_reading(n)
        except (ValueError, OverflowError) as exc:
            raise SystemExit(
                f"numeric canonicalization: digit run '{run}' at position "
                f"{m.start()} is outside supported range (0-999): {exc}; "
                f"cannot align with corrected text"
            ) from exc
        if reading == run:
            continue
        a, b = m.start(), m.end()
        group = norm_spans[a:b]
        st = min(s["start"] for s in group)
        en = max(s["end"] for s in group)
        tok = group[0]["token"]
        norm_spans[a:b] = [
            {"start": st, "end": en, "token": tok} for _ in reading
        ]
        norm_text = norm_text[:a] + reading + norm_text[b:]
    return norm_text, norm_spans


def attach_words(segments: list[dict], words: list[dict]) -> None:
    """Route top-level word timestamps (Groq verbose_json shape) into their
    segments in place, partitioned by TEXT (not by time).

    Why: Groq's segmenter can cut mid-word in time (a word starting just
    before a segment boundary while its text belongs to the next segment),
    so time-overlap routing misplaces boundary words and breaks the
    char-stream consistency check. Instead, expand all words to one timed
    char stream, verify it exactly tiles the concatenated segment texts,
    then slice per segment. Times are never created or altered — a word
    straddling a boundary is split into two dicts carrying the same real
    timestamps. Any tiling mismatch raises SystemExit (refuses to guess).
    """
    chars: list[tuple[str, float, float]] = []
    for w in words:
        for ch in v4.compact(w.get("word", "")):
            chars.append((ch, float(w["start"]), float(w["end"])))
    full = "".join(c[0] for c in chars)
    expect = "".join(v4.compact(s.get("text", "")) for s in segments)
    if full != expect:
        idx = next((i for i, (a, b) in enumerate(zip(full, expect)) if a != b), min(len(full), len(expect)))
        raise SystemExit(
            "word char stream does not tile segment texts "
            f"(first diff at char {idx}: stream={full[max(0,idx-8):idx+8]!r} "
            f"segments={expect[max(0,idx-8):idx+8]!r}); refusing to guess"
        )
    pos = 0
    for seg in segments:
        need = len(v4.compact(seg.get("text", "")))
        seg_words: list[dict] = []
        while need > 0:
            ch, st, en = chars[pos]
            # Re-group consecutive same-timestamp chars into one fragment,
            # stopping at the segment boundary (a straddling word is split
            # into two dicts with identical real timestamps).
            run = ch
            j = pos + 1
            while len(run) < need and j < len(chars) \
                    and chars[j][1] == st and chars[j][2] == en:
                run += chars[j][0]
                j += 1
            seg_words.append({"word": run, "start": st, "end": en})
            pos += len(run)
            need -= len(run)
        seg["words"] = seg_words


def build_src_spans(seg: dict) -> tuple[str, list[dict]]:
    """Per-character timing table for one transcript segment.

    Raises SystemExit (like build_v4) when the segment has no timed
    characters or the token stream is inconsistent with the text.
    """
    src_text = v4.compact(seg.get("text", ""))
    src_words = seg.get("words", [])
    src_spans: list[dict] = []
    for token_index, token in enumerate(src_words):
        token_text = v4.compact(token.get("word", ""))
        for _ in token_text:
            src_spans.append({
                "start": float(token["start"]),
                "end": float(token["end"]),
                "token": token_index,
            })
    if not src_spans:
        raise SystemExit(f"segment {seg.get('id')} has no timed characters")
    if "".join(v4.compact(w.get("word", "")) for w in src_words) != src_text \
            or len(src_spans) != len(src_text):
        raise SystemExit(f"segment {seg.get('id')} lacks consistent character timing")
    # Normalize ASR spelling variants (Latin loanwords, misspellings) to
    # canonical Thai so corrected phrases can fuzzy-match; timestamps ride
    # along per group (see _norm_stream). Timing derivation is unchanged.
    src_text, src_spans = _norm_stream(src_text, src_spans)
    # Numeric canonicalization: replace digit runs (e.g. "190") with Thai
    # readings so corrected phrases like "ร้อยเก้าสิบ" can full-match.
    src_text, src_spans = _numeric_canonicalize(src_text, src_spans)
    return src_text, src_spans


def align_cues(seg_id, src_text: str, src_spans: list[dict], phrases: list[str]) -> list[dict]:
    """Map each phrase (in order) onto the source char stream and derive its
    start/end from the matched span. Monotonic cursor, same as build_v4."""
    records: list[dict] = []
    source_cursor = 0
    for phrase_index, text in enumerate(phrases):
        target = v4.compact(text)
        # Normalize digit runs in target to match the normalized source,
        # so "6" in corrected text matches "หก" in normalized source.
        target = _normalize_digit_runs_text(target)
        remaining_targets = [v4.compact(t) for t in phrases[phrase_index + 1:]]
        min_remaining = 3 * len(remaining_targets)
        best = None
        max_start = max(source_cursor, len(src_text) - min_remaining - 1)
        for start_idx in range(source_cursor, max_start + 1):
            max_end = min(len(src_text), start_idx + len(target) + 15)
            min_end = min(max_end, start_idx + max(1, len(target) - 15))
            for end_idx in range(min_end, max_end + 1):
                if len(src_text) - end_idx < min_remaining:
                    continue
                ratio = difflib.SequenceMatcher(None, target, src_text[start_idx:end_idx]).ratio()
                length_penalty = 0.03 * abs((end_idx - start_idx) - len(target)) / max(1, len(target))
                score = ratio - length_penalty
                if best is None or score > best[0]:
                    best = (score, start_idx, end_idx)
        if best is None or best[0] < 0.42:
            raise SystemExit(f"could not align segment {seg_id} phrase {text!r}; best={best}")
        _, src_a, src_b = best
        source_cursor = src_b
        mapped = list(range(src_a, src_b))
        start = min(float(src_spans[i]["start"]) for i in mapped)
        end = max(float(src_spans[i]["end"]) for i in mapped)
        if not start < end:
            raise SystemExit(f"invalid timing for segment {seg_id} phrase {text!r}; source={src_a}:{src_b}")
        records.append({
            "text": text,
            "start": start,
            "end": end,
            "source_char_start": src_a,
            "source_char_end": src_b,
            "text_match_ratio": round(best[0], 4),
        })
    return records
