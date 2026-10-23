#!/usr/bin/env python3
"""Ticket 04 tests: FFmpeg burn with Lip traps + ffprobe/QA frames.

- Gate blocked -> burn REFUSES (MismatchGateError), ffmpeg never runs.
- Filter string is ``ass=sub.ass:fontsdir=fdir`` ONLY: never
  ``subtitles=``, no drive letters, no path separators.
- Audio path is ``-c:a copy``; failures that implicate the audio
  copy raise AudioReencodeRequiredError (stop-and-ask), never a
  silent re-encode.
- Integration (real ffmpeg/ffprobe, tiny synthetic 1080x1920 clip):
  geometry preserved, duration delta <= 0.1s, audio kept, 3 PNGs.

Run from the skill root:

    python3 tests/test_script_burn.py
    # or
    python3 -m unittest tests.test_script_burn -v

Stdlib unittest only (subprocess for ffmpeg; skipped if missing).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "render"))

from script_align import align_script_to_timeline  # noqa: E402
from script_burn import (  # noqa: E402
    ASS_FILENAME,
    FONTS_SUBDIR,
    AudioReencodeRequiredError,
    BurnError,
    build_filter_string,
    burn_script_aligned,
)
from script_gate import MismatchGateError  # noqa: E402


def W(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


def good_alignment() -> dict:
    script = ["สวยแพงมาก", "ทรงสวย"]
    words = [W("สวยแพง", 0.0, 0.5), W("มาก", 0.5, 0.8),
             W("ทรงสวย", 0.9, 1.6)]
    alignment = align_script_to_timeline(script, words)
    assert not alignment["mismatches"], alignment["mismatches"]
    return alignment


def blocked_alignment() -> dict:
    words = [W("สวัสดี", 0.0, 0.5), W("ครับ", 0.5, 0.8)]
    alignment = align_script_to_timeline(
        ["สวัสดีครับ", "ราคาเก้าพันบาท"], words)
    assert alignment["mismatches"], "fixture must be gate-blocked"
    return alignment


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


class TestFilterTraps(unittest.TestCase):
    def test_filter_is_ass_only(self):
        vf = build_filter_string()
        self.assertEqual(vf, f"ass={ASS_FILENAME}:fontsdir={FONTS_SUBDIR}")
        self.assertIn("ass=", vf)
        self.assertNotIn("subtitles=", vf)

    def test_filter_has_no_drive_letters_or_separators(self):
        vf = build_filter_string()
        self.assertNotIn("C:", vf)
        self.assertNotIn("D:", vf)
        # Bare filenames only: the only ':' chars are the two
        # separators inside the filter shape itself.
        self.assertEqual(vf.count(":"), 1)
        self.assertNotIn("/", vf)
        self.assertNotIn("\\", vf)

    def test_subtitles_shape_refused(self):
        with self.assertRaises(ValueError):
            build_filter_string(ass_filename="subtitles=sub.ass")
        with self.assertRaises(ValueError):
            build_filter_string(ass_filename="C:/tmp/sub.ass")
        with self.assertRaises(ValueError):
            build_filter_string(fonts_subdir="/tmp/fdir")


class TestGateRefusal(unittest.TestCase):
    def test_burn_refuses_when_gate_blocked(self):
        import script_burn as sb

        calls: list = []
        orig = sb.run_ffmpeg_burn
        sb.run_ffmpeg_burn = lambda *a, **k: calls.append((a, k))  # type: ignore
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "out.mp4"
                with self.assertRaises(MismatchGateError):
                    burn_script_aligned(
                        Path(tmp) / "clip.mp4", blocked_alignment(),
                        ROOT / "fonts", out)
                # ffmpeg must never run; no output may appear.
                self.assertEqual(calls, [])
                self.assertFalse(out.exists())
        finally:
            sb.run_ffmpeg_burn = orig

    def test_burn_needs_no_real_video_when_blocked(self):
        # Gate refusal happens before any input-file check.
        with self.assertRaises(MismatchGateError):
            burn_script_aligned(
                "/nonexistent/clip.mp4", blocked_alignment(),
                ROOT / "fonts", "/nonexistent/out.mp4")


class TestAudioCopyTrap(unittest.TestCase):
    def test_no_silent_reencode_flag_anywhere(self):
        import script_burn as sb

        # Executable artifacts only (docstrings/comments legitimately
        # name the forbidden filter to document the refusal).
        self.assertNotIn("subtitles=", sb.FILTER_STRING)
        self.assertEqual(sb.build_filter_string(),
                         "ass=sub.ass:fontsdir=fdir")
        import inspect
        code = inspect.getsource(sb.run_ffmpeg_burn)
        body = "\n".join(
            ln for ln in code.splitlines()
            if ln.strip() and not ln.strip().startswith(("#", '"""', "''")))
        self.assertIn('"copy"', body)
        # An 'aac' fallback would be a silent re-encode path; the
        # ask-path error message may name it as an example, but no
        # ffmpeg command may carry it.
        self.assertNotIn('"aac"', body)
        self.assertNotIn("'aac'", body)

    def test_audio_copy_failure_maps_to_ask_error(self):
        import script_burn as sb

        self.assertTrue(
            sb._looks_like_audio_copy_failure(
                "Could not write header for output file: "
                "muxer does not support -c:a copy with audio stream"))
        self.assertFalse(
            sb._looks_like_audio_copy_failure("x264 encoder error"))


@unittest.skipUnless(have_ffmpeg(), "ffmpeg/ffprobe not installed")
class TestSyntheticBurnIntegration(unittest.TestCase):
    """Tiny 1080x1920 source -> real burn -> QA gates + 3 frames."""

    def test_burn_preserves_geometry_duration_audio(self):
        alignment = good_alignment()
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # 2s portrait clip with a tone audio track (aac in mp4).
            src = tmpdir / "src.mp4"
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
            out = tmpdir / "burned.mp4"
            frames = tmpdir / "qa"
            result = burn_script_aligned(
                src, alignment, ROOT / "fonts", out, frames_dir=frames)
            self.assertTrue(result["ok"])
            self.assertEqual(result["filter"],
                             f"ass={ASS_FILENAME}:fontsdir={FONTS_SUBDIR}")
            self.assertNotIn("subtitles=", result["filter"])
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
