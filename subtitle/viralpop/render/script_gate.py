#!/usr/bin/env python3
"""Ticket 03: pre-burn mismatch gate (ask all at once).

Single entrypoint:

    gate_before_burn(alignment) -> {"ok", "report", "mismatch_count"}

- ``alignment``: dict from ``script_align.align_script_to_timeline`` with
  ``"matched"`` and ``"mismatches"`` keys. Each mismatch carries
  ``line_index`` / ``script_line`` / ``time_hint`` / ``heard_snippet`` /
  ``reason``.
- If ``mismatches`` is non-empty: ``ok`` is False and ``report`` is one
  human-readable block listing ALL mismatches (bad line + time window +
  heard snippet each). The caller must NOT write ASS and must NOT burn.
- If ``mismatches`` is empty: ``ok`` is True and the pipeline may
  continue to the ASS build stage (ticket 02).

Writes nothing. Raises nothing on bad input shape — treats a missing or
malformed ``mismatches`` list as a gate failure with an explanatory
report instead of inventing alignment.
"""
from __future__ import annotations


class MismatchGateError(Exception):
    """Raised when burn is attempted while mismatches are unresolved.

    Carries the human-readable report in ``args[0]`` so callers (ASS
    builder, ffmpeg stage) refuse with the same message Lip sees.
    """


def format_time_hint(time_hint) -> str:
    """Render a mismatch time hint for humans (never used for timing)."""
    if time_hint is None:
        return "unknown time"
    try:
        return f"{float(time_hint):.2f}s"
    except (TypeError, ValueError):
        return "unknown time"


def format_mismatch_report(alignment: dict) -> str:
    """One human-readable block covering ALL mismatches together."""
    mismatches = alignment.get("mismatches", []) if isinstance(alignment, dict) else []
    lines = [
        f"SCRIPT-ALIGNMENT GATE: {len(mismatches)} mismatch(s) — burn REFUSED.",
        "Fix the script lines (or audio), re-align, then burn. No ASS was written.",
        "",
    ]
    for n, mm in enumerate(mismatches, 1):
        idx = mm.get("line_index", "?")
        if isinstance(idx, int):
            idx = idx + 1  # human 1-based line number
        script_line = mm.get("script_line", "")
        reason = mm.get("reason", "unknown")
        when = format_time_hint(mm.get("time_hint"))
        heard = mm.get("heard_snippet", "") or "(no audio context)"
        lines.append(
            f"[{n}] line {idx} {script_line!r} "
            f"(reason: {reason}, at: {when}): heard {heard!r}"
        )
    lines.append("")
    lines.append("Resolve every item above, then re-run alignment before burn.")
    return "\n".join(lines)


def gate_before_burn(alignment: dict) -> dict:
    """Decide whether the pipeline may proceed to ASS build / burn.

    Returns ``{"ok": bool, "report": str, "mismatch_count": int}``.
    ``report`` is the all-mismatches block when blocked, or an OK line
    when clear.
    """
    if not isinstance(alignment, dict) or not isinstance(
        alignment.get("mismatches"), list
    ):
        report = (
            "SCRIPT-ALIGNMENT GATE: unreadable alignment — burn REFUSED.\n"
            "Expected {'matched': [...], 'mismatches': [...]} from "
            "align_script_to_timeline. Re-run alignment before burn."
        )
        return {"ok": False, "report": report, "mismatch_count": -1}
    mismatches = alignment["mismatches"]
    if mismatches:
        return {
            "ok": False,
            "report": format_mismatch_report(alignment),
            "mismatch_count": len(mismatches),
        }
    n = len(alignment.get("matched", []) or [])
    return {
        "ok": True,
        "report": f"SCRIPT-ALIGNMENT GATE: OK — {n} cue(s) aligned, no mismatches. May build ASS.",
        "mismatch_count": 0,
    }


def assert_gate(alignment: dict) -> str:
    """Return the OK report, or raise MismatchGateError with the report.

    ASS builder and burn stages call this first so a non-empty
    mismatches list can never slip through to a written file.
    """
    result = gate_before_burn(alignment)
    if not result["ok"]:
        raise MismatchGateError(result["report"])
    return result["report"]
