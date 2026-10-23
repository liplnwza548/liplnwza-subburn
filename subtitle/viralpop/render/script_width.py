#!/usr/bin/env python3
"""v1.1 T1: measured width gate for script-aligned cues (no split here).

Width physics (proven by the old Viral Pop path — build_clip.py):
  budget_px = (playres_w - marginL - marginR) * SAFETY (0.94 headroom
  for 3px outline + 103% pop overshoot).

Measurement instrument: render/thai_line_split.width_px (HarfBuzz shaping
of the real Prompt-Bold font, NOT char count). Geometry here mirrors
render/script_ass.py PROVEN batch01 values (1080 / fontsize 104 /
margins 47/47 -> budget 926.84px). T5 locked both together (a test pins
them equal): if Lip ever re-signs geometry, BOTH modules move together.

FAIL-LOUD: assert_fit() raises WidthOverflowError listing EVERY over line
(per-line px + worst) BEFORE any burn. Split lives in script_split.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import thai_line_split as tls  # noqa: E402

#: Proven batch01 geometry mirror (T5 lock — must equal script_ass values,
#: pinned by test).
PLAYRES_W = 1080
FONTSIZE = 104
MARGIN_L = 47
MARGIN_R = 47

#: Proven safety factor from build_clip.py (0.94).
SAFETY = 0.94

#: Default font (bundled Prompt Bold — same file script_ass renders).
DEFAULT_FONT = Path(__file__).resolve().parents[1] / "fonts" / "Prompt-Bold.ttf"

_inited_key: tuple | None = None


class WidthOverflowError(Exception):
    """One or more (sub-)cue lines exceed the measured width budget.

    Carries the human-readable per-line report in ``args[0]``.
    """


def budget_px(
    playres_w: float = PLAYRES_W,
    margin_l: float = MARGIN_L,
    margin_r: float = MARGIN_R,
    safety: float = SAFETY,
) -> float:
    """Skill-proven width budget in px."""
    return (playres_w - margin_l - margin_r) * safety


def ensure_init(font_path=None, fontsize: float | None = None) -> None:
    """Init the HarfBuzz shaper once (idempotent per font+size)."""
    global _inited_key
    font_path = str(font_path or DEFAULT_FONT)
    fontsize = FONTSIZE if fontsize is None else fontsize
    key = (font_path, float(fontsize))
    if _inited_key == key:
        return
    if not Path(font_path).is_file():
        raise WidthOverflowError(f"FONT_MISSING for width measurement: {font_path}")
    tls.init(font_path, float(fontsize))
    _inited_key = key


def measure(text: str) -> float:
    """Rendered width of *text* in px (HarfBuzz, Prompt Bold @ FONTSIZE)."""
    ensure_init()
    return tls.width_px(text)


def _entry_text(entry) -> str:
    if isinstance(entry, str):
        return entry
    for key in ("script_line", "text", "display_text"):
        value = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(value, str) and value != "":
            return value
    raise ValueError(f"width gate: entry has no cue text: {entry!r}"[:160])


def measure_report(entries: list, budget: float | None = None) -> dict:
    """Per-line px widths + over list. Pure measurement, never raises."""
    ensure_init()
    budget = budget_px() if budget is None else float(budget)
    lines = []
    for i, entry in enumerate(entries):
        text = _entry_text(entry)
        width = tls.width_px(text)
        idx = entry.get("line_index", i) if isinstance(entry, dict) else i
        sub = entry.get("sub_index") if isinstance(entry, dict) else None
        lines.append({
            "index": idx,
            "sub_index": sub,
            "text": text,
            "width_px": round(width, 1),
            "budget_px": round(budget, 1),
            "over": width > budget,
        })
    over = [ln for ln in lines if ln["over"]]
    worst = max((ln["width_px"] for ln in lines), default=0.0)
    return {
        "budget_px": round(budget, 1),
        "worst_px": worst,
        "line_count": len(lines),
        "over_count": len(over),
        "lines": lines,
        "over": over,
    }


def format_report(report: dict) -> str:
    """Human-readable width report (all over lines listed at once)."""
    out = [
        f"WIDTH GATE: budget {report['budget_px']}px, "
        f"{report['line_count']} line(s), worst {report['worst_px']}px, "
        f"{report['over_count']} over.",
    ]
    for ln in report["lines"]:
        flag = "OVER" if ln["over"] else "ok"
        sub = f"#{ln['sub_index']}" if ln.get("sub_index") not in (None,) else "-"
        out.append(
            f"  [{flag}] line {ln['index']} sub {sub} "
            f"{ln['width_px']}px: {ln['text']!r}"
        )
    return "\n".join(out)


def assert_fit(entries: list, budget: float | None = None) -> str:
    """FAIL-LOUD: raise WidthOverflowError when ANY line exceeds budget.

    Returns the OK report string when everything fits (so callers can log
    the per-line numbers even on success).
    """
    report = measure_report(entries, budget=budget)
    if report["over_count"]:
        raise WidthOverflowError(format_report(report))
    return format_report(report)
