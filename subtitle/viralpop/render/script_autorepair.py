#!/usr/bin/env python3
"""Automatic timing repair for sub-floor cues (runs AFTER merge_short_cues).

Batch1/batch2 proved every short cue falls into one of three shapes, always
fixed by hand the same way. This module applies those shapes automatically;
anything it cannot fix is reported as leftover (=> Lip exception, fail loud
downstream as before). Text is NEVER touched; words never move across cues.

Shapes (in order, first applicable wins):
  1. hold-back: previous cue ends before this cue starts (pause gap) ->
     move start back into the gap (display-hold into silence).
  2. hold-forward: next cue starts after this cue ends (pause gap), or this
     is the last cue (outro pause, capped by OUTRO_HOLD_MAX) -> move end
     forward into the gap.
  3. rebalance: same-line neighbor (prev or next) donates time across their
     shared boundary; both cues must stay >= min_floor + margin after.
Silence verification: when the word timeline is supplied, a hold is applied
only if no word overlaps the extended span (DECISIONS 2026-09-25: extend
into silence only). Without words, holds are limited to MICRO_HOLD (same
magnitude as the hand-approved batch1 rebalances) and anything bigger is
leftover.

Returns (matched, report) with report = {"repaired": [...], "leftover": [...]};
matched is mutated in place (same convention as driver apply_repairs).
Monotonicity is re-checked after every op; an op that would break it is
skipped and the cue becomes leftover.
"""
from __future__ import annotations

MICRO_HOLD = 0.15   # max hold without word-timeline proof (batch precedent)
OUTRO_HOLD_MAX = 1.2  # last-cue hold cap (proven: 1.00s C3 batch2)


def _overlaps_word(words, start, end):
    for w in words or []:
        try:
            ws, we = float(w.get("start", 0)), float(w.get("end", 0))
        except (TypeError, ValueError):
            continue
        if ws < end and we > start:
            return True
    return False


def _check_monotonic(cues):
    prev = -1.0
    for c in cues:
        s, e = float(c["start"]), float(c["end"])
        if not (s < e and round(s, 2) >= round(prev, 2)):
            return False
        prev = e
    return True


def _try_hold_back(cues, j, need, words):
    if j == 0:
        return None
    prev_end = float(cues[j - 1]["end"])
    start = float(cues[j]["start"])
    gap = start - prev_end
    if gap <= 0:
        return None
    take = min(gap, need)
    if words is not None:
        if _overlaps_word(words, start - take, start):
            return None
    elif take > MICRO_HOLD:
        return None
    cues[j]["start"] = round(start - take, 2)
    return ("hold-back", round(start - take, 2))


def _try_hold_forward(cues, j, need, words, is_last):
    end = float(cues[j]["end"])
    if j + 1 < len(cues):
        gap = float(cues[j + 1]["start"]) - end
    elif is_last:
        gap = OUTRO_HOLD_MAX
    else:
        return None
    if gap <= 0:
        return None
    take = min(gap, need)
    if words is not None:
        if _overlaps_word(words, end, end + take):
            return None
    elif take > MICRO_HOLD and j + 1 < len(cues):
        return None
    cues[j]["end"] = round(end + take, 2)
    return ("hold-forward", round(end + take, 2))


def _try_rebalance(cues, j, need, margin):
    cur = cues[j]
    line = cur.get("line_index")
    # Prefer next neighbor, then prev (matches hand tables: forward first).
    for nb_i, attr, sign in ((j + 1, "start", +1), (j - 1, "end", -1)):
        if not (0 <= nb_i < len(cues)):
            continue
        nb = cues[nb_i]
        if nb.get("line_index") != line:
            continue  # cross-line moves stay manual (true-boundary calls)
        if sign > 0:
            donor = float(nb["end"]) - float(nb["start"])
            if donor - need >= 0.5 + margin:
                cur["end"] = round(float(cur["end"]) + need, 2)
                nb["start"] = round(float(nb["start"]) + need, 2)
                return ("rebalance-forward", round(cur["end"], 2))
        else:
            donor = float(nb["end"]) - float(nb["start"])
            if donor - need >= 0.5 + margin:
                cur["start"] = round(float(cur["start"]) - need, 2)
                nb["end"] = round(float(nb["end"]) - need, 2)
                return ("rebalance-backward", round(cur["start"], 2))
    return None


def auto_repair(matched, words=None, min_floor=0.5, margin=0.03):
    """Repair sub-floor cues automatically. See module docstring."""
    if isinstance(words, dict):
        words = words.get("words") or []
    repaired, leftover = [], []
    for j, cue in enumerate(matched):
        dur = float(cue["end"]) - float(cue["start"])
        if dur >= min_floor - 1e-6:
            continue
        need = round(min_floor + margin - dur, 2)
        text = (cue.get("display_text") or cue.get("text") or "")[:24]
        applied = None
        snapshot = [(float(c["start"]), float(c["end"])) for c in matched]
        for shape in ("hold-back", "hold-forward", "rebalance"):
            for c, (s, e) in zip(matched, snapshot):
                c["start"], c["end"] = s, e
            if shape == "hold-back":
                got = _try_hold_back(matched, j, need, words)
            elif shape == "hold-forward":
                got = _try_hold_forward(matched, j, need, words,
                                        j == len(matched) - 1)
            else:
                got = _try_rebalance(matched, j, need, margin)
            if got is None:
                continue
            new_dur = float(matched[j]["end"]) - float(matched[j]["start"])
            if new_dur >= min_floor - 1e-6 and _check_monotonic(matched):
                applied = got
                break
        if applied is None:
            for c, (s, e) in zip(matched, snapshot):
                c["start"], c["end"] = s, e
            leftover.append(
                f"[{j}] L{cue.get('line_index')} {text!r} "
                f"{dur:.2f}s: no pause gap, no same-line donor — "
                f"needs hand table or Lip exception")
        else:
            op, edge = applied
            repaired.append(
                f"[{j}] L{cue.get('line_index')} {text!r} "
                f"{dur:.2f}s -> {op} to {edge:.2f}s")
    return matched, {"repaired": repaired, "leftover": leftover}
