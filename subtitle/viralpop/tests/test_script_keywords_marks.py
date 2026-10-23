#!/usr/bin/env python3
"""v1.1 T3+T4+T5 tests: keyword yellow + mark overlays + geometry lock + QA.

T3: keywords colorize SCRIPT substrings same-size yellow AFTER T2 split
    (per piece, never merging); empty = no yellow; no \\\\N.
T4: stacked-mark cues emit Layer1 overlays sharing anim tags + active
    color; single-mark controls are a no-op; base text stays verbatim.
T5: geometry/style locked to proven batch01 values in BOTH script_ass
    and script_width; content QA refuses overlap / zero-duration; width
    gate holds after split.

Run from the skill root:

    python3 tests/test_script_keywords_marks.py
    # or
    python3 -m unittest tests.test_script_keywords_marks -v

Stdlib unittest only.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))

import script_ass  # noqa: E402
import script_marks  # noqa: E402
import script_width  # noqa: E402
from script_align import align_script_to_timeline  # noqa: E402
from script_ass import build_ass  # noqa: E402
from script_keywords import (  # noqa: E402
    YELLOW_CLOSE,
    YELLOW_OPEN,
    colorize,
    has_yellow,
    load_keywords_job,
    strip_tags,
    yellow_dialogue_count,
)
from script_marks import (
    assert_no_overlap,
    assert_no_zero_duration,
    content_qa,
    count_stacked,
)
from script_timing import OVERLAP_EPS, trim_overlaps  # noqa: E402
from script_split import split_alignment, split_matched  # noqa: E402
from script_width import assert_fit, budget_px  # noqa: E402


def W(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


def dialogue_lines(ass_text: str) -> list[str]:
    return [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]


def layer_of(line: str) -> str:
    return line.split(",", 1)[0].split(":")[1].strip()


def payload_of(line: str) -> str:
    return line.split(",", 9)[9]


def visible_texts(ass_text: str, layer: str = "0") -> list[str]:
    return [
        re.sub(r"\{[^}]*\}", "", payload_of(ln))
        for ln in dialogue_lines(ass_text)
        if layer_of(ln) == layer
    ]


JOB_CLIP04 = Path("/home/box/subtitle-work/jobs/clip04.json")


class TestT3Colorize(unittest.TestCase):
    def test_yellow_woven_same_size_no_rewrite(self):
        out = colorize("สวยแพงมาก", ["สวยแพง"])
        self.assertIn(YELLOW_OPEN, out)
        self.assertIn("สวยแพง", out)
        self.assertEqual(strip_tags(out), "สวยแพงมาก")
        self.assertNotIn(r"\N", out)

    def test_empty_keywords_no_yellow(self):
        for kw in (None, [], [""]):
            out = colorize("สวยแพงมาก", kw)
            self.assertNotIn(YELLOW_OPEN, out)
            self.assertEqual(out, "สวยแพงมาก")

    def test_longest_term_first(self):
        # Longest-first replace order (same as the proven old colorize):
        # disjoint terms each get exactly one run; wording intact.
        out = colorize("กระเป๋าหนังสีดำสวยแพงมาก",
                       ["สวยแพง", "กระเป๋าหนังสีดำ"])
        self.assertEqual(out.count(YELLOW_OPEN), 2)
        self.assertIn(YELLOW_OPEN + "สวยแพง" + YELLOW_CLOSE, out)
        self.assertEqual(strip_tags(out), "กระเป๋าหนังสีดำสวยแพงมาก")

    def test_load_job_keywords_clip04(self):
        kws = load_keywords_job(JOB_CLIP04)
        self.assertEqual(len(kws), 9)
        for must in ("กระเป๋าหนังสีดำ", "สวยแพง", "ตะกร้าเหลือง"):
            self.assertIn(must, kws)

    def test_build_ass_yellow_present_and_absent(self):
        script = ["สวยแพงมาก", "ทรงสวย"]
        words = [W("สวยแพงมาก", 0.0, 0.6), W("ทรงสวย", 0.7, 1.4)]
        alignment = align_script_to_timeline(script, words)
        plain = build_ass(alignment)
        self.assertNotIn(YELLOW_OPEN, plain)
        yellow = build_ass(alignment, keywords=["สวยแพง"])
        self.assertIn(YELLOW_OPEN, yellow)
        self.assertEqual(visible_texts(yellow), script)
        self.assertNotIn(r"\N", yellow)

    def test_yellow_survives_t2_split_per_piece(self):
        long_line = (
            "กระเป๋าหนังสีดำที่แมตช์ง่ายกับทุกลุคแนะนำใบนี้เลยค่ะสวยแพงมาก"
        )
        words = [W(long_line, 1.0, 6.0)]
        alignment = align_script_to_timeline([long_line], words)
        split = split_alignment(alignment)
        self.assertGreater(len(split["matched"]), 1)
        ass_text = build_ass(split, keywords=["สวยแพง"])
        # concat of Layer0 visible pieces == script line exactly
        self.assertEqual("".join(visible_texts(ass_text)), long_line)
        # the piece holding the keyword carries yellow; pieces unmerged
        layered = [ln for ln in dialogue_lines(ass_text)
                   if layer_of(ln) == "0"]
        self.assertEqual(len(layered),
                         len(split["matched"]))
        self.assertTrue(any(YELLOW_OPEN in ln for ln in layered))
        # every emitted sub-cue still fits the locked budget
        assert_fit(split["matched"])


class TestT4Overlays(unittest.TestCase):
    """T4 RETIRED 2026-09-24: overlays CAUSED tofu/float (C6 QA evidence).

    Plain unsplit Prompt Bold shapes stacked marks correctly, so
    stacked-mark cues now emit Layer0 only — no Layer1, no hidden-mark
    tags. Detection (count_stacked) is kept for QA reporting.
    """

    def test_stacked_cue_emits_no_layer1(self):
        script = ["ที่นี่มีน้ำ"]
        words = [W("ที่นี่มีน้ำ", 0.0, 1.0)]
        alignment = align_script_to_timeline(script, words)
        ass_text = build_ass(alignment)
        layers = [layer_of(ln) for ln in dialogue_lines(ass_text)]
        self.assertEqual(layers, ["0"])
        self.assertEqual(visible_texts(ass_text), script)
        self.assertGreater(count_stacked("ที่นี่มีน้ำ"), 0)  # still detected

    def test_single_mark_control_is_noop(self):
        script = ["เก่งเป็นคน"]
        words = [W("เก่งเป็นคน", 0.0, 1.0)]
        alignment = align_script_to_timeline(script, words)
        ass_text = build_ass(alignment)
        layers = [layer_of(ln) for ln in dialogue_lines(ass_text)]
        self.assertEqual(layers, ["0"])
        self.assertEqual(visible_texts(ass_text), script)

    def test_no_layer1_anywhere_with_keywords(self):
        # retired: no overlays even with keywords — yellow lives on Layer0
        script = ["ที่นี่มีน้ำ"]
        words = [W("ที่นี่มีน้ำ", 0.5, 1.5)]
        alignment = align_script_to_timeline(script, words)
        ass_text = build_ass(alignment, keywords=["น้ำ"])
        lines = dialogue_lines(ass_text)
        self.assertEqual([layer_of(ln) for ln in lines], ["0"])
        self.assertIn(YELLOW_OPEN, lines[0])
        self.assertEqual(visible_texts(ass_text, layer="0"), script)

    def test_negation_keyword_highlights_whole_phrase(self):
        # C6 QA 2026-09-24: lone yellow "ปวดไหล่" inside "ไม่ปวดไหล่"
        # reads as a product flaw — negation must highlight whole phrase
        out = colorize("ไม่ปวดไหล่", ["ปวดไหล่"])
        self.assertEqual(strip_tags(out), "ไม่ปวดไหล่")
        self.assertTrue(out.startswith(YELLOW_OPEN))
        self.assertTrue(out.endswith(YELLOW_CLOSE))
        # non-adjacent negation does NOT extend
        out2 = colorize("อย่าลืมพิกัด", ["พิกัด"])
        self.assertNotIn("อย่าลืม" + YELLOW_CLOSE, out2)
        self.assertIn(YELLOW_OPEN + "พิกัด" + YELLOW_CLOSE, out2)

    def test_base_text_stays_verbatim_with_marks(self):
        script = ["ที่นี่มีน้ำ"]
        words = [W("ที่นี่มีน้ำ", 0.0, 1.0)]
        alignment = align_script_to_timeline(script, words)
        ass_text = build_ass(alignment, keywords=["น้ำ"])
        self.assertEqual(visible_texts(ass_text, layer="0"), script)


class TestT5GeometryLock(unittest.TestCase):
    def test_locked_values(self):
        self.assertEqual(script_ass.FONTSIZE, 104)
        self.assertEqual((script_ass.MARGIN_L, script_ass.MARGIN_R,
                          script_ass.MARGIN_V), (47, 47, 158))
        self.assertEqual((script_ass.POS_X, script_ass.POS_Y), (540, 1580))
        self.assertEqual((script_ass.OUTLINE, script_ass.SHADOW), (3, 1))
        self.assertEqual(script_width.FONTSIZE, 104)
        self.assertEqual((script_width.MARGIN_L, script_width.MARGIN_R),
                         (47, 47))
        self.assertAlmostEqual(budget_px(), (1080 - 47 - 47) * 0.94,
                               places=6)
        self.assertAlmostEqual(budget_px(), 926.84, places=6)

    def test_style_line_carries_lock(self):
        script = ["สวยแพงมาก"]
        alignment = align_script_to_timeline(
            script, [W("สวยแพงมาก", 0.0, 0.6)])
        ass_text = build_ass(alignment)
        style = next(ln for ln in ass_text.splitlines()
                     if ln.startswith("Style:"))
        self.assertIn(",104,", style)
        self.assertIn(",1,3,1,2,", style)  # BorderStyle 1, Outline 3, Shadow 1
        self.assertIn(",47,47,158,", style)
        self.assertIn(r"\pos(540,1580)", ass_text)

    def test_clip04_script_all_fit_after_split(self):
        script = [
            ln.strip()
            for ln in Path(
                "/home/box/subtitle-work/scripts/batch01_v1/clip04.txt"
            ).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        self.assertEqual(len(script), 10)
        t = 0.0
        matched = []
        for i, line in enumerate(script):
            matched.append({"line_index": i, "script_line": line,
                            "text": line, "display_text": line,
                            "start": t, "end": t + 2.0})
            t += 2.1
        expanded = split_matched(matched)
        report = assert_fit(expanded)
        self.assertIn("0 over", report)

    def test_overlap_trimmable_then_builds(self):
        # T5-fix: slight STT-jitter overlap is TRIMMED (text/onsets
        # untouched), not refused — build_ass receives clean spans.
        matched = [
            {"line_index": 0, "script_line": "สวยแพงมาก",
             "start": 0.0, "end": 1.0},
            {"line_index": 1, "script_line": "ทรงสวย",
             "start": 0.5, "end": 1.5},
        ]
        trimmed = trim_overlaps([dict(m) for m in matched])
        self.assertAlmostEqual(trimmed[0]["end"], 0.5 - OVERLAP_EPS)
        self.assertEqual(trimmed[0]["script_line"], "สวยแพงมาก")
        self.assertEqual(trimmed[1]["start"], 0.5)  # onsets untouched
        assert_no_overlap(trimmed)  # clean spans pass strict QA
        ass_text = build_ass(matched)  # build_ass trims defensively
        self.assertIn("สวยแพงมาก", ass_text)

    def test_overlap_severe_still_refused(self):
        # T5-fix: overlap the trim cannot save (would leave < min
        # duration) still FAILS LOUD — same spirit as cascade-refuse.
        matched = [
            {"line_index": 0, "script_line": "สวยแพงมาก",
             "start": 0.0, "end": 1.0},
            {"line_index": 1, "script_line": "ทรงสวย",
             "start": 0.02, "end": 1.5},
        ]
        with self.assertRaises(ValueError):
            trim_overlaps(matched)
        with self.assertRaises(ValueError):
            build_ass(matched)

    def test_zero_duration_refused(self):
        matched = [{"line_index": 0, "script_line": "สวยแพงมาก",
                    "start": 1.0, "end": 1.0}]
        with self.assertRaises(ValueError):
            assert_no_zero_duration(matched)
        with self.assertRaises(ValueError):
            build_ass(matched)

    def test_content_qa_overlay_count_on_stacked_fixture(self):
        matched = [
            {"line_index": 0, "script_line": "ที่นี่มีน้ำ",
             "start": 0.0, "end": 1.0},
            {"line_index": 1, "script_line": "ทรงสวย",
             "start": 1.1, "end": 2.0},
        ]
        qa = content_qa(matched)
        self.assertEqual(qa["cues"], 2)
        # retired 2026-09-24: detection kept, overlays no longer emitted
        self.assertEqual(qa["overlays"], 0)
        self.assertGreater(qa["stacked_cues"], 0)

    def test_clip04_keyword_coverage(self):
        """All 8 matchable clip04 keywords render yellow on Layer0.

        clip04 script says 'ทำงานก็รอด' where the job keyword is
        'เอาอยู่', so เอาอยู่ can never match SCRIPT text (by design:
        STT/job wording must not leak onto screen). The other 8 hit as
        whole pieces — including ตุ๊กตา/คอมพลีทลุค/ตะกร้าเหลือง, which
        only survive splitting when the real newmm word tokenizer runs
        (venv python with pythainlp; the stdlib fallback severs them).
        With the overlay layer retired, every yellow line here is a
        true Layer0 cue (old count mixed in 3 overlay headers).
        """
        kws = load_keywords_job(JOB_CLIP04)
        script = [
            ln.strip()
            for ln in Path(
                "/home/box/subtitle-work/scripts/batch01_v1/clip04.txt"
            ).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        hit = [k for k in kws if any(k in ln for ln in script)]
        miss = [k for k in kws if k not in hit]
        self.assertEqual(miss, ["เอาอยู่"])
        # Full new decision path (Codex review 2026-09-24): plan each
        # line inside a REALISTIC slot (production slots come from STT;
        # the old arbitrary 2.0s windows starve long lines and produce
        # unresolvable shorts), merge within lines, then burn. Zero
        # planner problems expected on realistic slots. (0.30s/syl here
        # is relaxed on purpose: this test proves the PATH, while
        # tighter real slots must surface verdicts, not silent cues.
        # (0.35s/syl absorbs float-borderline 2-syl pieces like ตุ๊กตา.)
        from script_cue_split import plan_cues
        from script_timing import merge_short_cues
        from thai_line_split import count_syllables
        t = 0.0
        pos = 0
        sentences = []
        for i, line in enumerate(script):
            dur = max(1.2, count_syllables(line) * 0.35)
            sentences.append({"line": line, "start": t, "end": t + dur,
                              "line_index": i,
                              "char_a": pos, "char_b": pos + len(line)})
            pos += len(line)
            t += dur + 0.1
        cues, problems = plan_cues(sentences, keywords=kws)
        self.assertEqual(problems, [])
        cues, report = merge_short_cues(cues, allow_cross_line=False)
        self.assertEqual(report["skipped"], [])
        alignment = {"matched": cues, "mismatches": []}
        ass_text = build_ass(alignment, keywords=kws)
        self.assertEqual(yellow_dialogue_count(ass_text), 8)


class TestResolveKeywordsGuard(unittest.TestCase):
    """resolve_keywords must fail LOUD on comma-strings (Lip-locked
    2026-09-25: a bare str char-splits via list(), which killed all
    yellow + caused false R4 negation refusals on client-batch1)."""

    def test_comma_string_refused(self):
        from script_pipeline import resolve_keywords  # noqa: E402
        with self.assertRaises(ValueError) as ctx:
            resolve_keywords("ตะกร้าด้านล่าง,กระเป๋าใบเก่ง", None)
        self.assertIn("LIST", str(ctx.exception))

    def test_list_passes_through(self):
        from script_pipeline import resolve_keywords  # noqa: E402
        kws = ["ตะกร้าด้านล่าง", "กระเป๋าใบเก่ง"]
        self.assertEqual(resolve_keywords(kws, None), kws)


class TestR5LoanwordIntegrity(unittest.TestCase):
    """R5: a cut strictly inside a protected term refuses loudly.

    Proven case that motivated R5 (2026-09-29, C3): misspelled กิมมิก
    tokenized to กค/วาม and the splitter severed ความ mid-word, burning
    กิมมิกค/วามเท่. The end-to-end splitter test below locks the real
    regression (registered variants never sever); the refusal test locks
    the R5 mechanism itself on a canonical term."""

    def _cues(self, *texts):
        cues = []
        t = 0.0
        for text in texts:
            # Real split output carries the fragment in script_line too.
            cues.append({"line_index": 0, "script_line": text,
                         "display_text": text,
                         "start": t, "end": t + 1.0})
            t += 1.1
        return cues

    def test_severed_term_refused(self):
        from script_marks import validate_final_cues  # noqa: E402
        cues = self._cues("ไปที่ตะกร้", "าด้านล่างนะ")
        with self.assertRaises(ValueError) as ctx:
            validate_final_cues(cues, [], None, profile="vo")
        self.assertIn("ตะกร้า", str(ctx.exception))  # canonical named

    def test_clean_split_passes(self):
        from script_marks import validate_final_cues  # noqa: E402
        cues = self._cues("แต่ซ่อนกิมมิค", "ความเท่ไว้ในทรง ")
        result = validate_final_cues(cues, [], None, profile="vo")
        self.assertEqual(result["cues"], 2)

    def test_split_never_severs_registered_variant(self):
        # End-to-end through the real splitter: even the misspelled line
        # must not produce a cue boundary inside the variant span.
        from script_split import split_alignment  # noqa: E402
        line = ("ดีไซน์เรียบง่ายแต่ซ่อนกิมมิกความเท่ไว้ในทรง "
                "สะพายแล้วดูเป็นคนใส่ใจในรายละเอียดการแต่งตัว")
        words = []
        t = 0.0
        for token in ["ดีไซน์", "เรียบง่าย", "แต่", "ซ่อน", "กิมมิก",
                      "ความเท่", "ไว้", "ใน", "ทรง", "สะพาย", "แล้ว",
                      "ดู", "เป็น", "คน", "ใส่ใจ", "ใน", "รายละเอียด",
                      "การ", "แต่งตัว"]:
            words.append({"word": token, "start": t, "end": t + 0.5})
            t += 0.5
        alignment = {"matched": [{
            "line_index": 0, "script_line": line, "text": line,
            "display_text": line, "start": 0.0, "end": t,
            "source_char_start": 0, "source_char_end": 10 ** 6,
            "text_match_ratio": 1.0}], "mismatches": []}
        split = split_alignment(alignment, words=words, keywords=[])
        frags = [(c.get("display_text") or c.get("text"))
                 for c in split["matched"]]
        for prev, cur in zip(frags, frags[1:]):
            joined, cut = prev + cur, len(prev)
            for bad in ("กิมมิก",):
                for i in range(len(joined) - len(bad) + 1):
                    if joined[i:i + len(bad)] == bad:
                        self.assertFalse(
                            i < cut < i + len(bad),
                            f"split severed {bad!r}: {prev!r} | {cur!r}")


class TestKeywordCoverageGate(unittest.TestCase):
    """KEYWORD-COVERAGE GATE: a keyword with zero verbatim hits refuses
    before ASS/burn (silent zero-yellow class). Proves wiring through
    run_script_pipeline on a synthetic 2s clip; unit-proves the counter."""

    def test_check_keyword_hits_counts(self):
        from script_pipeline import resolve_keywords  # noqa: E402
        from script_keywords import check_keyword_hits  # noqa: E402
        lines = ["ลองดูพิกัดที่ตะกร้าด้านล่างได้เลย", "ถูกใจจนเป็นใบโปรดไปแล้ว"]
        hits = check_keyword_hits(lines, resolve_keywords(
            ["ตะกร้าด้านล่าง", "ใบโปรด", "คุ้มค่า"], None))
        self.assertEqual(hits, {"ตะกร้าด้านล่าง": 1, "ใบโปรด": 1,
                                "คุ้มค่า": 0})

    def test_zero_hit_keyword_blocks_burn(self):
        import json
        import subprocess
        import tempfile
        from script_pipeline import run_script_pipeline  # noqa: E402
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            src = tmpdir / "kw_src.mp4"
            gen = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
                capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            script_txt = tmpdir / "kw_script.txt"
            script_txt.write_text("ลองดูพิกัดที่ตะกร้าด้านล่างได้เลย\n",
                                  encoding="utf-8")
            words = {"words": [
                {"word": "ลองดู", "start": 0.0, "end": 0.4},
                {"word": "พิกัด", "start": 0.4, "end": 0.7},
                {"word": "ที่", "start": 0.7, "end": 0.8},
                {"word": "ตะกร้า", "start": 0.8, "end": 1.1},
                {"word": "ด้านล่าง", "start": 1.1, "end": 1.5},
                {"word": "ได้เลย", "start": 1.5, "end": 1.9}],
                "segments": [{"start": 0.0, "end": 1.9,
                              "text": "ลองดูพิกัดที่ตะกร้าด้านล่างได้เลย"}],
                "text": "ลองดูพิกัดที่ตะกร้าด้านล่างได้เลย"}
            words_json = tmpdir / "kw_words.json"
            words_json.write_text(json.dumps(words, ensure_ascii=False),
                                  encoding="utf-8")
            out = tmpdir / "kw_burned.mp4"
            result = run_script_pipeline(
                src, script_txt, root / "fonts", out,
                words_json=words_json,
                keywords=["ตะกร้าด้านล่าง", "คำที่ไม่มีในบท"])
            self.assertFalse(result["ok"])
            self.assertIn("KEYWORD-COVERAGE", result["report"])
            self.assertEqual(result["keyword_misses"], ["คำที่ไม่มีในบท"])
            self.assertFalse(out.is_file(), "refused burn must write nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
