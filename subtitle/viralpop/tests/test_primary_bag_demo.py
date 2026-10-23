#!/usr/bin/env python3
"""Ticket 06: primary_bag_demo regression pack (SPEC acceptance table).

Synthetic timeline fixtures (no real bag clip needed on this machine).
Each test FAILS if a primary-case error returns:

1. ``มาก`` attachment — script ["สวยแพงมาก", "ทรงสวย"] must burn as
   exactly those two cue texts; ``สวยแพง`` / ``มากทรงสวย`` is forbidden.
2. ``คลาสสิก`` mid-word orphan — no cue may be ``ก`` (or any
   single-char orphan); ``โทนสีดำคลาสสิก`` stays one intact cue text.
3. ASR spelling — when STT hears ``อย่าบในภาพ`` but the script says
   ``อย่างในภาพ``, the on-screen text MUST be ``อย่างในภาพ``; ``อย่าบ``
   must never appear in the ASS.

Plus an end-to-end smoke: synthetic 1080x1920 clip + script file ->
pipeline (words-json path, no network) -> burned MP4 with exact cue
texts in the ASS, geometry/duration/audio QA, 3 frames.

Run from the skill root:

    python3 tests/test_primary_bag_demo.py
    # or
    python3 -m unittest tests.test_primary_bag_demo -v

Stdlib unittest only (subprocess for ffmpeg; burn tests skip if
ffmpeg/ffprobe are missing).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "render"))

from script_align import align_script_to_timeline  # noqa: E402
from script_ass import build_ass  # noqa: E402
from script_burn import ASS_FILENAME, FONTS_SUBDIR  # noqa: E402
from script_pipeline import run_script_pipeline  # noqa: E402


def W(word: str, start: float, end: float) -> dict:
    """One Groq verbose_json style word entry (timing only)."""
    return {"word": word, "start": start, "end": end}


def dialogue_texts(ass_text: str) -> list[str]:
    """On-screen texts of every Dialogue event (tag prefix stripped)."""
    texts = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        payload = line.split(",", 9)[-1]
        payload = re.sub(r"^\{[^}]*\}", "", payload)
        texts.append(payload)
    return texts


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class TestPrimaryBagDemoMakAttachment(unittest.TestCase):
    """SPEC row: script สวยแพงมาก / ทรงสวย; forbidden สวยแพง / มากทรงสวย."""

    SCRIPT = ["สวยแพงมาก", "ทรงสวย"]

    def test_mak_stays_with_first_cue(self):
        words = [W("สวยแพง", 0.0, 0.5), W("มาก", 0.5, 0.8),
                 W("ทรงสวย", 0.9, 1.6)]
        alignment = align_script_to_timeline(self.SCRIPT, words)
        self.assertEqual(alignment["mismatches"], [],
                         f"primary demo must align cleanly: {alignment['mismatches']}")
        ass_text = build_ass(alignment)
        texts = dialogue_texts(ass_text)
        self.assertEqual(texts, self.SCRIPT)
        # Forbidden repackings from the old ASR-as-text path:
        self.assertNotIn("สวยแพง", texts)
        self.assertNotIn("มากทรงสวย", texts)
        for bad in ("สวยแพง", "มากทรงสวย"):
            self.assertNotIn(bad, ass_text.split("Dialogue:")[-1])

    def test_mak_attachment_alt_word_chopping(self):
        # Same audio, different Whisper chopping — text still exact.
        words = [W("สวย", 0.0, 0.3), W("แพงมาก", 0.3, 0.8),
                 W("ทรง", 0.9, 1.1), W("สวย", 1.1, 1.6)]
        alignment = align_script_to_timeline(self.SCRIPT, words)
        self.assertEqual(alignment["mismatches"], [])
        self.assertEqual(dialogue_texts(build_ass(alignment)), self.SCRIPT)


class TestPrimaryBagDemoKlasikOrphan(unittest.TestCase):
    """SPEC row: คลาสสิก must never split to mid-word orphan ก."""

    def test_klasik_never_orphans_kor_kai(self):
        words = [W("โทนสีดำคลาสสิก", 2.0, 3.2)]
        alignment = align_script_to_timeline(["โทนสีดำคลาสสิก"], words)
        self.assertEqual(alignment["mismatches"], [])
        texts = dialogue_texts(build_ass(alignment))
        self.assertEqual(texts, ["โทนสีดำคลาสสิก"])
        for cue in texts:
            self.assertNotEqual(cue, "ก")
            self.assertGreater(len(cue), 1,
                               f"single-char orphan cue forbidden, got {cue!r}")
        self.assertIn("คลาสสิก", texts[0])

    def test_no_orphan_across_two_line_script(self):
        words = [W("โทนสีดำคลาสสิก", 0.0, 0.9),
                 W("อย่างในภาพ", 1.0, 1.8)]
        script = ["โทนสีดำคลาสสิก", "อย่างในภาพ"]
        alignment = align_script_to_timeline(script, words)
        self.assertEqual(alignment["mismatches"], [])
        texts = dialogue_texts(build_ass(alignment))
        self.assertEqual(texts, script)
        joined = "".join(texts)
        self.assertNotIn("คลาสิ", joined.replace("คลาสสิก", ""))
        for cue in texts:
            self.assertNotEqual(cue.strip(), "ก")


class TestPrimaryBagDemoAsrSpelling(unittest.TestCase):
    """SPEC row: STT hears อย่าบในภาพ; screen must show อย่างในภาพ."""

    def test_asr_misspelling_never_becomes_display_text(self):
        words = [W("โทนสีดำคลาสสิก", 0.0, 0.9),
                 W("อย่าบในภาพ", 1.0, 1.8)]
        script = ["โทนสีดำคลาสสิก", "อย่างในภาพ"]
        alignment = align_script_to_timeline(script, words)
        self.assertEqual(alignment["mismatches"], [])
        ass_text = build_ass(alignment)
        texts = dialogue_texts(ass_text)
        self.assertEqual(texts, script)
        self.assertEqual(texts[1], "อย่างในภาพ")
        self.assertNotIn("อย่าบ", ass_text)


@unittest.skipUnless(have_ffmpeg(), "ffmpeg/ffprobe not installed")
class TestPrimaryBagDemoSmoke(unittest.TestCase):
    """Synthetic 1080x1920 clip + script file -> pipeline -> MP4 + QA."""

    def test_pipeline_burns_exact_script_texts(self):
        script = ["สวยแพงมาก", "ทรงสวย"]
        words = {"words": [W("สวยแพง", 0.0, 0.5), W("มาก", 0.5, 0.8),
                           W("ทรงสวย", 0.9, 1.6)]}
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            src = tmpdir / "bag_demo_src.mp4"
            gen = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i",
                 "testsrc2=size=1080x1920:rate=30:duration=2",
                 "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-shortest",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac",
                 str(src)],
                capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            script_txt = tmpdir / "bag_demo_script.txt"
            script_txt.write_text("\n".join(script) + "\n", encoding="utf-8")
            words_json = tmpdir / "bag_demo_words.json"
            import json

            words_json.write_text(
                json.dumps(words, ensure_ascii=False), encoding="utf-8")
            out = tmpdir / "bag_demo_burned.mp4"
            frames = tmpdir / "qa"
            result = run_script_pipeline(
                src, script_txt, ROOT / "fonts", out,
                frames_dir=frames, words_json=words_json)
            self.assertTrue(result["ok"], result.get("report"))
            self.assertEqual(result["filter"],
                             f"ass={ASS_FILENAME}:fontsdir={FONTS_SUBDIR}")
            self.assertEqual((result["width"], result["height"]),
                             (1080, 1920))
            self.assertLessEqual(result["duration_delta"], 0.1)
            self.assertTrue(result["has_audio"])
            self.assertEqual(len(result["frames"]), 3)
            for frame in result["frames"]:
                self.assertTrue(Path(frame).is_file())
                self.assertGreater(Path(frame).stat().st_size, 0)
            self.assertTrue(out.is_file() and out.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
