#!/usr/bin/env python3
"""v1.1 T4+T5: Thai mark detection (overlays RETIRED 2026-09-24) + content QA.

T4 was the Layer1 ``thai_mark_fix`` compensation layer. C6 30-frame QA +
isolated render tests proved the overlay layer itself CAUSED tofu/float
(run-splitting isolates marks from their base); plain unsplit Prompt Bold
shapes stacked marks correctly on the current ffmpeg+libass build.
``build_overlays`` is now a no-op (ENABLE_OVERLAYS=False); detection
(``count_stacked``) is kept for QA reporting. Do NOT re-enable without
re-verifying on real burned frames.

Single-mark cues are a no-op (no overlays, base text unchanged) — proven
by the thai_mark_fix helpers this wraps (no reimplementation here).

(Historical mechanism, retired: the layer used to (1) hide the broken
inline mark glyph in the main line and (2) emit one Layer1 overlay
Dialogue per stacked cluster. Kept here for audit only.)

T5 content QA (fail or flag clearly, before burn):

- ``assert_no_overlap``: adjacent (sub-)cues must be monotonic
  (next.start >= prev.end - epsilon). Trimmable overlaps should go
  through ``script_timing.trim_overlaps`` first (``build_ass`` does
  this automatically); what the trim cannot save still refuses loudly.
- ``assert_no_zero_duration``: every cue has start < end.
- ``assert_all_fit``: re-export of the T1 width gate for QA call sites.
- ``count_stacked``: stacked-cluster count in a text (spot-check QA).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import thai_mark_fix as tmf  # noqa: E402  (wrapped, never reimplemented)

from script_timing import (  # noqa: E402  (trim lives here; re-exported)
    MIN_DURATION,
    OVERLAP_EPS,
    trim_overlaps,
)


def count_stacked(plain_text: str) -> int:
    """Number of stacked-mark clusters in *plain_text* (0 = no-op cue)."""
    return len(tmf.find_stacked_clusters(plain_text))


def build_overlays(
    plain_text: str,
    colored_text: str,
    pos_x: float,
    pos_y: float,
    fontsize: float,
    an: str,
    scale_tags: str,
) -> tuple[str, list[str]]:
    """Wrap ``thai_mark_fix.build_fixed_cue`` for one script (sub-)cue.

    *plain_text*: script piece (cluster detection).
    *colored_text*: same piece after ASS-escape + keyword colorize
    (identical char sequence + ``{\\c...}`` runs; overlay headers pick
    up the active keyword color per mark).
    Returns ``(main_text, overlay_payloads)`` — payloads still need the
    ``Dialogue: 1,start,end,...`` prefix + the cue's own timing.
    """
    return tmf.build_fixed_cue(
        plain_text, colored_text, pos_x, pos_y, fontsize, an, scale_tags
    )


def assert_no_zero_duration(matched: list[dict]) -> None:
    """Refuse cues with start >= end (zero/negative duration)."""
    for entry in matched:
        start = float(entry["start"])
        end = float(entry["end"])
        if not start < end:
            raise ValueError(
                f"zero-duration cue refused for line "
                f"{entry.get('line_index')}"
                + (
                    f" sub {entry.get('sub_index')}"
                    if entry.get("sub_count", 1) > 1
                    else ""
                )
                + f": start={start} end={end}"
            )


def assert_no_overlap(matched: list[dict], eps: float = OVERLAP_EPS) -> None:
    """Refuse overlapping adjacent cues (monotonic timeline).

    Compares in list order (the burn order). Reports EVERY overlap at
    once so Lip fixes them in one pass.
    """
    bad = []
    for prev, cur in zip(matched, matched[1:]):
        if float(cur["start"]) < float(prev["end"]) - eps:
            bad.append(
                f"line {cur.get('line_index')} starts "
                f"{float(cur['start']):.2f} < prev end "
                f"{float(prev['end']):.2f}"
            )
    if bad:
        raise ValueError(
            "overlapping cues refused "
            f"({len(bad)} overlap(s), eps={eps}s): " + "; ".join(bad)
        )


def content_qa(matched: list[dict]) -> dict:
    """Run all content QA checks (zero-duration + overlap). Pure report.

    Returns ``{"cues": N, "stacked_cues": M, "overlays": K}`` where K is
    the total Layer1 overlay count a burn would emit. Raises on refusal
    conditions (zero-duration / overlap) — no silent pass.
    """
    assert_no_zero_duration(matched)
    assert_no_overlap(matched)
    stacked_cues = 0
    overlays = 0
    for entry in matched:
        text = entry.get("script_line", "")
        n = count_stacked(text) if isinstance(text, str) else 0
        if n:
            stacked_cues += 1
            # retired 2026-09-24: detection kept for reporting, but no
            # Layer1 events are emitted while ENABLE_OVERLAYS is False
            if tmf.ENABLE_OVERLAYS:
                overlays += n
    return {"cues": len(matched), "stacked_cues": stacked_cues,
            "overlays": overlays}


#: Readability floors (SPEC_CUES.md): refuse, or pass with a recorded
#: Lip exception id carried on the cue ("exception") and listed in the
#: build's exceptions=[...].
MIN_CUE_FLOOR = 0.6
MAX_SYL_PER_CUE = 6
RATE_REFUSE_SYL_PER_S = 10.0
RATE_WARN_SYL_PER_S = 7.0

#: Named readability profiles: (min_cue_floor_s, max_syl_per_cue,
#: refuse_rate_syl_per_s). "general" is the strict default everywhere
#: (music/showcase clips). "vo" is the voiceover profile (Lip-approved
#: 2026-09-24 for full-auto VO burning): phrase-complete spoken cues
#: read fine at 7-8 syllables, so length alone is not a violation
#: there. AGY's rule stands: phrase-complete
#: beats short — the profile encodes it so Lip never has to sign each
#: long cue by hand. Exceptions still work on top of either profile.
PROFILES: dict[str, tuple[float, int, float]] = {
    "general": (MIN_CUE_FLOOR, MAX_SYL_PER_CUE, RATE_REFUSE_SYL_PER_S),
    "vo": (0.2, 10, 20.0),
}


def _cue_text(entry: dict) -> str:
    for key in ("script_line", "text", "display_text"):
        value = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(value, str) and value != "":
            return value
    return ""


def validate_final_cues(
    matched: list[dict],
    keywords: list[str] | None = None,
    exceptions: list[str] | None = None,
    profile: str = "general",
) -> dict:
    """Final gate on the EXACT cue list about to burn (SPEC_CUES.md).

    Runs AFTER the last timing change (trim) and checks post-rounding
    reality: centisecond-distinct spans, rounded monotonicity, width,
    duration/syllable/rate floors, negation integrity. Hard gates
    refuse loudly; readability floors refuse UNLESS the cue carries an
    "exception" id listed in *exceptions*. Trim-shortened cues
    (trimmed_end) below the duration floor WARN (audio-driven) instead
    of refusing — always reported, never silent.
    *profile* selects the readability floors ("general" strict default;
    "vo" voiceover profile for full-auto VO burning — see PROFILES).
    Returns {"cues": N, "warnings": [...]}. Raises ValueError on refuse.
    """
    from script_width import measure as _measure, budget_px as _budget
    from thai_line_split import count_syllables as _syl
    from script_keywords import NEGATIONS as _NEGS

    allowed = set(exceptions or [])
    budget = _budget()
    try:
        min_floor, max_syl, refuse_rate = PROFILES[profile]
    except KeyError:
        raise ValueError(
            f"unknown readability profile {profile!r} "
            f"(known: {sorted(PROFILES)})")
    refused: list[str] = []
    warnings: list[str] = []
    prev_re: float | None = None
    for n, entry in enumerate(matched):
        text = _cue_text(entry)
        start, end = float(entry["start"]), float(entry["end"])
        label = f"cue {n} ({text!r})"
        exc = entry.get("exception")
        has_exc = isinstance(exc, str) and exc in allowed
        # H3: post-centisecond reality
        rs, re = round(start, 2), round(end, 2)
        if not rs < re:
            refused.append(
                f"{label}: zero-length after centisecond rounding "
                f"[{start:.3f}, {end:.3f}]")
        if prev_re is not None and rs < prev_re:
            refused.append(
                f"{label}: rounded start {rs:.2f} < prev rounded end "
                f"{prev_re:.2f} (overlap after rounding)")
        prev_re = re
        # H2: width (build_ass also asserts; double-guard here is cheap)
        width = _measure(text) if text else 0.0
        if width > budget:
            refused.append(
                f"{label}: {width:.1f}px over budget {budget:.1f}px")
        # R1: duration floor (1e-6 float-dust tolerance — ASS resolves
        # centiseconds, so 0.5999999s counts as 0.6s)
        dur = end - start
        if dur < min_floor - 1e-6 and not has_exc:
            if entry.get("trimmed_end"):
                warnings.append(
                    f"{label}: {dur:.2f}s under {min_floor}s after "
                    "audio-driven trim — flagged for Lip review")
            else:
                refused.append(
                    f"{label}: {dur:.2f}s under {min_floor}s and no "
                    "Lip exception — merge it or record one")
        # R2/R3: syllables + speech rate
        syl = _syl(text) if text else 0
        if syl > max_syl and not has_exc:
            refused.append(
                f"{label}: {syl} syllables over cap {max_syl} "
                "and no Lip exception — split it or record one")
        rate = syl / dur if dur > 0 else float("inf")
        if rate > refuse_rate and not has_exc:
            refused.append(
                f"{label}: {rate:.1f} syl/s over refuse-rate "
                f"{refuse_rate} — unreadable, re-time it")
        elif rate > RATE_WARN_SYL_PER_S:
            warnings.append(
                f"{label}: {rate:.1f} syl/s over warn-rate "
                f"{RATE_WARN_SYL_PER_S} — fast, check against audio")
    # R4: negation integrity across adjacent cues (colorize only fixes
    # whole-phrase yellow INSIDE one cue — a stranded negation across a
    # cut must refuse here, where the cut can still be replanned)
    terms = sorted(
        [k for k in (keywords or []) if isinstance(k, str)],
        key=len, reverse=True)
    if terms:
        for prev, cur in zip(matched, matched[1:]):
            pt, ct = _cue_text(prev), _cue_text(cur)
            joined = pt + ct
            cut = len(pt)
            for term in terms:
                start_i = 0
                while True:
                    i = joined.find(term, start_i)
                    if i < 0:
                        break
                    j = i + len(term)
                    if i < cut < j:
                        refused.append(
                            f"cut between {pt!r} | {ct!r} severs keyword "
                            f"{term!r} — replan the cut")
                        break
                    for neg in _NEGS:
                        ns = i - len(neg)
                        if (ns >= 0 and joined[ns:i] == neg
                                and ns < cut <= i):
                            refused.append(
                                f"cut between {pt!r} | {ct!r} strands "
                                f"negation {neg!r} from {term!r} — "
                                "replan the cut")
                            break
                        ns_sp = i - len(neg) - 1
                        if (ns_sp >= 0
                                and joined[ns_sp:ns_sp + len(neg)] == neg
                                and joined[ns_sp + len(neg)] == " "
                                and ns_sp < cut <= i):
                            refused.append(
                                f"cut between {pt!r} | {ct!r} strands "
                                f"negation {neg!r} from {term!r} — "
                                "replan the cut")
                            break
                    start_i = i + 1
    # R5: protected-loanword integrity across adjacent cues. Same cut
    # technique as R4: a cut that severs a protected term (canonical or a
    # known misspelling variant) means the splitter trusted a garbage
    # tokenization — proven case 2026-09-29: กิมมิก (ก) tokenized to
    # กค/วาม and burned on screen as กิมมิกค/วามเท่. Refuse loudly with
    # the canonical spelling so the fix is obvious.
    from loanwords import entries as _loan_entries

    def _frag(entry):
        # Burned fragment text (display_text), NOT the parent script_line:
        # severing happens at within-line split points, which the parent
        # line text cannot see.
        for key in ("display_text", "text"):
            value = entry.get(key) if isinstance(entry, dict) else None
            if isinstance(value, str) and value != "":
                return value
        return ""
    loan_terms = []
    for entry in _loan_entries():
        if not entry.get("protect", True):
            continue
        canon = entry.get("canonical", "")
        seen = {canon}
        loan_terms.append((canon, canon))
        for variant in entry.get("variants", []) or []:
            if variant and variant not in seen:
                seen.add(variant)
                loan_terms.append((variant, canon))
    loan_terms.sort(key=lambda pair: len(pair[0]), reverse=True)
    if loan_terms:
        for prev, cur in zip(matched, matched[1:]):
            pt, ct = _frag(prev), _frag(cur)
            joined = pt + ct
            cut = len(pt)
            for term, canon in loan_terms:
                if not term:
                    continue
                start_i = 0
                hit = False
                while True:
                    i = joined.find(term, start_i)
                    if i < 0:
                        break
                    j = i + len(term)
                    if i < cut < j:
                        refused.append(
                            f"cut between {pt!r} | {ct!r} severs loanword "
                            f"{term!r} (canonical {canon!r}) — fix the "
                            f"spelling or replan the cut")
                        hit = True
                        break
                    start_i = i + 1
                if hit:
                    break
    if refused:
        raise ValueError(
            "final cue validation refused "
            f"({len(refused)} problem(s)): " + "; ".join(refused))
    return {"cues": len(matched), "warnings": warnings}
