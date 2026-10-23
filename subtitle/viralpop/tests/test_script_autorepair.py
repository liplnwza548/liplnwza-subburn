#!/usr/bin/env python3
"""Ticket auto-repair: hand-table shapes from batch1/batch2, applied by machine.

Cases mirror the real batch2 repair tables (times verbatim):
  C3[18] hold-back into pause · C3[28] outro hold · C2[1]/[6] rebalance ·
  C4[19] micro-hold · C4[29] outro hold · C4[5] cross-line leftover.
Plus: silence proof via word timeline, monotonicity, text untouched.

Run from the skill root:
    python3 -m pytest tests/test_script_autorepair.py -q
Stdlib unittest only. No network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from script_autorepair import auto_repair  # noqa: E402


def cue(line, text, start, end):
    return {"line_index": line, "script_line": text, "display_text": text,
            "start": start, "end": end}


def durs(matched):
    return [round(float(c["end"]) - float(c["start"]), 2) for c in matched]


class TestBatch2Shapes(unittest.TestCase):
    def test_c3_hold_back_into_pause(self):
        matched = [cue(2, "การแต่งตัว", 15.00, 15.70),
                   cue(3, "ช่องด้าน", 15.78, 16.27),
                   cue(3, "ภายในกว้าง ใส่", 16.28, 17.30)]
        _, report = auto_repair(matched)
        self.assertEqual(len(report["leftover"]), 0)
        self.assertTrue(any("hold-back" in r for r in report["repaired"]))
        self.assertTrue(all(d >= 0.5 - 1e-6 for d in durs(matched)))
        self.assertEqual(matched[1]["display_text"], "ช่องด้าน")  # untouched

    def test_c3_outro_hold(self):
        matched = [cue(4, "ที่ตะกร้าด้านล่าง", 22.98, 23.82),
                   cue(4, "ได้เลย", 23.82, 24.20)]
        _, report = auto_repair(matched)
        self.assertEqual(len(report["leftover"]), 0)
        self.assertTrue(any("hold-forward" in r for r in report["repaired"]))
        self.assertGreaterEqual(matched[1]["end"] - matched[1]["start"],
                                0.5 - 1e-6)
        self.assertLessEqual(matched[1]["end"], 24.20 + 1.2)  # capped

    def test_c2_rebalance_forward(self):
        matched = [cue(0, "สะพายไปทำงาน", 0.66, 1.51),
                   cue(0, " หรือว่า", 1.51, 1.91),
                   cue(0, "จะไปแฮงเอาต์", 1.91, 2.67)]
        _, report = auto_repair(matched)
        self.assertEqual(len(report["leftover"]), 0)
        self.assertTrue(any("rebalance-forward" in r
                            for r in report["repaired"]))
        self.assertAlmostEqual(matched[1]["end"], matched[2]["start"])
        self.assertTrue(all(d >= 0.5 - 1e-6 for d in durs(matched)))

    def test_c4_micro_hold_forward(self):
        matched = [cue(2, "กระเป๋าแบรนด์", 15.02, 15.72),
                   cue(2, "แนบผิว", 15.72, 16.20),
                   cue(3, "เดินช้อปปิ้งซื้อ", 16.22, 17.20)]
        _, report = auto_repair(matched)
        self.assertEqual(len(report["leftover"]), 0)
        self.assertTrue(all(d >= 0.5 - 1e-6 for d in durs(matched)))

    def test_c4_cross_line_leftover(self):
        # [5] L0 0.11s: no gaps, neighbors different lines for the needed
        # move -> must stay manual (true-boundary call), reported loudly.
        matched = [cue(0, "ที่ตะกร้าด้านล่าง", 3.67, 4.44),
                   cue(0, "ได้เลย", 4.44, 4.55),
                   cue(1, "กระเป๋าใบนี้ช่วย", 4.56, 5.46)]
        _, report = auto_repair(matched)
        self.assertEqual(len(report["repaired"]), 0)
        self.assertEqual(len(report["leftover"]), 1)
        self.assertIn("ได้เลย", report["leftover"][0])

    def test_silence_proof_redirects_to_rebalance(self):
        # Gap 15.70-15.78 is NOT silence (word proves it) -> hold-back
        # refused; no forward gap either -> same-line rebalance applies.
        words = [{"word": "x", "start": 15.71, "end": 15.77}]
        matched = [cue(2, "การแต่งตัว", 15.00, 15.70),
                   cue(3, "ช่องด้าน", 15.78, 16.27),
                   cue(3, "ภายในกว้าง ใส่", 16.27, 17.30)]
        _, report = auto_repair(matched, words=words)
        self.assertEqual(len(report["leftover"]), 0)
        self.assertTrue(any("rebalance-forward" in r
                            for r in report["repaired"]))
        self.assertTrue(all(d >= 0.5 - 1e-6 for d in durs(matched)))

    def test_monotonic_and_text_preserved(self):
        matched = [cue(0, "สะพายไปทำงาน", 0.66, 1.51),
                   cue(0, " หรือว่า", 1.51, 1.91),
                   cue(0, "จะไปแฮงเอาต์", 1.91, 2.67),
                   cue(0, " ก็เข้ากับทุก", 2.67, 3.30)]
        before = [c["display_text"] for c in matched]
        auto_repair(matched)
        self.assertEqual([c["display_text"] for c in matched], before)
        prev = -1.0
        for c in matched:
            self.assertLess(float(c["start"]), float(c["end"]))
            self.assertGreaterEqual(round(float(c["start"]), 2),
                                    round(prev, 2))
            prev = float(c["end"])


if __name__ == "__main__":
    unittest.main()
