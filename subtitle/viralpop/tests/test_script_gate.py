#!/usr/bin/env python3
"""Ticket 03 tests: pre-burn mismatch gate.

- Non-empty mismatches -> gate blocks, human-readable report of ALL
  mismatches, NO ass file written, NO burn.
- Empty mismatches -> gate passes, ASS build proceeds.

Run from the skill root:

    python3 tests/test_script_gate.py
    # or
    python3 -m unittest tests.test_script_gate -v

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from script_align import align_script_to_timeline  # noqa: E402
from script_ass import build_ass  # noqa: E402
from script_gate import (  # noqa: E402
    MismatchGateError,
    assert_gate,
    format_mismatch_report,
    gate_before_burn,
)


def W(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


class TestGateBlocks(unittest.TestCase):
    def test_nonempty_mismatches_refuse(self):
        words = [W("สวัสดี", 0.0, 0.5), W("ครับ", 0.5, 0.8)]
        alignment = align_script_to_timeline(
            ["สวัสดีครับ", "ราคาเก้าพันบาท"], words)
        result = gate_before_burn(alignment)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatch_count"], 1)
        self.assertTrue(result["report"])

    def test_report_covers_all_mismatches(self):
        words = [W("สวัสดี", 0.0, 0.5)]
        script = ["สวัสดีครับ", "ราคาเก้าพันบาท", "โทนสีดำคลาสสิก"]
        alignment = align_script_to_timeline(script, words)
        self.assertGreaterEqual(len(alignment["mismatches"]), 2)
        report = format_mismatch_report(alignment)
        for mm in alignment["mismatches"]:
            self.assertIn(mm["script_line"], report)
            self.assertIn(mm["reason"], report)
            if mm["heard_snippet"]:
                self.assertIn(mm["heard_snippet"], report)

    def test_report_has_time_window_and_heard(self):
        words = [W("สวัสดี", 0.0, 0.5), W("ครับ", 0.5, 0.8)]
        alignment = align_script_to_timeline(
            ["สวัสดีครับ", "ราคาเก้าพันบาท"], words)
        report = format_mismatch_report(alignment)
        mm = alignment["mismatches"][0]
        self.assertIn("ราคาเก้าพันบาท", report)
        self.assertIn(mm["heard_snippet"], report)
        self.assertRegex(report, r"\d+\.\d{2}s|unknown time")

    def test_assert_gate_raises_with_report(self):
        words = [W("สวัสดี", 0.0, 0.5)]
        alignment = align_script_to_timeline(["ราคาเก้าพันบาท"], words)
        with self.assertRaises(MismatchGateError) as ctx:
            assert_gate(alignment)
        self.assertIn("ราคาเก้าพันบาท", str(ctx.exception))


class TestGateAndAssFiles(unittest.TestCase):
    def test_blocked_writes_no_ass_file(self):
        import tempfile

        words = [W("สวัสดี", 0.0, 0.5), W("ครับ", 0.5, 0.8)]
        alignment = align_script_to_timeline(
            ["สวัสดีครับ", "ราคาเก้าพันบาท"], words)
        self.assertFalse(gate_before_burn(alignment)["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub.ass"
            with self.assertRaises(MismatchGateError):
                build_ass(alignment, target)
            self.assertFalse(target.exists())

    def test_clear_gate_allows_ass_file(self):
        import tempfile

        words = [W("สวยแพงมาก", 0.0, 0.6), W("ทรงสวย", 0.7, 1.4)]
        alignment = align_script_to_timeline(["สวยแพงมาก", "ทรงสวย"], words)
        result = gate_before_burn(alignment)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mismatch_count"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub.ass"
            ass_text = build_ass(alignment, target)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), ass_text)
            self.assertIn("สวยแพงมาก", ass_text)

    def test_malformed_alignment_refuses(self):
        result = gate_before_burn({"matched": []})
        self.assertFalse(result["ok"])
        result = gate_before_burn("not-a-dict")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
