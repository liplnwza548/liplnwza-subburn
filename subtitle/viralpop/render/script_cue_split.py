#!/usr/bin/env python3
"""P2 cue planner (Codex review 2026-09-24): turn ONE approved script
sentence + its approved time slot into burnable cue pieces — or return
an UNRESOLVABLE verdict instead of forcing a bad plan.

Per-piece guarantees (SPEC_CUES.md):
  - width <= budget (word-boundary cuts via split_phrase_tracked)
  - spoken syllables <= max_syl (default 6, Lip 2026-09-24)
  - negation integrity: a cut never strands ไม่/ไม่ได้/ไม่ต้อง/อย่า/ห้าม
    from its keyword (checked at every junction, repaired by moving the
    cut; unresolvable when repair overflows the width)
  - durations from script_slot.split_slot_by_syllables (ESTIMATED,
    flagged is_estimated=True + method on every cue)
  - every piece >= min_duration (default 0.6s) AFTER the in-line merge
    pass; anything still short is UNRESOLVABLE, never force-merged
  - char spans subdivide the sentence window proportionally, so
    script_lock.verify_coverage() proves wording-lock mechanically

Returns (cues, problems): problems non-empty means STOP (burn nothing)
unless every problem carries a Lip exception. plan_cues() runs the
planner over a sentence list and aggregates.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import thai_line_split as tls  # noqa: E402
from script_keywords import NEGATIONS  # noqa: E402
from script_slot import split_slot_by_syllables  # noqa: E402
from script_timing import MIN_CUE_DURATION, merge_short_cues  # noqa: E402
from script_width import budget_px, ensure_init  # noqa: E402

#: Float-dust tolerance for duration gates (ASS resolves centiseconds;
#: 1e-6s can never show on screen). A true-0.6s span computing as
#: 0.5999999s still passes.
DUR_EPS = 1e-6


def _word_offsets(text: str) -> list[int]:
    """Character offsets of word-token boundaries in text."""
    offsets: list[int] = []
    pos = 0
    for tok in tls.tokenize_words(text):
        if not tok:
            continue
        i = text.find(tok, pos)
        if i < 0:
            break
        pos = i + len(tok)
        if 0 < pos < len(text):
            offsets.append(pos)
    return sorted(set(offsets))


def _evaluate_cuts(
    line: str,
    cuts: list[int],
    slot_start: float,
    slot_end: float,
    budget: float,
    max_syl: int,
    min_duration: float,
) -> tuple[list[str], list[tuple[float, float]]] | None:
    """Feasibility of one cut set: width + syllables + durations.

    Durations are syllable-proportional estimates (SPEC_CUES.md E1).
    Returns (pieces, spans) or None when any piece fails a gate.
    Float dust (e.g. 0.5999999s for a true 0.6s) never fails a gate:
    comparisons carry DUR_EPS. Cuts stranding a combining mark or a
    leading vowel are rejected outright (H4), even when they come from
    outside the word-boundary grid.
    """
    bounds = [0] + sorted(cuts) + [len(line)]
    for k in sorted(cuts):
        if line[k] in tls.COMBINING_MARKS or line[k - 1] in tls.LEADING_VOWELS:
            return None
    pieces = [line[s:e] for s, e in zip(bounds[:-1], bounds[1:])]
    if any(not p for p in pieces):
        return None
    if any(tls.width_px(p) > budget for p in pieces):
        return None
    syls = [tls.count_syllables(p) for p in pieces]
    if any(s > max_syl or s < 1 for s in syls):
        return None
    spans = split_slot_by_syllables(pieces, slot_start, slot_end)
    if any(e - s < min_duration - DUR_EPS for s, e in spans):
        return None
    return pieces, spans


def _search_cuts(
    line: str,
    slot_start: float,
    slot_end: float,
    budget: float,
    max_syl: int,
    min_duration: float,
    tried: list[str],
) -> tuple[list[str], list[tuple[float, float]]] | None:
    """Joint cut search (Codex P2+P3): fewest pieces wins, ties broken
    by the longest shortest-cue (most headroom above the floor).

    Tries the width-splitter layout first (proven path, loanword
    protection), then 1-cut and 2-cut word-boundary layouts. More than
    2 cuts (4+ pieces from one sentence) is declared UNRESOLVABLE by
    the caller — reword or widen the slot instead of confetti cues.
    """
    bounds0 = [0]
    pos = 0
    first_pieces, _forced = tls.split_phrase_tracked(line, budget)
    for p in first_pieces[:-1]:
        pos += len(p)
        bounds0.append(pos)
    candidates: list[list[int]] = [bounds0[1:]]
    word_cuts = _word_offsets(line)
    candidates.extend([k] for k in word_cuts)
    # syllable-boundary 1-cuts: the word grid sometimes hides the
    # calmer cut (C6: cut after "ที่" keeps ที่ with its clause and
    # drops the speech rate 7.5 -> 6.8). H4 (combining-mark/leading
    # vowel) is enforced inside _evaluate_cuts, so unsafe syllable
    # cuts die there, not here.
    syl_cuts = []
    spos = 0
    for tok in tls._syllable_tokens(line):
        spos += len(tok)
        if 0 < spos < len(line) and tok.strip():
            syl_cuts.append(spos)
    candidates.extend([k] for k in dict.fromkeys(syl_cuts))
    for a_i, a in enumerate(word_cuts):
        for b in word_cuts[a_i + 1:]:
            candidates.append([a, b])
    best = None
    best_key = None
    for cuts in candidates:
        if len(cuts) > 2 and cuts != bounds0[1:]:
            continue
        ev = _evaluate_cuts(
            line, cuts, slot_start, slot_end,
            budget, max_syl, min_duration)
        if ev is None:
            continue
        _pieces, spans = ev
        durs = [e - s for s, e in spans]
        syls = [tls.count_syllables(p) for p in _pieces]
        worst_rate = max(s / d for s, d in zip(syls, durs))
        key = (-len(_pieces), -worst_rate, min(durs))
        # Maximize: fewer pieces first, then CALMER peak speech rate,
        # then longer shortest-cue. Rate uses estimated durations, so
        # this prefers balanced syllable distribution, not truth.
        if best_key is None or key > best_key:
            best_key, best = key, ev
    if best is not None:
        tried.append(
            f"cut search chose {len(best[0])} piece(s) "
            f"(min dur {min(e - s for _, (s, e) in zip(best[0], best[1])):.2f}s)")
    return best


def _forbidden_cut_intervals(
    line: str, keywords: list[str]
) -> list[tuple[int, int, str]]:
    """Char intervals a cut must avoid: keyword spans and
    [negation-start, keyword-end] spans (negation adjacent or 1 space)."""
    terms = sorted(
        [k for k in (keywords or []) if k], key=len, reverse=True)
    intervals: list[tuple[int, int, str]] = []
    for term in terms:
        start = 0
        while True:
            i = line.find(term, start)
            if i < 0:
                break
            j = i + len(term)
            intervals.append((i, j, f"keyword {term!r}"))
            for neg in NEGATIONS:
                if line[max(0, i - len(neg)):i] == neg:
                    intervals.append(
                        (i - len(neg), j,
                         f"negation phrase {neg}{term}"))
                    break
                if (i - len(neg) - 1 >= 0
                        and line[i - len(neg) - 1:i - 1] == neg
                        and line[i - 1] == " "):
                    intervals.append(
                        (i - len(neg) - 1, j,
                         f"negation phrase {neg} {term}"))
                    break
            start = i + 1
    return intervals


def _repair_negation_cuts(
    line: str, cuts: list[int], keywords: list[str], budget: float,
    tried: list[str],
) -> tuple[list[int], list[dict]]:
    """Snap cuts out of forbidden intervals (keyword/negation spans).

    Prefer the interval start (keeps the keyword whole in the next
    piece); fall back to the interval end. Rechecks widths after every
    move; unresolvable when a repair overflows the budget.
    """
    problems: list[dict] = []
    cuts = sorted(cuts)
    for a, b, why in _forbidden_cut_intervals(line, keywords):
        for n, k in enumerate(cuts):
            if a < k < b:
                moved = a if a > 0 else b
                if moved == k:
                    continue
                trial = sorted(cuts[:n] + [moved] + cuts[n + 1:])
                bounds = [0] + trial + [len(line)]
                widths = [tls.width_px(line[s:e])
                          for s, e in zip(bounds[:-1], bounds[1:])]
                if all(w <= budget for w in widths):
                    tried.append(
                        f"moved cut {k}->{moved} to protect {why}")
                    cuts = trial
                else:
                    problems.append({
                        "type": "unresolvable",
                        "reason": f"cut at {k} breaks {why} and repair "
                        "overflows the width budget — needs Lip exception",
                        "tried": list(tried) + [f"repair cut {k}"],
                    })
    return cuts, problems


def plan_sentence(
    line: str,
    slot_start: float,
    slot_end: float,
    line_index: int,
    char_a: int,
    char_b: int,
    *,
    budget: float | None = None,
    keywords: list[str] | None = None,
    max_syl: int = 6,
    min_duration: float = MIN_CUE_DURATION,
) -> tuple[list[dict], list[dict]]:
    """Plan burnable cues for one approved sentence in its slot.

    Returns (cues, problems). cues carry source_char_start/end (for
    script_lock.verify_coverage), is_estimated=True + method
    "syllable-proportional" on every cue. problems non-empty => STOP.
    """
    ensure_init()
    budget = budget_px() if budget is None else float(budget)
    tried: list[str] = []
    problems: list[dict] = []
    if not float(slot_start) < float(slot_end):
        return [], [{"type": "unresolvable",
                     "reason": f"line {line_index}: invalid slot",
                     "tried": tried}]
    found = _search_cuts(
        line, float(slot_start), float(slot_end),
        budget, max_syl, min_duration, tried)
    if found is None:
        return [], [{
            "type": "unresolvable",
            "reason": f"line {line_index}: {line!r} has no feasible "
            f"cut (width {budget:.0f}px / {max_syl} syl / "
            f"{min_duration}s) in this slot — widen the slot, reword, "
            "or record a Lip exception",
            "tried": list(tried) + ["width layout", "1-cut + 2-cut search"],
        }]
    pieces, spans = found
    cuts: list[int] = []
    pos = 0
    for p in pieces[:-1]:
        pos += len(p)
        cuts.append(pos)
    cuts, neg_problems = _repair_negation_cuts(
        line, cuts, keywords or [], budget, tried)
    problems.extend(neg_problems)
    # rebuild pieces from (possibly repaired) cuts + recheck durations
    bounds = [0] + sorted(cuts) + [len(line)]
    pieces = [line[s:e] for s, e in zip(bounds[:-1], bounds[1:])]
    spans = split_slot_by_syllables(pieces, slot_start, slot_end)
    if any(e - s < min_duration - DUR_EPS for s, e in spans):
        return [], [{
            "type": "unresolvable",
            "reason": f"line {line_index}: negation repair pushed a "
            f"piece under {min_duration}s — needs Lip exception",
            "tried": list(tried) + ["negation repair"],
        }]
    # char spans proportional to piece char length (traceability)
    char_bounds = [char_a]
    acc = 0
    total_len = max(sum(len(p) for p in pieces), 1)
    for p in pieces[:-1]:
        acc += len(p)
        char_bounds.append(
            char_a + round((char_b - char_a) * acc / total_len))
    char_bounds.append(char_b)
    cues = []
    for sub_i, (piece, (s, e)) in enumerate(zip(pieces, spans)):
        cues.append({
            "line_index": line_index,
            "script_line": piece,
            "text": piece,
            "display_text": piece,
            "parent_script_line": line,
            "sub_index": sub_i,
            "sub_count": len(pieces),
            "start": s,
            "end": e,
            "source_char_start": char_bounds[sub_i],
            "source_char_end": char_bounds[sub_i + 1],
            "is_estimated": True,
            "timing_method": "syllable-proportional",
        })
    # in-line merge pass (same sentence only): short pieces fold forward
    merged, report = merge_short_cues(
        cues, min_duration=min_duration, max_width_px=budget,
        max_syl=max_syl, allow_cross_line=False)
    for skipped_text, reason in report["skipped"]:
        problems.append({
            "type": "unresolvable",
            "reason": f"line {line_index}: {skipped_text!r} under "
            f"{min_duration}s but merge refused ({reason}) — needs "
            "Lip exception",
            "tried": tried + ["in-line merge"],
        })
    for m in report["merged"]:
        tried.append(f"merged short piece -> {m!r}")
    return merged, problems


def plan_cues(
    sentences: list[dict],
    *,
    budget: float | None = None,
    keywords: list[str] | None = None,
    max_syl: int = 6,
    min_duration: float = MIN_CUE_DURATION,
) -> tuple[list[dict], list[dict]]:
    """Plan a full cue list. sentences: [{line, start, end, line_index,
    char_a, char_b}]. Returns (cues, problems) aggregated in order."""
    cues: list[dict] = []
    problems: list[dict] = []
    for s in sentences:
        sub, sub_problems = plan_sentence(
            s["line"], float(s["start"]), float(s["end"]),
            int(s["line_index"]), int(s["char_a"]), int(s["char_b"]),
            budget=budget, keywords=keywords, max_syl=max_syl,
            min_duration=min_duration)
        cues.extend(sub)
        problems.extend(sub_problems)
    return cues, problems
