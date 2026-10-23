#!/usr/bin/env python3
"""P3 slot subdivision (Codex review 2026-09-24): split an approved time
slot between cue pieces proportional to SPOKEN SYLLABLES — never
half-time.

Syllables are not equal length (stress, drag, pauses), so the result is
an ESTIMATE inside an evidence-backed slot, not word timing. Every span
it emits is flagged estimated=True by callers; real word-timestamp
boundaries win whenever they exist. Replaces ad-hoc half splits (which
packed 7 syllables into 0.78s on C6) and the width-proportional split
inside script_split for the cue-planning path (script_split keeps its
own behavior for the legacy path — one formula per path, no silent
mixing; see SPEC_CUES.md E1).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import thai_line_split as tls  # noqa: E402
from script_split import _subdivide_span  # noqa: E402 (shared span math)


def split_slot_by_syllables(
    pieces: list[str],
    start: float,
    end: float,
) -> list[tuple[float, float]]:
    """Subdivide [start, end) across pieces ~ spoken-syllable counts.

    Weight per piece = max(count_syllables(piece), 1) so empty/zero
    pieces can never collapse a span. Strictly increasing spans;
    raises ValueError on zero/negative slot (same spirit as
    _subdivide_span). ESTIMATE — callers must flag is_estimated.
    """
    if not pieces:
        raise ValueError("slot: no pieces to place")
    if not float(start) < float(end):
        raise ValueError(f"slot: invalid span [{start}, {end}]")
    weights = [max(tls.count_syllables(p), 1) for p in pieces]
    return _subdivide_span(float(start), float(end), weights)
