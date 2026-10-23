#!/usr/bin/env python3
"""v1.1 T3: keyword highlight from SCRIPT text (same-size yellow).

Display text still comes from the script ONLY; STT is still timing-only.
This module only weaves ``{\\c...}`` color runs around matching
substrings of SCRIPT text — it never rewrites wording, never changes
fontsize, never emits ``\\N``.

Proven tag shape (old Viral Pop path, render/build_clip.py)::

    text.replace(term, r"{\\c&H00F0FF&}" + term + r"{\\c&HFFFFFF&}")

Applied AFTER T2 split (script_ass colorizes each sub-cue piece
independently): a keyword that spans a cut is colorized within each
piece separately — pieces are never merged for color.

Sources: explicit ``keywords=[...]`` list, or a per-clip job file
(``jobs/clipNN.json`` ``"keywords"`` array) via ``load_keywords_job``.
Empty list = no yellow (valid, e.g. clips without keywords).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: Same-size yellow highlight (old proven tag) + back-to-white closer.
YELLOW_OPEN = r"{\c&H00F0FF&}"
YELLOW_CLOSE = r"{\c&HFFFFFF&}"

#: Any color override run, used by tests/QA to detect yellow presence.
COLOR_RUN_RE = re.compile(r"\{\\c&H[0-9A-Fa-f]+&\}")

#: Override-tag stripper (QA: recover plain on-screen wording).
TAG_RE = re.compile(r"\{[^}]*\}")


def load_keywords_job(job_json: str | Path) -> list[str]:
    """Load the ``keywords`` array from a per-clip job file.

    Returns [] when the file has no keywords (valid: no yellow).
    Raises FileNotFoundError / ValueError on missing file / bad shape.
    """
    path = Path(job_json)
    if not path.is_file():
        raise FileNotFoundError(f"job file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"job file unreadable: {path}: {exc}") from exc
    keywords = data.get("keywords", []) if isinstance(data, dict) else None
    if keywords is None:
        return []
    if not isinstance(keywords, list) or not all(
        isinstance(k, str) for k in keywords
    ):
        raise ValueError(f"job file keywords must be a string array: {path}")
    return [k for k in keywords if k]


def normalize_keywords(keywords: list[str] | None) -> list[str]:
    """Drop blanks, keep order (longest-first sorting happens at use)."""
    return [k for k in (keywords or []) if isinstance(k, str) and k]


def check_keyword_hits(script_lines, keywords) -> dict:
    """Count verbatim substring hits per keyword across script lines.

    A keyword with zero hits burns zero yellow with no error downstream —
    callers fail loud on the zeros instead of shipping a keyword-less batch
    (proven near-miss class: batch1 char-split + renaming mid-batch).
    Matching is verbatim substring, same as the highlighter.
    """
    corpus = "\n".join(str(line) for line in (script_lines or []))
    return {kw: corpus.count(kw) for kw in (keywords or [])}


#: Thai negation / prohibition particles: when one sits immediately before
#: a keyword (optionally one space), the highlight must cover the WHOLE
#: phrase — a lone yellow "ปวดไหล่" inside white "ไม่ปวดไหล่" reads as a
#: product flaw at glance speed (C6 QA 2026-09-24). Longest first.
NEGATIONS = ("ไม่ได้", "ไม่ต้อง", "ไม่", "อย่า", "ห้าม")


def _split_runs(text: str) -> list[tuple[str, str]]:
    """Split ASS text into ("tag", "{...}") / ("plain", "...") runs.

    Matching only runs against plain runs, so a shorter keyword can never
    match inside an already-inserted color run (no nested double-wrap).
    """
    runs: list[tuple[str, str]] = []
    pos = 0
    for m in TAG_RE.finditer(text):
        if m.start() > pos:
            runs.append(("plain", text[pos:m.start()]))
        runs.append(("tag", m.group(0)))
        pos = m.end()
    if pos < len(text):
        runs.append(("plain", text[pos:]))
    return runs


def _find_spans(plain: str, terms: list[str]) -> list[tuple[int, int]]:
    """Non-overlapping (start, end) match spans in one plain run.

    Longest terms first; each accepted span may extend left over an
    immediately-adjacent negation particle (NEGATIONS + optional space).
    Greedy: an accepted span blocks any overlapping shorter match.
    """
    accepted: list[tuple[int, int]] = []

    def blocked(s: int, e: int) -> bool:
        return any(s < ae and e > aas for aas, ae in accepted)

    for term in sorted(terms, key=len, reverse=True):
        start = 0
        while True:
            i = plain.find(term, start)
            if i < 0:
                break
            s, e = i, i + len(term)
            for neg in NEGATIONS:
                ns = s - len(neg)
                if ns >= 0 and plain[ns:s] == neg:
                    s = ns
                    break
                ns_sp = s - len(neg) - 1
                if (ns_sp >= 0 and plain[ns_sp:ns_sp + len(neg)] == neg
                        and plain[ns_sp + len(neg)] == " "):
                    s = ns_sp
                    break
            if not blocked(s, e):
                accepted.append((s, e))
            start = i + 1
    return sorted(accepted)


def colorize(text: str, keywords: list[str] | None) -> str:
    """Weave yellow runs around keyword substrings of SCRIPT text.

    Longest terms first (so a short term inside a longer one does not
    steal the match). A keyword immediately preceded by a negation
    particle highlights the whole phrase. Matching is tag-aware: never
    matches inside already-inserted runs. Pure substring match on the
    script piece — never touches STT. Size unchanged.
    """
    terms = normalize_keywords(keywords)
    if not terms:
        return text
    out: list[str] = []
    for kind, chunk in _split_runs(text):
        if kind == "tag":
            out.append(chunk)
            continue
        spans = _find_spans(chunk, terms)
        pos = 0
        for s, e in spans:
            out.append(chunk[pos:s])
            out.append(YELLOW_OPEN + chunk[s:e] + YELLOW_CLOSE)
            pos = e
        out.append(chunk[pos:])
    return "".join(out)


def strip_tags(colored: str) -> str:
    """Remove ``{...}`` override runs → plain on-screen wording."""
    return TAG_RE.sub("", colored)


def has_yellow(colored: str) -> bool:
    """True when a yellow highlight run is present."""
    return YELLOW_OPEN in colored


def yellow_dialogue_count(ass_text: str) -> int:
    """Count Dialogue lines carrying a yellow run (coverage QA)."""
    return sum(
        1
        for line in ass_text.splitlines()
        if line.startswith("Dialogue:") and YELLOW_OPEN in line
    )
