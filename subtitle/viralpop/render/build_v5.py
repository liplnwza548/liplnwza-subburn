#!/usr/bin/env python3
"""Build V5: V4's proven audio-grounded alignment method, re-applied to a
width-aware phrase list (so no cue overflows the 464px frame during fast
speech), plus the Thai mark-shaping fix (see output/THAI_SHAPING_ROOT_CAUSE.md).

V5 status: approved by the project owner on 2026-09-03 (both the typography
fix and the overflow fix verified on the real video before promotion) and is
now the current golden output, alongside V4 (V4's own timing proof stands;
V5 changes only cue segmentation/rendering on top of it, not the underlying
audio-grounded timestamps).

Does NOT modify or re-run over build_v4.py, tes2_v4.ass,
tes2_v4_transcript.json, or the V4 render -- those remain the reference for
V4's timing proof. Reuses build_v4.py's exact, unmodified alignment
functions (compact/align_maps/phrase_span) by import -- no new timing math
is introduced. The only new input is a width-aware split of the SAME
authored phrase text (see thai_line_split.py); every resulting sub-phrase's
start/end still comes from the same real Whisper per-character timestamps
via the same DP alignment build_v4.py already uses.
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_v4 as v4  # noqa: E402  (reuse compact/align_maps/phrase_span/fmt/ass_escape, unmodified)
import thai_line_split as tls  # noqa: E402
from thai_mark_fix import build_fixed_cue  # noqa: E402

FONT_FILE = ROOT / "fonts" / "Prompt-Bold.ttf"
OUT_JSON = ROOT / "output" / "tes2_v5_transcript.json"
OUT_ASS = ROOT / "output" / "tes2_v5.ass"

FONTSIZE = 46
PLAYRESX = 464
MARGIN_L = 20
MARGIN_R = 20
# Leave a little headroom under the raw available width so the outline (3px)
# and the animation's brief 103% overshoot never touch the frame edge.
SAFETY_FACTOR = 0.94
MAX_WIDTH_PX = (PLAYRESX - MARGIN_L - MARGIN_R) * SAFETY_FACTOR

KEYWORD_TERMS = ["ไอแพด", "แฟลชไดรฟ์", "แอปไฟล์", "แอปรูปภาพ", "กดปุ่มแชร์", "บันทึก", "ข้อมูล", "ความจำ", "เต็ม", "ตัวนี้"]


def keyword_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for term in KEYWORD_TERMS:
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(term)))
            start = idx + len(term)
    return spans


def split_phrases_for_width(phrase_defs: list[tuple[str, int]]) -> list[tuple[str, int]]:
    out = []
    for text, word_count in phrase_defs:
        pieces = tls.split_phrase(text, MAX_WIDTH_PX, keyword_spans(text))
        if len(pieces) == 1:
            out.append((text, word_count))
            continue
        total_len = len(text)
        for piece in pieces:
            wc = max(1, round(word_count * len(piece) / total_len))
            out.append((piece, wc))
    return out


def colorize(text: str) -> str:
    for term in sorted(KEYWORD_TERMS, key=len, reverse=True):
        if term in text:
            text = text.replace(term, r"{\c&H00F0FF&}" + term + r"{\c&HFFFFFF&}")
    return text


def main() -> int:
    tls.init(str(FONT_FILE), FONTSIZE)

    data = json.loads(v4.INPUT_JSON.read_text(encoding="utf-8"))
    if len(data.get("segments", [])) != len(v4.PHRASES):
        raise SystemExit("segment count mismatch")

    new_phrases_by_segment = [split_phrases_for_width(group) for group in v4.PHRASES]

    out_segments = []
    ass_lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {PLAYRESX}", "PlayResY: 848",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,Prompt,{FONTSIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,{MARGIN_L},{MARGIN_R},70,1", "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    total_cues = 0
    split_from = 0
    fixed_cues = 0

    for seg, phrase_defs, orig_phrase_defs in zip(data["segments"], new_phrases_by_segment, v4.PHRASES):
        if len(phrase_defs) != len(orig_phrase_defs):
            split_from += 1
        src_text = v4.compact(seg["text"])
        src_words = seg.get("words", [])
        src_spans = []
        for token_index, token in enumerate(src_words):
            token_text = v4.compact(token.get("word", ""))
            for _ in token_text:
                src_spans.append({"start": float(token["start"]), "end": float(token["end"]), "token": token_index})
        if not src_spans:
            raise SystemExit(f"segment {seg['id']} has no timed characters")
        if "".join(v4.compact(w["word"]) for w in src_words) != src_text or len(src_spans) != len(src_text):
            raise SystemExit(f"segment {seg['id']} lacks consistent character timing")

        phrase_records = []
        source_cursor = 0
        for phrase_index, (text, word_count) in enumerate(phrase_defs):
            target = v4.compact(text)
            remaining_targets = [v4.compact(t) for t, _ in phrase_defs[phrase_index + 1:]]
            min_remaining = 3 * len(remaining_targets)
            best = None
            max_start = max(source_cursor, len(src_text) - min_remaining - 1)
            for start_idx in range(source_cursor, max_start + 1):
                max_end = min(len(src_text), start_idx + len(target) + 15)
                min_end = min(max_end, start_idx + max(1, len(target) - 15))
                for end_idx in range(min_end, max_end + 1):
                    if len(src_text) - end_idx < min_remaining:
                        continue
                    ratio = difflib.SequenceMatcher(None, target, src_text[start_idx:end_idx]).ratio()
                    length_penalty = 0.03 * abs((end_idx - start_idx) - len(target)) / max(1, len(target))
                    score = ratio - length_penalty
                    if best is None or score > best[0]:
                        best = (score, start_idx, end_idx)
            if best is None or best[0] < 0.42:
                raise SystemExit(f"could not align segment {seg['id']} phrase {text!r}; best={best}")
            _, src_a, src_b = best
            source_cursor = src_b
            mapped = list(range(src_a, src_b))
            start = min(float(src_spans[i]["start"]) for i in mapped)
            end = max(float(src_spans[i]["end"]) for i in mapped)
            if not start < end:
                raise SystemExit(f"invalid timing for segment {seg['id']} phrase {text!r}; source={src_a}:{src_b}")
            phrase_records.append({
                "text": text, "word_count": word_count, "start": start, "end": end,
                "source_char_start": src_a, "source_char_end": src_b,
                "text_match_ratio": round(best[0], 4),
            })

        out_segments.append({
            "id": seg["id"], "start": seg["start"], "end": seg["end"],
            "source_asr_text": seg["text"], "corrected_text": "".join(t for t, _ in phrase_defs),
            "alignment_method": "whisper-character-token-timing + monotonic DP text alignment (width-aware phrase split)",
            "alignment_level": "MICRO-PHRASE", "cues": phrase_records,
        })

    for block in out_segments:
        for cue in block["cues"]:
            plain = v4.ass_escape(cue["text"])
            colored = colorize(plain).replace(r"&H00FFFFFF&", r"&HFFFFFF&")
            n = total_cues
            s1 = 96 if n % 3 == 0 else 97
            pos_x, pos_y = 232, 760
            an = r"\an2"
            scale_tags = rf"\fscx{s1}\fscy{s1}\t(0,120,\fscx103\fscy103)\t(120,210,\fscx100\fscy100)"
            tag = f"{{{an}\\pos({pos_x},{pos_y}){scale_tags}}}"

            main_text, overlays = build_fixed_cue(plain, colored, pos_x, pos_y, FONTSIZE, an, scale_tags)
            if overlays:
                fixed_cues += 1

            start, end = v4.fmt(cue["start"]), v4.fmt(cue["end"])
            ass_lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{tag}{main_text}")
            for overlay_text in overlays:
                ass_lines.append(f"Dialogue: 1,{start},{end},Default,,0,0,0,,{overlay_text}")
            total_cues += 1

    result = {
        "source": str(v4.INPUT_JSON),
        "alignment_level": "MICRO-PHRASE",
        "alignment_method": "Whisper per-character token timestamps aligned to corrected spoken text by monotonic dynamic programming; width-aware phrase splitting added on top so no cue overflows the frame; no weighted distribution, VAD, silence or energy timing",
        "segments": out_segments,
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_ASS.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")
    print(f"WROTE {OUT_JSON}")
    print(f"WROTE {OUT_ASS}")
    print(f"CUES {total_cues} (was 20 in V4)  SEGMENTS_SPLIT {split_from}/{len(v4.PHRASES)}  CUES_WITH_MARK_FIX {fixed_cues}")
    for block in out_segments:
        for cue in block["cues"]:
            print(f"{cue['start']:.2f}-{cue['end']:.2f} ({cue['end']-cue['start']:.2f}s) [{cue['word_count']}] {cue['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
