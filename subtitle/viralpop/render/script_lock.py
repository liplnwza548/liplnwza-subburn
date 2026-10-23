#!/usr/bin/env python3
"""P1 script-lock (Codex review 2026-09-24): Lip's approved script is the
single source of wording. STT is timing-only.

- ``lock_script(path)`` -> {"lines", "text", "sha256"}: the approved
  wording + its hash. The hash travels with every build log so anyone
  can prove WHICH script a burn came from.
- ``diff_report(script_lines, stt_text)`` -> ordered mismatch list for
  Lip to adjudicate BEFORE burn. STT can be wrong too: a mismatch NEVER
  auto-rewrites the script — Lip picks keep-script or fix-script after
  listening. Returns [] when every script sentence appears in order.
- ``verify_coverage(script_text, cues)``: mechanical wording proof —
  every cue carries source_char_start/end into the script text; the
  spans must cover [0, len(script)) EXACTLY ONCE, in cue order.
  Raises CoverageError otherwise (dropped/duplicated/invented words
  cannot pass, even when cue text "looks right").

Cues built by hand tables must still pass verify_coverage: hand tables
are a timing/span exception, never a wording exception.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


class CoverageError(ValueError):
    """Cue spans do not cover the approved script exactly once."""


def lock_script(path: str | Path) -> dict:
    """Read the approved script file: lines + full text + sha256."""
    p = Path(path)
    lines = [
        ln.strip()
        for ln in p.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if not lines:
        raise CoverageError(f"approved script is empty: {p}")
    text = "".join(lines)
    digest = hashlib.sha256(
        "\n".join(lines).encode("utf-8")).hexdigest()
    return {"path": str(p), "lines": lines, "text": text, "sha256": digest}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def diff_report(script_lines: list[str], stt_text: str) -> list[dict]:
    """Ordered subsequence check: every script sentence must appear in
    the STT text, in order. Reports each break for Lip to adjudicate
    (keep-script vs fix-script after listening). Pure report, raises
    nothing; [] means STT covers the script in order (STT extras are
    listed separately, never auto-adopted)."""
    stt = _norm(stt_text)
    out: list[dict] = []
    cursor = 0
    for i, line in enumerate(script_lines):
        needle = _norm(line)
        if not needle:
            continue
        pos = stt.find(needle, cursor)
        if pos < 0:
            # maybe STT has it with different spacing/words: report raw
            out.append({
                "type": "script_not_found_in_stt",
                "line_index": i,
                "script": line,
                "note": "Lip decides after listening: keep script or fix it",
            })
        else:
            if pos > cursor:
                out.append({
                    "type": "stt_extra_before_match",
                    "line_index": i,
                    "stt_extra": stt_text,
                    "note": "STT has unmatched speech before this sentence",
                })
            cursor = pos + len(needle)
    return out


def verify_coverage(script_text: str, cues: list[dict]) -> dict:
    """Prove cue texts == script text exactly (spans cover once, ordered).

    Each cue needs int source_char_start/end; the cue's wording must
    equal script_text[start:end]. Coverage must be [0, len(script))
    with no gaps and no overlaps, in cue order.
    Returns {"cues": N, "chars": M}. Raises CoverageError on any break.
    """
    problems: list[str] = []
    cursor = 0
    for n, cue in enumerate(cues):
        text = cue.get("script_line") or cue.get("text") or ""
        a, b = cue.get("source_char_start"), cue.get("source_char_end")
        label = f"cue {n} ({text!r})"
        if not isinstance(a, int) or not isinstance(b, int):
            problems.append(f"{label}: missing int char span")
            continue
        if (a, b) != (cursor, cursor + len(text)):
            problems.append(
                f"{label}: span [{a}, {b}) breaks ordered coverage "
                f"(expected [{cursor}, {cursor + len(text)}))")
            continue
        if script_text[a:b] != text:
            problems.append(f"{label}: text != script[{a}:{b}]")
            continue
        cursor = b
    if cursor != len(script_text):
        problems.append(
            f"coverage ends at char {cursor}, script has "
            f"{len(script_text)} (gap or shortfall)")
    if problems:
        raise CoverageError(
            "wording-lock failed "
            f"({len(problems)} problem(s)): " + "; ".join(problems))
    return {"cues": len(cues), "chars": len(script_text)}
