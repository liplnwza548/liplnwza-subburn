#!/usr/bin/env python3
"""Ticket 02 tests: Viral Pop ASS from aligned script lines.

Forbidden outcomes under test (primary case):
- Dialogue text != script line (e.g. "สวยแพง" / "มากทรงสวย" instead of
  "สวยแพงมาก" / "ทรงสวย")
- mid-word split of Thai ("คลาสสิก" -> "คลาสิ" / "ก", orphan "ก" cue)
- loanword/ASR rewrite altering script text ("อย่าบในภาพ" leaking in)

Run from the skill root:

    python3 tests/test_script_ass.py
    # or
    python3 -m unittest tests.test_script_ass -v

Stdlib unittest only.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from script_align import align_script_to_timeline  # noqa: E402
from script_ass import FONT_NAME, STYLE_NAME, build_ass  # noqa: E402
from script_gate import MismatchGateError  # noqa: E402


def W(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


def dialogue_payloads(ass_text: str) -> list[str]:
    """Raw Text field (after the 9th comma) of every Dialogue line."""
    out = []
    for line in ass_text.splitlines():
        if line.startswith("Dialogue:"):
            out.append(line.split(",", 9)[9])
    return out


def dialogue_texts(ass_text: str) -> list[str]:
    """On-screen text per Dialogue (override tags stripped)."""
    return [re.sub(r"\{[^}]*\}", "", p) for p in dialogue_payloads(ass_text)]


PRIMARY_WORDS = [
    W("สวยแพง", 0.0, 0.5),
    W("มาก", 0.5, 0.8),
    W("ทรงสวย", 0.9, 1.6),
]


class TestDialogueEqualsScript(unittest.TestCase):
    def test_each_dialogue_text_equals_script_line(self):
        script = ["สวยแพงมาก", "ทรงสวย"]
        alignment = align_script_to_timeline(script, PRIMARY_WORDS)
        ass_text = build_ass(alignment)
        self.assertEqual(dialogue_texts(ass_text), script)

    def test_one_dialogue_per_script_line_no_repack(self):
        script = ["สวยแพงมาก", "ทรงสวย"]
        alignment = align_script_to_timeline(script, PRIMARY_WORDS)
        ass_text = build_ass(alignment)
        self.assertEqual(len(dialogue_payloads(ass_text)), len(script))

    def test_forbidden_cross_line_pack_absent(self):
        script = ["สวยแพงมาก", "ทรงสวย"]
        alignment = align_script_to_timeline(script, PRIMARY_WORDS)
        ass_text = build_ass(alignment)
        texts = dialogue_texts(ass_text)
        self.assertNotIn("สวยแพง", texts)
        self.assertNotIn("มากทรงสวย", texts)
        self.assertNotIn("มาก", texts)

    def test_no_wrap_or_split_markers(self):
        script = ["สวยแพงมาก", "ทรงสวย"]
        alignment = align_script_to_timeline(script, PRIMARY_WORDS)
        ass_text = build_ass(alignment)
        self.assertNotIn(r"\N", ass_text)
        self.assertIn("WrapStyle: 2", ass_text)


class TestNoMidWordSplit(unittest.TestCase):
    def test_classic_never_splits_to_orphan(self):
        script = ["โทนสีดำคลาสสิก"]
        words = [W("โทนสีดำคลาสสิก", 2.0, 3.2)]
        alignment = align_script_to_timeline(script, words)
        ass_text = build_ass(alignment)
        texts = dialogue_texts(ass_text)
        self.assertEqual(texts, script)
        self.assertNotIn("ก", texts)
        self.assertNotIn("คลาสิ", texts)
        for payload in dialogue_payloads(ass_text):
            visible = re.sub(r"\{[^}]*\}", "", payload)
            self.assertGreater(len(visible), 1)

    def test_long_line_stays_single_cue(self):
        script = ["โทนสีดำคลาสสิก", "อย่างในภาพ"]
        words = [W("โทนสีดำคลาสสิก", 0.0, 0.9), W("อย่าบในภาพ", 1.0, 1.8)]
        alignment = align_script_to_timeline(script, words)
        ass_text = build_ass(alignment)
        self.assertEqual(len(dialogue_payloads(ass_text)), 2)


class TestNoRewrite(unittest.TestCase):
    def test_asr_misspelling_never_reaches_ass(self):
        script = ["โทนสีดำคลาสสิก", "อย่างในภาพ"]
        words = [W("โทนสีดำคลาสสิก", 0.0, 0.9), W("อย่าบในภาพ", 1.0, 1.8)]
        alignment = align_script_to_timeline(script, words)
        ass_text = build_ass(alignment)
        self.assertIn("อย่างในภาพ", ass_text)
        self.assertNotIn("อย่าบ", ass_text)

    def test_module_applies_no_rewrite_helpers(self):
        # v1.1 T1+T2: width MEASUREMENT/split helpers are allowed (they only
        # measure pixels / choose cut points — never rewrite wording).
        # v1.1 T3+T4: script_keywords (weaves {\\c} runs around script
        # substrings) and script_marks (thin wrapper over the proven
        # thai_mark_fix Layer1 helpers) are allowed — neither rewrites the
        # base on-screen wording. Direct STT-text/loanword rewrites stay
        # forbidden.
        src = (Path(__file__).resolve().parents[1] / "render" / "script_ass.py").read_text(
            encoding="utf-8"
        )
        imports = [l for l in src.splitlines()
                   if l.strip().startswith(("import ", "from "))]
        for forbidden in ("loanword", "thai_mark_fix",
                          "build_v4", "build_v5", "build_clip"):
            for line in imports:
                self.assertNotIn(forbidden, line)


class TestStyleLock(unittest.TestCase):
    def test_viral_pop_and_prompt_bold(self):
        script = ["สวยแพงมาก"]
        alignment = align_script_to_timeline(script, [W("สวยแพงมาก", 0.0, 0.6)])
        ass_text = build_ass(alignment)
        self.assertIn(f"Style: {STYLE_NAME},{FONT_NAME},", ass_text)
        self.assertIn("Fontname", ass_text)
        self.assertIn("Prompt Bold", ass_text)
        style_lines = [l for l in ass_text.splitlines() if l.startswith("Style:")]
        self.assertEqual(len(style_lines), 1)
        for payload_line in dialogue_payloads(ass_text):
            _ = payload_line  # payload shape asserted via dialogue tests
        for line in ass_text.splitlines():
            if line.startswith("Dialogue:"):
                self.assertIn(f",{STYLE_NAME},", line)

    def test_portrait_playres(self):
        script = ["สวยแพงมาก"]
        alignment = align_script_to_timeline(script, [W("สวยแพงมาก", 0.0, 0.6)])
        ass_text = build_ass(alignment)
        self.assertIn("PlayResX: 1080", ass_text)
        self.assertIn("PlayResY: 1920", ass_text)


class TestRefusals(unittest.TestCase):
    def test_mismatched_alignment_refuses_without_file(self):
        import tempfile

        words = [W("สวัสดี", 0.0, 0.5), W("ครับ", 0.5, 0.8)]
        alignment = align_script_to_timeline(
            ["สวัสดีครับ", "ราคาเก้าพันบาท"], words)
        self.assertTrue(alignment["mismatches"])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub.ass"
            with self.assertRaises(MismatchGateError):
                build_ass(alignment, target)
            self.assertFalse(target.exists())

    def test_empty_matched_refuses(self):
        with self.assertRaises(ValueError):
            build_ass([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
