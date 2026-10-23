#!/usr/bin/env python3
"""v1.1 T5-fix tests: script_timing.trim_overlaps.

- Slight adjacent overlaps are trimmed (end only; text + onsets kept).
- Severe overlaps that cannot keep MIN_DURATION still raise (fail loud).
- Zero-duration input raises (fail loud is kept).
- Non-overlapping / contiguous cues pass through untouched.
- Input list is never mutated.
- clip01-shaped overlap (line 5 starts 11.04 < prev end 11.42) trims.

Run from the skill root:

    python3 tests/test_script_timing.py
    # or
    python3 -m unittest tests.test_script_timing -v

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from script_marks import assert_no_overlap  # noqa: E402 (strict QA still holds)
from script_timing import (  # noqa: E402
    MIN_CUE_DURATION,
    MIN_DURATION,
    OVERLAP_EPS,
    assert_no_speech_overrun,
    merge_short_cues,
    trim_overlaps,
)


def C(i: int, start: float, end: float, text: str = "ทดสอบ") -> dict:
    return {"line_index": i, "script_line": text, "text": text,
            "display_text": text, "start": start, "end": end}


class TrimOverlapsTest(unittest.TestCase):
    def test_trims_end_only(self):
        matched = [C(4, 9.0, 11.42), C(5, 11.04, 13.0)]
        out = trim_overlaps(matched)
        self.assertAlmostEqual(out[0]["end"], 11.04 - OVERLAP_EPS)
        self.assertEqual(out[1]["start"], 11.04)  # onset untouched
        self.assertEqual(out[0]["script_line"], "ทดสอบ")  # text untouched
        assert_no_overlap(out)

    def test_input_not_mutated(self):
        matched = [C(0, 0.0, 1.0), C(1, 0.5, 1.5)]
        trim_overlaps(matched)
        self.assertEqual(matched[0]["end"], 1.0)

    def test_no_overlap_untouched(self):
        matched = [C(0, 0.0, 1.0), C(1, 1.0, 2.0), C(2, 2.5, 3.5)]
        out = trim_overlaps(matched)
        self.assertEqual([(c["start"], c["end"]) for c in out],
                         [(0.0, 1.0), (1.0, 2.0), (2.5, 3.5)])

    def test_chain_of_overlaps(self):
        matched = [C(0, 0.0, 1.2), C(1, 1.0, 2.1), C(2, 2.0, 3.0)]
        out = trim_overlaps(matched)
        assert_no_overlap(out)
        self.assertAlmostEqual(out[0]["end"], 1.0 - OVERLAP_EPS)
        self.assertAlmostEqual(out[1]["end"], 2.0 - OVERLAP_EPS)

    def test_severe_overlap_raises(self):
        # Trim would leave < MIN_DURATION: fail loud, report names lines.
        matched = [C(0, 0.0, 1.0), C(1, 0.02, 1.5)]
        with self.assertRaises(ValueError) as ctx:
            trim_overlaps(matched)
        self.assertIn("overlap-trim refused", str(ctx.exception))

    def test_zero_duration_raises(self):
        matched = [C(0, 1.0, 1.0), C(1, 1.5, 2.0)]
        with self.assertRaises(ValueError):
            trim_overlaps(matched)

    def test_reports_every_bad_pair(self):
        matched = [C(0, 0.0, 0.05), C(1, 0.01, 0.06), C(2, 0.02, 1.0)]
        with self.assertRaises(ValueError) as ctx:
            trim_overlaps(matched)
        self.assertIn("2 unresolvable", str(ctx.exception))

    def test_empty_and_singleton(self):
        self.assertEqual(trim_overlaps([]), [])
        self.assertEqual(len(trim_overlaps([C(0, 0.0, 1.0)])), 1)

    def test_min_duration_floor(self):
        self.assertGreaterEqual(MIN_DURATION, 0.02)


class MergeShortCuesTest(unittest.TestCase):
    """2026-09-24 readability floor: no cue under MIN_CUE_DURATION (0.6s).

    Short cues merge forward (verbatim concat, span first.start→next.end);
    a short LAST cue folds backward; over-width merges are skipped and
    reported, never forced; input never mutated.
    """

    def test_cross_line_merge_refused_by_default(self):
        # Lip-locked 2026-09-25 (client-batch1 C2 post-mortem): a
        # cross-line merge shows the next sentence BEFORE it is spoken,
        # which reads as a wrong sentence. Default refuses; the short
        # cue is reported so the gate asks Lip for a short-flash
        # exception instead.
        matched = [C(0, 9.36, 9.74, "แต่ยัง"), C(1, 9.74, 10.60, "แฝงความเท่"),
                   C(2, 10.60, 11.50, "แมตช์ชุดไหน")]
        out, rep = merge_short_cues(matched)
        self.assertEqual([e["script_line"] for e in out],
                         ["แต่ยัง", "แฝงความเท่", "แมตช์ชุดไหน"])
        self.assertEqual(len(rep["skipped"]), 1)
        # input untouched
        self.assertEqual(matched[0]["script_line"], "แต่ยัง")

    def test_legacy_cross_line_opt_in(self):
        # Explicit opt-in keeps the old mechanics (never used in
        # production burns).
        matched = [C(0, 9.36, 9.74, "แต่ยัง"), C(1, 9.74, 10.60, "แฝงความเท่"),
                   C(2, 10.60, 11.50, "แมตช์ชุดไหน")]
        out, rep = merge_short_cues(matched, allow_cross_line=True)
        self.assertEqual([e["script_line"] for e in out],
                         ["แต่ยังแฝงความเท่", "แมตช์ชุดไหน"])
        self.assertAlmostEqual(out[0]["start"], 9.36)
        self.assertAlmostEqual(out[0]["end"], 10.60)
        self.assertEqual(rep["merged"], ["แต่ยังแฝงความเท่"])
        # input untouched
        self.assertEqual(matched[0]["script_line"], "แต่ยัง")

    def test_short_last_cue_folds_backward(self):
        matched = [C(0, 0.0, 1.0, "ทรงสวย"),
                   C(1, 1.0, 1.3, "หยิบของ")]
        out, rep = merge_short_cues(matched, allow_cross_line=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["script_line"], "ทรงสวยหยิบของ")
        self.assertAlmostEqual(out[0]["end"], 1.3)
        self.assertEqual(rep["merged"], ["ทรงสวยหยิบของ"])

    def test_short_last_cue_refused_by_default(self):
        matched = [C(0, 0.0, 1.0, "ทรงสวย"),
                   C(1, 1.0, 1.3, "หยิบของ")]
        out, rep = merge_short_cues(matched)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(rep["skipped"]), 1)

    def test_backward_merge_refused_over_syllable_cap(self):
        # "ดีไซน์เข้ากับสรีระ"+"หยิบของ" = 8 syllables > 6: reported,
        # never force-merged (Codex review 2026-09-24)
        matched = [C(0, 0.0, 1.0, "ดีไซน์เข้ากับสรีระ"),
                   C(1, 1.0, 1.3, "หยิบของ")]
        out, rep = merge_short_cues(matched)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(rep["skipped"]), 1)

    def test_ok_cues_untouched(self):
        matched = [C(0, 0.0, 0.66, "แล้วจุของ"),
                   C(1, 0.66, 1.30, "ได้เยอะมาก")]
        out, rep = merge_short_cues(matched)
        self.assertEqual(len(out), 2)
        self.assertEqual(rep, {"merged": [], "skipped": []})

    def test_over_width_merge_skipped(self):
        long_tail = "ก" * 60
        matched = [C(0, 0.0, 0.3, "หยิบของ"), C(1, 0.3, 1.1, long_tail)]
        out, rep = merge_short_cues(matched, max_width_px=926.84)
        self.assertEqual(len(out), 2)  # nothing forced
        self.assertEqual(len(rep["skipped"]), 1)

    def test_floor_value(self):
        self.assertAlmostEqual(MIN_CUE_DURATION, 0.6)


class TestSpeechOverrun(unittest.TestCase):
    """assert_no_speech_overrun (Lip-locked 2026-09-25): repair holds
    may only consume silence, never neighbouring speech."""

    def test_forward_hold_into_speech_refused(self):
        # C2 'สะพายปุ๊บ' shape: line 2 bound ends 6.94, held to 7.18,
        # next line's speech already started 6.80 per segments.
        matched = [C(2, 6.28, 7.18, "สะพายปุ๊บ"),
                   C(3, 7.26, 8.26, "ลุควันนั้นก็ดูเต็ม")]
        segs = [{"start": 6.30, "end": 6.80, "text": "สะพายปุ๊บ"},
                {"start": 6.80, "end": 9.20, "text": "ลุควันนั้นก็ดูเต็ม"}]
        bounds = {2: (6.74, 6.94), 3: (7.26, 8.26)}
        with self.assertRaises(ValueError) as ctx:
            assert_no_speech_overrun(matched, segs, bounds)
        self.assertIn("line 2", str(ctx.exception))

    def test_hold_inside_silence_passes(self):
        matched = [C(7, 13.62, 14.60, "ใส่กับขาสั้น"),
                   C(8, 16.24, 16.74, "สะพายแล้วกระจาย")]
        segs = [{"start": 13.70, "end": 15.40, "text": "ใส่กับขาสั้น"},
                {"start": 16.24, "end": 17.04, "text": "สะพายแล้วกระจาย"}]
        bounds = {7: (13.62, 14.60), 8: (16.24, 16.74)}
        assert_no_speech_overrun(matched, segs, bounds)  # no raise

    def test_backward_hold_into_speech_refused(self):
        matched = [C(3, 13.32, 14.10, "แถมดูสมส่วน"),
                   C(4, 14.18, 14.86, "ช่องเก็บ")]
        segs = [{"start": 11.10, "end": 14.40, "text": "สะพายแล้วรู้สึกไม่"},
                {"start": 14.40, "end": 15.99, "text": "ช่องเก็บของตรงกลาง"}]
        bounds = {3: (13.32, 14.10), 4: (14.40, 14.86)}
        with self.assertRaises(ValueError) as ctx:
            assert_no_speech_overrun(matched, segs, bounds)
        self.assertIn("line 4", str(ctx.exception))

    def test_internal_rebalance_passes(self):
        # Rebalances inside a line's own bounds are always honest.
        matched = [C(8, 20.72, 21.25, "ใบนี้"),
                   C(8, 21.25, 21.98, "คือทางออกเลยนะ")]
        segs = [{"start": 20.70, "end": 22.10, "text": "ใบนี้คือทางออกเลยนะ"}]
        bounds = {8: (20.72, 21.98)}
        assert_no_speech_overrun(matched, segs, bounds)  # no raise

    def test_cues_without_line_index_skipped(self):
        matched = [{"start": 0.0, "end": 99.0, "text": "ลอย"}]
        assert_no_speech_overrun(matched, [], None)  # no raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
