#!/usr/bin/env python3
"""Codex review 2026-09-24 item 5: tests that catch the REAL C6 problems.

Covers the new decision path end to end (not just helpers in
isolation): script-lock + diff + span coverage, syllable-proportional
slots, the cue planner (feasible + impossible slots), final validation
(exceptions, trim-warn, rate, centisecond, negation-across-cut), and
production-called merge guards. venv python (pythainlp) required —
the stdlib fallback severs words and miscounts syllables.

Run from the skill root:

    /home/box/subtitle-work/.venv/bin/python3 tests/test_cue_decisions.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from script_cue_split import plan_cues, plan_sentence  # noqa: E402
from script_lock import (  # noqa: E402
    CoverageError,
    diff_report,
    lock_script,
    verify_coverage,
)
from script_marks import validate_final_cues  # noqa: E402
from script_slot import split_slot_by_syllables  # noqa: E402
from script_timing import merge_short_cues  # noqa: E402
from thai_line_split import count_syllables  # noqa: E402

SCRIPT = Path("/home/box/subtitle-work/clips/batch6/script/c6.approved.txt")


def C(i, s, e, text, **kw):
    d = {"line_index": i, "script_line": text, "text": text,
         "display_text": text, "start": s, "end": e}
    d.update(kw)
    return d


class TestScriptLock(unittest.TestCase):
    def test_lock_reads_lip_script_with_hash(self):
        lock = lock_script(SCRIPT)
        self.assertEqual(len(lock["lines"]), 17)
        self.assertEqual(len(lock["sha256"]), 64)
        self.assertIn("ใครแบกกระเป๋าแล้วปวดไหล่", lock["lines"])
        self.assertIn("ก็เข้ากั๊นเข้ากัน", lock["lines"])

    def test_diff_finds_missing_sentence(self):
        lock = lock_script(SCRIPT)
        rep = diff_report(lock["lines"], "ไปตำกันได้เลย น้ำหนักเบา")
        kinds = [r["type"] for r in rep]
        self.assertIn("script_not_found_in_stt", kinds)
        # the missing one names the exact script line (no guessing)
        miss = [r for r in rep if r["type"] == "script_not_found_in_stt"]
        self.assertTrue(all(m["script"] in lock["lines"] for m in miss))

    def test_diff_clean_on_full_stt(self):
        lock = lock_script(SCRIPT)
        rep = diff_report(lock["lines"], lock["text"])
        self.assertEqual(
            [r for r in rep if r["type"] == "script_not_found_in_stt"], [])

    def test_coverage_passes_on_exact_spans(self):
        lock = lock_script(SCRIPT)
        cues = []
        pos = 0
        for i, line in enumerate(lock["lines"]):
            cues.append(C(i, float(i), float(i + 1), line,
                          source_char_start=pos,
                          source_char_end=pos + len(line)))
            pos += len(line)
        self.assertEqual(
            verify_coverage(lock["text"], cues)["chars"], len(lock["text"]))

    def test_coverage_refuses_dropped_word(self):
        lock = lock_script(SCRIPT)
        cues = []
        pos = 0
        for i, line in enumerate(lock["lines"]):
            text = line.replace("โอบ", "") if "โอบ" in line else line
            cues.append(C(i, float(i), float(i + 1), text,
                          source_char_start=pos,
                          source_char_end=pos + len(text)))
            pos += len(text)
        with self.assertRaises(CoverageError):
            verify_coverage(lock["text"], cues)

    def test_coverage_refuses_missing_span(self):
        lock = lock_script(SCRIPT)
        cues = [C(0, 0.0, 1.0, lock["lines"][0])]
        with self.assertRaises(CoverageError):
            verify_coverage(lock["text"], cues)


class TestSlot(unittest.TestCase):
    def test_c6_design_split_is_syllable_proportional(self):
        # 9 syllables over 1.56s: 5+4 -> 0.87/0.69 (NOT half-time 0.78)
        spans = split_slot_by_syllables(
            ["ดีไซน์โอบเข้ากับ", "สรีระได้ดี"], 3.64, 5.20)
        (s1, e1), (s2, e2) = spans
        self.assertAlmostEqual(s1, 3.64)
        self.assertAlmostEqual(e2, 5.20)
        self.assertAlmostEqual(e1, 3.64 + 1.56 * 5 / 9, places=2)
        self.assertGreater(e1 - s1, 0.6)
        self.assertGreater(e2 - s2, 0.6)

    def test_invalid_slot_refuses(self):
        with self.assertRaises(ValueError):
            split_slot_by_syllables(["ก"], 5.0, 5.0)
        with self.assertRaises(ValueError):
            split_slot_by_syllables([], 0.0, 1.0)


class TestPlanner(unittest.TestCase):
    def test_design_sentence_plans_two_feasible_cues(self):
        line = "ดีไซน์โอบเข้ากับสรีระได้ดี"
        cues, problems = plan_sentence(
            line, 3.64, 5.20, 3, 0, len(line))
        self.assertEqual(problems, [])
        self.assertEqual([c["script_line"] for c in cues],
                         ["ดีไซน์โอบเข้ากับ", "สรีระได้ดี"])
        for c in cues:
            self.assertGreaterEqual(c["end"] - c["start"], 0.6)
            self.assertLessEqual(count_syllables(c["script_line"]), 6)
            self.assertTrue(c["is_estimated"])
        self.assertEqual("".join(c["script_line"] for c in cues), line)

    def test_impossible_slot_returns_verdict_not_cues(self):
        # 7 syllables that cannot fit width AND time: verdict, no force
        cues, problems = plan_sentence(
            "ใครแบกกระเป๋าแล้วปวดไหล่", 5.95, 6.30, 4, 0, 23)
        self.assertTrue(problems)
        self.assertTrue(all(p["type"] == "unresolvable" for p in problems))

    def test_plan_cues_chains_char_spans(self):
        lock = lock_script(SCRIPT)
        sents = []
        pos = 0
        t = 0.0
        for i, line in enumerate(lock["lines"][:3]):
            dur = max(1.2, count_syllables(line) * 0.35)
            sents.append({"line": line, "start": t, "end": t + dur,
                          "line_index": i,
                          "char_a": pos, "char_b": pos + len(line)})
            pos += len(line)
            t += dur + 0.1
        cues, problems = plan_cues(sents)
        self.assertEqual(problems, [])
        verify_coverage("".join(lock["lines"][:3]), cues)


class TestValidation(unittest.TestCase):
    def test_exception_lets_short_cue_burn(self):
        cues = [C(0, 16.92, 17.48, "หยิบของ", exception="c6-yib-056")]
        out = validate_final_cues(cues, exceptions=["c6-yib-056"])
        self.assertEqual(out["cues"], 1)

    def test_short_cue_without_exception_refuses(self):
        with self.assertRaises(ValueError) as ctx:
            validate_final_cues([C(0, 16.92, 17.48, "หยิบของ")])
        self.assertIn("no Lip exception", str(ctx.exception))

    def test_trim_shortened_warns_not_refuses(self):
        cues = [C(0, 0.0, 0.49, "สวยแพงมาก", trimmed_end=True)]
        out = validate_final_cues(cues)
        self.assertEqual(len(out["warnings"]), 1)

    def test_unreadable_rate_refuses(self):
        cues = [C(0, 0.0, 0.5, "ดีไซน์โอบเข้ากับสรีระได้ดี")]
        with self.assertRaises(ValueError) as ctx:
            validate_final_cues(cues)
        self.assertIn("syl/s", str(ctx.exception))

    def test_centisecond_zero_length_refuses(self):
        with self.assertRaises(ValueError):
            validate_final_cues([C(0, 1.001, 1.004, "นะ")])

    def test_negation_stranded_across_cut_refuses(self):
        cues = [C(0, 0.0, 0.7, "ไม่"), C(1, 0.7, 1.4, "ปวดไหล่")]
        with self.assertRaises(ValueError) as ctx:
            validate_final_cues(cues, keywords=["ปวดไหล่"])
        self.assertIn("negation", str(ctx.exception))

    def test_negation_whole_in_one_cue_passes(self):
        cues = [C(0, 0.0, 0.8, "ไม่ปวดไหล่")]
        out = validate_final_cues(cues, keywords=["ปวดไหล่"])
        self.assertEqual(out["cues"], 1)


class TestMergeProductionGuards(unittest.TestCase):
    def test_cross_line_merge_refused_when_disallowed(self):
        m = [C(0, 11.50, 12.02, "ก็เข้ากัน"),
             C(1, 12.02, 12.60, "เข้ากัน")]
        out, rep = merge_short_cues(m, allow_cross_line=False)
        self.assertEqual(len(out), 2)
        self.assertTrue(rep["skipped"])

    def test_over_syllable_merge_refused(self):
        # guard order is width-then-syllables: force the syllable guard
        # with a fitting width but max_syl=4 (merged 'แต่ยังแฝงความเท่'
        # is 5 syllables, 806px — width passes, syllables refuse)
        m = [C(0, 9.36, 9.74, "แต่ยัง"),
             C(0, 9.74, 10.60, "แฝงความเท่")]
        out, rep = merge_short_cues(m, max_syl=4)
        self.assertEqual(len(out), 2)
        self.assertIn("syllable", rep["skipped"][0][1])

    def test_merged_cue_keeps_char_span(self):
        m = [C(0, 9.36, 9.74, "แต่ยัง",
                 source_char_start=0, source_char_end=5),
             C(0, 9.74, 10.60, "แฝงความเท่",
                 source_char_start=5, source_char_end=15)]
        out, rep = merge_short_cues(m)
        self.assertEqual(rep["merged"], ["แต่ยังแฝงความเท่"])
        self.assertEqual(
            (out[0]["source_char_start"], out[0]["source_char_end"]),
            (0, 15))


if __name__ == "__main__":
    unittest.main(verbosity=2)
