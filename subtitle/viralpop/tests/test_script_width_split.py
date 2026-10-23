#!/usr/bin/env python3
"""v1.1 T1+T2 tests: measured width gate + within-line width-safe split.

T1: budget_px == (1080-60-60)*0.94; per-line px report; FAIL-LOUD
    (WidthOverflowError) on a deliberately long line.
T2: over-budget script lines split ONLY within the line at Thai word
    boundaries; sub-cue timings subdivided from the line's own STT span
    (monotonic, no zero-duration); concat(sub-texts) == script line
    exactly; every sub-cue <= budget; primary-bag short lines unsplit.

Run from the skill root:

    python3 tests/test_script_width_split.py
    # or
    python3 -m unittest tests.test_script_width_split -v

Stdlib unittest only.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

import thai_line_split as tls  # noqa: E402
from script_align import align_script_to_timeline  # noqa: E402
from script_ass import FONTSIZE as ASS_FONTSIZE  # noqa: E402
from script_ass import MARGIN_L as ASS_MARGIN_L  # noqa: E402
from script_ass import MARGIN_R as ASS_MARGIN_R  # noqa: E402
from script_ass import PLAYRES_X as ASS_PLAYRES_X  # noqa: E402
from script_ass import build_ass  # noqa: E402
from script_split import split_alignment, split_matched  # noqa: E402
from script_width import (  # noqa: E402
    FONTSIZE,
    MARGIN_L,
    MARGIN_R,
    PLAYRES_W,
    WidthOverflowError,
    assert_fit,
    budget_px,
    ensure_init,
    measure_report,
)


def W(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


def dialogue_texts(ass_text: str) -> list[str]:
    texts = []
    for line in ass_text.splitlines():
        if line.startswith("Dialogue:"):
            payload = line.split(",", 9)[-1]
            texts.append(re.sub(r"^\{[^}]*\}", "", payload))
    return texts


#: A deliberately over-budget script line (real clip04-adjacent wording,
#: repeated until it cannot fit 926.84px at 104pt Prompt Bold).
LONG_LINE = (
    "กระเป๋าหนังสีดำที่แมตช์ง่ายกับทุกลุคแนะนำใบนี้เลยค่ะสวยแพงมาก"
)


class TestT1Budget(unittest.TestCase):
    def test_budget_formula(self):
        self.assertAlmostEqual(budget_px(), (1080 - 47 - 47) * 0.94, places=6)
        self.assertAlmostEqual(budget_px(), 926.84, places=6)

    def test_geometry_matches_ass(self):
        # Measurement must use the SAME geometry the ASS renders, or the
        # gate guarantees nothing. T5 moves both together.
        self.assertEqual(PLAYRES_W, ASS_PLAYRES_X)
        self.assertEqual(FONTSIZE, ASS_FONTSIZE)
        self.assertEqual(MARGIN_L, ASS_MARGIN_L)
        self.assertEqual(MARGIN_R, ASS_MARGIN_R)


class TestT1Gate(unittest.TestCase):
    def test_short_primary_lines_fit(self):
        ok = assert_fit(["สวยแพงมาก", "ทรงสวย", "โทนสีดำคลาสสิก"])
        self.assertIn("0 over", ok)

    def test_report_lists_per_line_px(self):
        report = measure_report(["สวยแพงมาก", LONG_LINE])
        self.assertEqual(report["budget_px"], 926.8)  # rounded to 0.1px
        self.assertEqual(report["line_count"], 2)
        self.assertEqual(report["over_count"], 1)
        self.assertTrue(report["lines"][0]["width_px"] > 0)
        self.assertLessEqual(report["lines"][0]["width_px"], 926.84)
        self.assertGreater(report["lines"][1]["width_px"], 926.84)
        self.assertTrue(report["lines"][1]["over"])
        self.assertFalse(report["lines"][0]["over"])

    def test_long_line_trips_gate_fail_loud(self):
        with self.assertRaises(WidthOverflowError) as ctx:
            assert_fit(["สวยแพงมาก", LONG_LINE])
        msg = str(ctx.exception)
        self.assertIn("OVER", msg)
        self.assertIn(LONG_LINE, msg)

    def test_build_ass_refuses_over_budget_before_write(self):
        import tempfile

        words = [W(LONG_LINE, 0.0, 5.0)]
        alignment = align_script_to_timeline([LONG_LINE], words)
        self.assertEqual(alignment["mismatches"], [])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub.ass"
            # Codex review 2026-09-24: final validation fires BEFORE the
            # legacy width assert (same refusal — nothing written).
            with self.assertRaises(ValueError) as ctx:
                build_ass(alignment, target)
            self.assertIn("over budget", str(ctx.exception))
            self.assertFalse(target.exists())


class TestT2Split(unittest.TestCase):
    def _aligned_long(self):
        words = [W(LONG_LINE, 1.0, 6.0)]
        alignment = align_script_to_timeline([LONG_LINE], words)
        self.assertEqual(alignment["mismatches"], [])
        return alignment

    def test_long_line_splits_and_concat_is_exact(self):
        alignment = split_alignment(self._aligned_long())
        pieces = [e["script_line"] for e in alignment["matched"]]
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), LONG_LINE)

    def test_every_sub_cue_fits_budget(self):
        alignment = split_alignment(self._aligned_long())
        report = measure_report(alignment["matched"])
        self.assertEqual(report["over_count"], 0, msg=report["over"])

    def test_timings_monotonic_inside_parent_span_no_zero_duration(self):
        alignment = split_alignment(self._aligned_long())
        subs = alignment["matched"]
        self.assertAlmostEqual(subs[0]["start"], 1.0, places=6)
        self.assertAlmostEqual(subs[-1]["end"], 6.0, places=6)
        for i, sub in enumerate(subs):
            self.assertLess(sub["start"], sub["end"],
                            f"zero-duration sub-cue {i}: {sub!r}")
            if i:
                self.assertGreaterEqual(sub["start"], subs[i - 1]["end"] - 1e-6)
                self.assertGreater(sub["end"], subs[i - 1]["end"])

    def test_no_cross_line_moves(self):
        script = ["สวยแพงมาก", LONG_LINE, "ทรงสวย"]
        words = [W("สวยแพงมาก", 0.0, 0.8), W(LONG_LINE, 1.0, 6.0),
                 W("ทรงสวย", 6.5, 7.2)]
        alignment = align_script_to_timeline(script, words)
        self.assertEqual(alignment["mismatches"], [])
        split = split_alignment(alignment)
        # Group pieces by parent line; each group's concat == that line.
        groups: dict[int, list[str]] = {}
        for entry in split["matched"]:
            groups.setdefault(entry["line_index"], []).append(entry["script_line"])
        self.assertEqual(set(groups), {0, 1, 2})
        for idx, line in enumerate(script):
            self.assertEqual("".join(groups[idx]), line)
        # Short lines pass through as exactly one cue each.
        self.assertEqual(groups[0], ["สวยแพงมาก"])
        self.assertEqual(groups[2], ["ทรงสวย"])

    def test_split_points_are_word_safe(self):
        alignment = split_alignment(self._aligned_long())
        for entry in alignment["matched"]:
            piece = entry["script_line"]
            self.assertTrue(piece)
            self.assertNotIn(piece, ("ก",))
            self.assertGreater(len(piece), 1)
            if entry["sub_index"]:
                self.assertNotIn(piece[0], tls.COMBINING_MARKS)

    def test_split_entries_carry_parent_bookkeeping(self):
        alignment = split_alignment(self._aligned_long())
        n = len(alignment["matched"])
        for i, entry in enumerate(alignment["matched"]):
            self.assertEqual(entry["parent_script_line"], LONG_LINE)
            self.assertEqual(entry["sub_index"], i)
            self.assertEqual(entry["sub_count"], n)

    def test_primary_bag_lines_stay_single_cue(self):
        script = ["สวยแพงมาก", "ทรงสวย"]
        words = [W("สวยแพง", 0.0, 0.5), W("มาก", 0.5, 0.8),
                 W("ทรงสวย", 0.9, 1.6)]
        alignment = align_script_to_timeline(script, words)
        split = split_alignment(alignment)
        self.assertEqual([e["script_line"] for e in split["matched"]], script)
        self.assertTrue(all(e["sub_count"] == 1 for e in split["matched"]))
        self.assertEqual(dialogue_texts(build_ass(split)), script)

    def test_split_output_burns_one_dialogue_per_sub_cue(self):
        # v1.1 T4: Layer0 = one Dialogue per sub-cue; stacked-mark cues
        # additionally emit Layer1 overlays. Strip ALL override runs
        # (header + mark-hide spans) to recover on-screen wording.
        alignment = split_alignment(self._aligned_long())
        ass_text = build_ass(alignment)
        layer0 = [ln for ln in ass_text.splitlines()
                  if ln.startswith("Dialogue: 0,")]
        texts = [re.sub(r"\{[^}]*\}", "", ln.split(",", 9)[-1])
                 for ln in layer0]
        self.assertEqual("".join(texts), LONG_LINE)
        self.assertEqual(len(texts), len(alignment["matched"]))
        self.assertNotIn(r"\N", ass_text)


class TestC6NoDictFallback(unittest.TestCase):
    """C6 2026-09-25: prod boxes without pythainlp ran the regex-fallback
    cutter, which severed เซ็ต -> เซ็ | ต mid-word (Thai has no spaces,
    so a syllable/class-only grid is not a word grid). Backend-independent:
    each test forces fallback by monkeypatching tls._tokenizer to None."""

    C6_LINE = "เข้ากับเสื้อผ้าได้ทุกเซ็ต สะพายขึ้นไหล่ปุ๊บ"

    def _no_dict(self):
        ensure_init()
        saved = tls._tokenizer
        tls._tokenizer = None
        self.addCleanup(setattr, tls, "_tokenizer", saved)

    def test_c6_line_fails_loud_without_dictionary(self):
        # The C6 run 'เข้ากับเสื้อผ้าได้ทุกเซ็ต' (969.7px) overflows on its
        # own with NO whitespace inside: without a dictionary there is no
        # provable word cut, so the cutter must REFUSE (old code severed
        # เซ็ต -> เซ็ | ต here). Remedy is in the error: install pythainlp.
        self._no_dict()
        with self.assertRaises(tls.NoSafeSplitError) as ctx:
            tls.split_phrase_tracked(self.C6_LINE, budget_px())
        self.assertIn("pythainlp", str(ctx.exception))

    def test_space_grid_still_splits_when_halves_fit(self):
        # Fallback CAN split: 'X X' cuts at the whitespace gap, both
        # halves keep whole words and fit the budget.
        self._no_dict()
        line = "สะพายขึ้นไหล่ปุ๊บ สะพายขึ้นไหล่ปุ๊บ"
        self.assertGreater(tls.width_px(line), budget_px())
        pieces, forced = tls.split_phrase_tracked(line, budget_px())
        self.assertFalse(forced)
        self.assertEqual(pieces, ["สะพายขึ้นไหล่ปุ๊บ", " สะพายขึ้นไหล่ปุ๊บ"])
        self.assertEqual("".join(pieces), line)
        for p in pieces:
            self.assertLessEqual(tls.width_px(p), budget_px())

    def test_spaceless_overflow_fails_loud_instead_of_guessing(self):
        self._no_dict()
        line = "เข้ากับเสื้อผ้าได้ทุกเซ็ตสะพายขึ้นไหล่ปุ๊บ" * 2  # no spaces
        self.assertGreater(tls.width_px(line), budget_px())
        with self.assertRaises(tls.NoSafeSplitError):
            tls.split_phrase_tracked(line, budget_px())

    def test_split_matched_reports_overflow_error_not_silent_sever(self):
        self._no_dict()
        line = "เข้ากับเสื้อผ้าได้ทุกเซ็ตสะพายขึ้นไหล่ปุ๊บ" * 2
        entry = {"line_index": 0, "script_line": line,
                 "start": 1.0, "end": 6.0,
                 "source_char_start": 0, "source_char_end": len(line)}
        with self.assertRaises(WidthOverflowError):
            split_matched([entry])


if __name__ == "__main__":
    unittest.main(verbosity=2)
