#!/usr/bin/env python3
"""Loanword whitelist + ASR spelling normalization (static skill data).

Data file: <skill-root>/loanword_dict.json
- ``normalize(text)`` maps every known variant spelling to its canonical form
  (longest match first). Idempotent on already-canonical text.
- ``protected_spans(text)`` returns (start, end) ranges that must never be
  cut through when splitting lines/cues.
- ``syllable_override(text)`` returns the true spoken syllable count for a
  span that exactly matches a table entry with an explicit count, else None.
- ``thai_number_reading(n)`` spells 0-999 in Thai words so digit runs can be
  syllable-counted by pronunciation instead of by digit characters.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TABLE_PATH = Path(__file__).resolve().parents[1] / "loanword_dict.json"

# A variant match fused with one of these on its right edge is part of a
# longer word (e.g. "โฮลเดอร" inside the already-correct "โฮลเดอร์") and
# must be skipped — replacing it would duplicate the mark ("เดอร์ร์").
TRAILING_FUSE = set("ัิีึืุูำ็่้๊๋์ํฺ")

_table: dict | None = None


def _load() -> dict:
    global _table
    if _table is None:
        _table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    return _table


def entries() -> list[dict]:
    data = _load()
    return list(data.get("loanwords", [])) + list(data.get("corrections", []))


def _variant_map() -> list[tuple[str, str]]:
    """(pattern, canonical) longest-first, INCLUDING identity pairs.

    Identity pairs (canonical->canonical) are load-bearing, not redundant:
    collection locks longer matches first, so "แมทช์" locks its span before
    the shorter variant "แมท" is considered — without this, "แมท" matches
    inside the already-correct "แมทช์" and duplicates a character
    ("แมทช์ช์"). Same class as the โฮลเดอร/โฮลเดอร์ hazard."""
    pairs: list[tuple[str, str]] = []
    for e in entries():
        pairs.append((e["canonical"], e["canonical"]))
        for v in e.get("variants", []):
            if v != e["canonical"]:
                pairs.append((v, e["canonical"]))
    # Stable: longest first; identity before variants at equal length.
    pairs.sort(key=lambda p: (len(p[0]), p[0] == p[1]), reverse=True)
    return pairs


def find_occurrences(text: str, variant: str) -> list[tuple[int, int]]:
    """Non-overlapping occurrences of variant, skipping fused matches.

    Thai variants use plain substring search but skip a hit whose next char
    is a combining mark (part of a longer word). Latin variants additionally
    require letter boundaries on both sides ("Metal" must not fire inside
    "Metals").
    """
    occs: list[tuple[int, int]] = []
    if re.search(r"[A-Za-z]", variant):
        # Letter boundaries, camelCase-aware: compacting the source fuses
        # "Methan Guard Holder" into "MethanGuardHolder", so a following
        # UPPERCASE letter also counts as a boundary (but a lowercase one
        # means mid-word: "metal" must not fire inside "metals"). Same for
        # a lowercase->Uppercase step on the left ("SuperMetal").
        # NOTE: (?-i:...) scoping is required because re.IGNORECASE would
        # otherwise make [a-z] match uppercase letters too. An all-CAPS
        # match ("GUARD" in "GUARDIAN") is exempt from the camelCase rule
        # and needs true non-letter boundaries on both sides.
        pat = re.compile(
            r"(?:(?<![A-Za-z])|(?<=(?-i:[a-z]))(?=(?-i:[A-Z])))"
            + re.escape(variant)
            + r"(?!(?-i:[a-z]))",
            re.IGNORECASE,
        )
        occs: list[tuple[int, int]] = []
        for m in pat.finditer(text):
            a, b = m.start(), m.end()
            if text[a:b].isupper():
                prev_ok = a == 0 or not text[a - 1].isalpha()
                next_ok = b == len(text) or not text[b].isalpha()
                if not (prev_ok and next_ok):
                    continue
            occs.append((a, b))
        return occs
    start = 0
    while True:
        idx = text.find(variant, start)
        if idx < 0:
            break
        end = idx + len(variant)
        if end >= len(text) or text[end] not in TRAILING_FUSE:
            occs.append((idx, end))
        start = idx + 1 if end >= len(text) or text[end] in TRAILING_FUSE else end
    return occs


def _collect(text: str) -> list[tuple[int, int, str]]:
    """All (start, end, canonical) occurrences, longest-first with locking:
    a span claimed by a longer pattern blocks shorter ones inside it."""
    occs: list[tuple[int, int, str]] = []
    for pattern, canonical in _variant_map():
        for a, b in find_occurrences(text, pattern):
            if not any(x < b and a < y for x, y, _ in occs):
                occs.append((a, b, canonical))
    return occs


def normalize(text: str) -> str:
    """Replace known variant spellings with canonical forms.

    After the generic variant map, apply owner-verified CONTEXTUAL
    corrections (loanword_dict.json ``contextual_corrections``). Each entry
    replaces a longer context-bearing pattern (never a bare word like
    "วันนี้"), so correct uses elsewhere are untouched. Idempotent.
    """
    out = text
    for a, b, canonical in sorted(_collect(out), reverse=True):
        if out[a:b] != canonical:
            out = out[:a] + canonical + out[b:]
    for e in _load().get("contextual_corrections", []):
        pat, rep = e["pattern"], e["replacement"]
        if pat in out:
            out = out.replace(pat, rep)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def contextual_corrections() -> list[dict]:
    """Owner-verified conditional phrase fixes with provenance (see table)."""
    return list(_load().get("contextual_corrections", []))


def override_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every occurrence whose syllable count is overridden
    (protected loanwords + plain overrides). Used for counting;
    only the protected subset also blocks line cuts."""
    terms: list[str] = []
    for e in _load().get("loanwords", []):
        if e.get("syllables") is not None:
            terms.append(e["canonical"])
    for e in _load().get("syllable_overrides", []):
        terms.append(e["canonical"])
    spans: list[tuple[int, int]] = []
    for term in sorted(set(terms), key=len, reverse=True):
        for idx, end in find_occurrences(text, term):
            if not any(a < end and idx < b for a, b in spans):
                spans.append((idx, end))
    spans.sort()
    return spans


def protected_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every protected loanword occurrence (longest first,
    no overlapping spans)."""
    terms = [e["canonical"] for e in _load().get("loanwords", []) if e.get("protect", True)]
    spans: list[tuple[int, int]] = []
    for term in sorted(terms, key=len, reverse=True):
        for idx, end in find_occurrences(text, term):
            if not any(a < end and idx < b for a, b in spans):
                spans.append((idx, end))
    spans.sort()
    return spans


def syllable_override(span_text: str) -> int | None:
    for e in _load().get("loanwords", []):
        if span_text == e["canonical"] and e.get("syllables") is not None:
            return int(e["syllables"])
    for e in _load().get("syllable_overrides", []):
        if span_text == e["canonical"]:
            return int(e["syllables"])
    return None


_ONES = ["ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]


def thai_number_reading(n: int) -> str:
    """Thai words for non-negative integers 0-999.

    Convention: leading หนึ่ง is omitted for hundreds (ร้อย not หนึ่งร้อย)
    to match natural spoken Thai. 101 is ร้อยเอ็ด (not หนึ่งร้อยเอ็ด).
    """
    if not isinstance(n, int) or n < 0 or n > 999:
        raise ValueError(f"out of supported range 0-999: {n}")
    if n < 10:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        tens_word = ("สิบ" if tens == 1
                      else "ยี่สิบ" if tens == 2
                      else _ONES[tens] + "สิบ")
        if ones == 0:
            return tens_word
        return tens_word + ("เอ็ด" if ones == 1 else _ONES[ones])
    # 100-999
    hundreds, remainder = divmod(n, 100)
    parts: list[str] = []
    if hundreds == 1:
        parts.append("ร้อย")
    else:
        parts.append(_ONES[hundreds] + "ร้อย")
    if remainder == 0:
        return parts[0]
    if remainder < 10:
        # Thai convention: trailing หนึ่ง becomes เอ็ด in compound numbers
        # (101=ร้อยเอ็ด, 201=สองร้อยเอ็ด, not *ร้อยหนึ่ง).
        parts.append("เอ็ด" if remainder == 1 else _ONES[remainder])
    else:
        tens, ones = divmod(remainder, 10)
        tens_word = ("สิบ" if tens == 1
                      else "ยี่สิบ" if tens == 2
                      else _ONES[tens] + "สิบ")
        if ones == 0:
            parts.append(tens_word)
        else:
            parts.append(tens_word + ("เอ็ด" if ones == 1 else _ONES[ones]))
    return "".join(parts)
