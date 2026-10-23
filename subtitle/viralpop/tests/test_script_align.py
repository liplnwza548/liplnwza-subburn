#!/usr/bin/env python3
"""Ticket 01 tests: script lines align to STT word timeline (timing only).

Run from the skill root:

    python3 tests/test_script_align.py
    # or
    python3 -m unittest tests.test_script_align -v

Stdlib unittest only (no pytest in this skill).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from script_align import align_script_to_timeline  # noqa: E402


def W(word: str, start: float, end: float) -> dict:
    """One Groq verbose_json style word entry."""
    return {"word": word, "start": start, "end": end}


class TestPrimaryCase(unittest.TestCase):
    def test_mak_stays_on_first_line_text(self):
        # STT heard the audio; script defines the on-screen wording.
        # Regression: "มาก" must not leak into the next cue's TEXT.
        words = [
            W("สวยแพง", 0.0, 0.5),
            W("มาก", 0.5, 0.8),
            W("ทรงสวย", 0.9, 1.4),
        ]
        res = align_script_to_timeline(["สวยแพงมาก", "ทรงสวย"], words)
        self.assertEqual(res["mismatches"], [])
        self.assertEqual(len(res["matched"]), 2)
        first, second = res["matched"]
        # Cue text is the script line verbatim, modifier attached.
        self.assertEqual(first["script_line"], "สวยแพงมาก")
        self.assertEqual(first["text"], "สวยแพงมาก")
        self.assertEqual(first["display_text"], "สวยแพงมาก")
        self.assertEqual(second["text"], "ทรงสวย")
        self.assertEqual(second["display_text"], "ทรงสวย")
        # Timing comes from STT only.
        self.assertAlmostEqual(first["start"], 0.0)
        self.assertAlmostEqual(first["end"], 0.8)
        self.assertAlmostEqual(second["start"], 0.9)
        self.assertAlmostEqual(second["end"], 1.4)
        # Monotonic: second cue starts at/after first cue ends.
        self.assertGreaterEqual(second["start"], first["end"])

    def test_primary_case_alt_word_split(self):
        # Same audio, different Whisper word chopping: text still exact.
        words = [
            W("สวย", 0.0, 0.3),
            W("แพงมาก", 0.3, 0.8),
            W("ทรง", 0.9, 1.1),
            W("สวย", 1.1, 1.4),
        ]
        res = align_script_to_timeline(["สวยแพงมาก", "ทรงสวย"], words)
        self.assertEqual(res["mismatches"], [])
        texts = [m["text"] for m in res["matched"]]
        self.assertEqual(texts, ["สวยแพงมาก", "ทรงสวย"])

    def test_asr_misspelling_never_becomes_text(self):
        # Spec row: ASR hears "อย่าบในภาพ", script says "อย่างในภาพ".
        # Alignment may fuzzy-match for TIMING; on-screen text stays script.
        words = [
            W("โทนสีดำคลาสสิก", 0.0, 0.9),
            W("อย่าบในภาพ", 1.0, 1.8),
        ]
        script = ["โทนสีดำคลาสสิก", "อย่างในภาพ"]
        res = align_script_to_timeline(script, words)
        self.assertEqual(res["mismatches"], [])
        for m, line in zip(res["matched"], script):
            self.assertEqual(m["text"], line)
            self.assertEqual(m["display_text"], line)
            self.assertNotIn("อย่าบ", m["text"])
        self.assertEqual(res["matched"][1]["text"], "อย่างในภาพ")

    def test_long_word_not_split(self):
        # "คลาสสิก" must align as one cue span, never orphan "ก".
        words = [W("โทนสีดำคลาสสิก", 2.0, 3.2)]
        res = align_script_to_timeline(["โทนสีดำคลาสสิก"], words)
        self.assertEqual(res["mismatches"], [])
        self.assertEqual(len(res["matched"]), 1)
        m = res["matched"][0]
        self.assertEqual(m["text"], "โทนสีดำคลาสสิก")
        self.assertLess(m["start"], m["end"])


class TestMismatches(unittest.TestCase):
    def test_unmatched_line_reported_no_invention(self):
        words = [W("สวัสดี", 0.0, 0.5), W("ครับ", 0.5, 0.8)]
        res = align_script_to_timeline(
            ["สวัสดีครับ", "ราคาเก้าพันบาท"], words)
        self.assertEqual(len(res["matched"]), 1)
        self.assertEqual(res["matched"][0]["text"], "สวัสดีครับ")
        self.assertEqual(len(res["mismatches"]), 1)
        mm = res["mismatches"][0]
        self.assertEqual(mm["line_index"], 1)
        self.assertEqual(mm["script_line"], "ราคาเก้าพันบาท")
        self.assertIn("reason", mm)
        self.assertIsNotNone(mm["time_hint"])
        self.assertTrue(mm["heard_snippet"])

    def test_empty_timeline_all_mismatch(self):
        res = align_script_to_timeline(["สวยแพงมาก"], [])
        self.assertEqual(res["matched"], [])
        self.assertEqual(len(res["mismatches"]), 1)
        self.assertEqual(
            res["mismatches"][0]["reason"], "empty_word_timeline")

    def test_accepts_full_verbose_json_dict(self):
        payload = {"words": [W("สวยแพงมาก", 0.0, 0.6)],
                   "segments": [{"text": "สวยแพงมาก"}]}
        res = align_script_to_timeline(["สวยแพงมาก"], payload)
        self.assertEqual(res["mismatches"], [])
        self.assertEqual(res["matched"][0]["text"], "สวยแพงมาก")


class TestRepeatLatchWarning(unittest.TestCase):
    """Line 0 latching onto a repeated later span must warn (C4 2026-09-29:
    opening line matched at 20.4s of a 25s timeline, starving lines 1-4)."""

    def test_late_line_zero_warns(self):
        words = [W("zzzqqq", 0.0, 2.0),
                 W("ยกให้เป็นกระเป๋า", 8.0, 9.0),
                 W("ยืนพื้นประจำตู้", 9.0, 10.0)]
        res = align_script_to_timeline(["ยกให้เป็นกระเป๋ายืนพื้นประจำตู้"],
                                       words)
        self.assertEqual(res["mismatches"], [])
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("line 0", res["warnings"][0])

    def test_normal_order_no_warning(self):
        words = [W("ยกให้เป็นกระเป๋า", 0.0, 1.0),
                 W("ยืนพื้นประจำตู้", 1.0, 2.0)]
        res = align_script_to_timeline(["ยกให้เป็นกระเป๋ายืนพื้นประจำตู้"],
                                       words)
        self.assertEqual(res["mismatches"], [])
        self.assertEqual(res.get("warnings", []), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
