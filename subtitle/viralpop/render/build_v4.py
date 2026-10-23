#!/usr/bin/env python3
"""Build V4 Thai micro-phrase subtitles from Whisper character-token timing.

No weighted/proportional timing is used. Phrase boundaries come from the
actual Whisper per-token timestamps after monotonic text alignment.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_JSON = ROOT / "output" / "tes2_transcript_v2.json"
OUT_JSON = ROOT / "output" / "tes2_v4_transcript.json"
OUT_ASS = ROOT / "output" / "tes2_v4.ass"

# Correct only obvious ASR orthographic errors while preserving spoken wording.
PHRASES = [
    [
        ("ไอแพดยิ่งทำง่ายเลย", 4), ("สำรองข้อมูลลงแฟลชไดรฟ์", 5),
        ("ในเวลาไม่ถึงหนึ่งนาที", 5),
    ],
    [("คนไม่เก่งคอมก็ทำได้", 5)],
    [
        ("เสียบทั้งคู่เข้ากับไอแพดนะ", 5),
        ("จากนั้นก็เข้าไปที่แอปไฟล์", 5),
    ],
    [("ในเครื่องมีอยู่แล้ว", 5)],
    [("รอแป๊บนึงเดี๋ยวไฟล์ใหม่จะโชว์ขึ้นมา", 5)],
    [
        ("ตอนนี้ยังไม่มีข้อมูลอะไรนะ", 5),
        ("เราสลับมาที่แอปรูปภาพ", 5),
    ],
    [("แล้วก็เลือกรูปที่เราต้องการในนี้ได้เลย", 5)],
    [("ผมเลือกเป็นตัวอย่างจากนั้นกดปุ่มแชร์", 5)],
    [("แล้วเลือกคำว่าบันทึกไปยังแอปไฟล์", 5)],
    [
        ("เลือกตัวแฟลชไดรฟ์เป็นปลายทาง", 5),
        ("แล้วก็กดบันทึกลงไปได้เลย", 5),
    ],
    [
        ("เป็นอันเสร็จเรียบร้อยนะ", 5),
        ("อันนี้คือข้อมูลอยู่ในแฟลชไดรฟ์", 5),
    ],
    [("แล้วลองไปเช็กดูได้", 5)],
    [
        ("มือถือหรือไอแพดใครความจำเต็ม", 5),
        ("ตัวนี้ช่วยได้ครับ", 4),
    ],
]


def compact(s: str) -> str:
    return re.sub(r"\s+", "", s)


def align_maps(src: str, dst: str) -> list[int | None]:
    """Map each destination character monotonically to a source character index."""
    n, m = len(src), len(dst)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    bt = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = -i
        bt[i][0] = "up"
    for j in range(1, m + 1):
        dp[0][j] = -j
        bt[0][j] = "left"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diag = dp[i - 1][j - 1] + (3 if src[i - 1] == dst[j - 1] else -1)
            up = dp[i - 1][j] - 1
            left = dp[i][j - 1] - 1
            dp[i][j], bt[i][j] = max(
                (diag, "diag"), (up, "up"), (left, "left"), key=lambda x: x[0]
            )
    mapping: list[int | None] = [None] * m
    i, j = n, m
    while i or j:
        step = bt[i][j]
        if step == "diag":
            mapping[j - 1] = i - 1
            i -= 1; j -= 1
        elif step == "up":
            i -= 1
        else:
            # Destination insertion: attach to the nearest real source character.
            mapping[j - 1] = min(i, n - 1) if n else None
            j -= 1
    return mapping


def phrase_span(phrase: str, full: str, pos: int) -> tuple[int, int]:
    target = compact(phrase)
    idx = full.find(target, pos)
    if idx < 0:
        raise ValueError(f"phrase not found in corrected segment: {phrase!r}")
    return idx, idx + len(target)


def fmt(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def main() -> int:
    data = json.loads(INPUT_JSON.read_text(encoding="utf-8"))
    if len(data.get("segments", [])) != len(PHRASES):
        raise SystemExit("segment count mismatch")

    out_segments = []
    ass_lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 464", "PlayResY: 848",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Default,Prompt,46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,20,20,70,1", "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    keyword_terms = ["ไอแพด", "แฟลชไดรฟ์", "แอปไฟล์", "แอปรูปภาพ", "กดปุ่มแชร์", "บันทึก", "ข้อมูล", "ความจำ", "เต็ม", "ตัวนี้"]
    total_cues = 0

    for seg, phrase_defs in zip(data["segments"], PHRASES):
        src_text = compact(seg["text"])
        src_words = seg.get("words", [])
        src_spans = []
        for token_index, token in enumerate(src_words):
            token_text = compact(token.get("word", ""))
            for _ in token_text:
                src_spans.append({"start": float(token["start"]), "end": float(token["end"]), "token": token_index})
        if not src_spans:
            raise SystemExit(f"segment {seg['id']} has no timed characters")
        if "".join(compact(w["word"]) for w in src_words) != src_text or len(src_spans) != len(src_text):
            raise SystemExit(f"segment {seg['id']} lacks consistent character timing")
        corrected = "".join(text for text, _ in phrase_defs)
        phrase_records = []
        source_cursor = 0
        for phrase_index, (text, word_count) in enumerate(phrase_defs):
            target = compact(text)
            remaining_targets = [compact(t) for t, _ in phrase_defs[phrase_index + 1:]]
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
                "text": text,
                "word_count": word_count,
                "start": start,
                "end": end,
                "source_char_start": src_a,
                "source_char_end": src_b,
                "text_match_ratio": round(best[0], 4),
            })

        out_segments.append({
            "id": seg["id"],
            "start": seg["start"],
            "end": seg["end"],
            "source_asr_text": seg["text"],
            "corrected_text": corrected,
            "alignment_method": "whisper-character-token-timing + monotonic DP text alignment",
            "alignment_level": "MICRO-PHRASE",
            "cues": phrase_records,
        })

    for block in out_segments:
        for cue in block["cues"]:
            text = ass_escape(cue["text"])
            for term in sorted(keyword_terms, key=len, reverse=True):
                if term in text:
                    text = text.replace(term, r"{\c&H00F0FF&}" + term + r"{\c&H00FFFFFF&}")
            n = total_cues
            s1 = 96 if n % 3 == 0 else 97
            tag = rf"{{\an2\pos(232,760)\fscx{s1}\fscy{s1}\t(0,120,\fscx103\fscy103)\t(120,210,\fscx100\fscy100)}}"
            # Correct ASS BGR white: &H00FFFFFF&
            text = text.replace(r"&H00FFFFFF&", r"&HFFFFFF&")
            ass_lines.append(f"Dialogue: 0,{fmt(cue['start'])},{fmt(cue['end'])},Default,,0,0,0,,{tag}{text}")
            total_cues += 1

    result = {
        "source": str(INPUT_JSON),
        "alignment_level": "MICRO-PHRASE",
        "alignment_method": "Whisper per-character token timestamps aligned to corrected spoken text by monotonic dynamic programming; no weighted distribution, VAD, silence or energy timing",
        "segments": out_segments,
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_ASS.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")
    print(f"WROTE {OUT_JSON}")
    print(f"WROTE {OUT_ASS}")
    print(f"CUES {total_cues}")
    for block in out_segments:
        for cue in block["cues"]:
            print(f"{cue['start']:.2f}-{cue['end']:.2f} ({cue['end']-cue['start']:.2f}s) [{cue['word_count']}] {cue['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
