#!/usr/bin/env python3
"""Ticket STT-guard: detect collapsed segments, re-transcribe, merge.

- C4 fixture (real batch2 JSON): one 20.4s garbage segment => 1 suspect range.
- Clean C3 fixture => no suspects.
- merge_chunk: chunk offsets land correctly, streams stay sorted.
- transcribe_guarded with a fake transcriber (no network): bad range is
  retried once and merged; a still-bad chunk keeps the original loudly.

Run from the skill root:
    python3 -m pytest tests/test_stt_guard.py -q
Stdlib unittest only (+ pytest runner). No network.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

from stt_guard import (  # noqa: E402
    find_suspect_ranges,
    merge_chunk,
    transcribe_guarded,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "stt_guard"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def seg(start, end, text="x"):
    return {"start": start, "end": end, "text": text}


def word(token, start, end):
    return {"word": token, "start": start, "end": end}


def words_n(n, start, end):
    if n <= 0:
        return []
    step = (end - start) / n
    return [word(f"w{i}", start + i * step, start + (i + 1) * step)
            for i in range(n)]


class TestFindSuspects(unittest.TestCase):
    def test_c4_collapsed_segment_detected(self):
        data = load("copy_DDFB850F-60C6-4669-BB06-C7F87E79544C.json.full-stt-bak")
        ranges = find_suspect_ranges(data)
        self.assertEqual(len(ranges), 1)
        start, end, reason = ranges[0]
        self.assertAlmostEqual(start, 8.50, places=1)
        self.assertAlmostEqual(end, 28.90, places=1)
        self.assertIn("collapsed", reason)

    def test_clean_clip_has_no_suspects(self):
        data = load("copy_77E700B1-BF57-463A-BB99-9485207E3F02.json")
        self.assertEqual(find_suspect_ranges(data), [])

    def test_empty_long_segment_flagged(self):
        data = {"segments": [seg(0.0, 5.0, "silence?")], "words": []}
        ranges = find_suspect_ranges(data)
        self.assertEqual(len(ranges), 1)
        self.assertIn("missed speech", ranges[0][2])


class TestMergeChunk(unittest.TestCase):
    def test_c4_merge_offsets(self):
        full = load("copy_DDFB850F-60C6-4669-BB06-C7F87E79544C.json.full-stt-bak")
        chunk = load("c4_tail.json")
        n_full_words = len(full["words"])
        n_chunk_words = len(chunk["words"])
        merged = merge_chunk(full, chunk, 8.50, 28.90)
        # Garbage seg dropped, 3 neighbors + 5 chunk segs survive.
        self.assertEqual(len(merged["segments"]), 3 + 5)
        starts = [float(w["start"]) for w in merged["words"]]
        self.assertEqual(starts, sorted(starts))
        # Chunk was cut at base = start - PAD = 8.0: first chunk seg lands there.
        chunk_first = [s for s in merged["segments"]
                       if abs(float(s["start"]) - 8.0) < 0.6]
        self.assertTrue(chunk_first, "chunk segs not offset to base 8.0s")
        # Collapsed-region fiction is gone: word count == kept + chunk.
        # (merge_chunk mutates full in place, so reload for the expectation.)
        fresh = load("copy_DDFB850F-60C6-4669-BB06-C7F87E79544C.json.full-stt-bak")
        kept = [w for w in fresh["words"]
                if (float(w["start"]) + float(w["end"])) / 2 < 8.0
                or (float(w["start"]) + float(w["end"])) / 2 >= 29.4]
        self.assertEqual(len(merged["words"]), len(kept) + n_chunk_words)
        self.assertLess(len(merged["words"]), n_full_words + n_chunk_words)


def make_wav(path, seconds=15):
    p = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)],
        capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-200:]


class TestGuardedFlow(unittest.TestCase):
    def test_bad_range_retried_and_merged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "full.wav"
            make_wav(wav)
            garbage = {"segments": [seg(0.0, 4.0, "ok"),
                                    seg(4.0, 14.0, "garbage")],
                       "words": words_n(12, 0.0, 4.0) + words_n(2, 4.0, 14.0),
                       "text": "ok garbage"}
            good_chunk = {"segments": [seg(0.0, 5.0, "r1"), seg(5.0, 9.5, "r2")],
                          "words": words_n(20, 0.0, 5.0) + words_n(18, 5.0, 9.5),
                          "text": "r1 r2"}
            calls = []

            def fake(path):
                calls.append(Path(path).name)
                return good_chunk if "chunk_" in Path(path).name else garbage

            result = transcribe_guarded(wav, transcriber=fake)
            self.assertEqual(len(calls), 2)  # full + 1 chunk retry
            self.assertEqual(len(result["repairs"]), 1)
            self.assertIn("retranscribed", result["repairs"][0]["status"])
            segs = result["data"]["segments"]
            self.assertEqual([s["text"] for s in segs], ["ok", "r1", "r2"])
            self.assertAlmostEqual(float(segs[1]["start"]), 3.5, places=1)

    def test_still_bad_chunk_keeps_original(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "full.wav"
            make_wav(wav)
            garbage = {"segments": [seg(0.0, 10.0, "garbage")],
                       "words": words_n(2, 0.0, 10.0), "text": "garbage"}

            def fake(path):
                return garbage

            result = transcribe_guarded(wav, transcriber=fake)
            # Full + 1 retry only (no infinite loop).
            self.assertEqual(len(result["repairs"]), 1)
            self.assertIn("kept-original", result["repairs"][0]["status"])
            self.assertEqual(result["data"]["segments"], garbage["segments"])


if __name__ == "__main__":
    unittest.main()
