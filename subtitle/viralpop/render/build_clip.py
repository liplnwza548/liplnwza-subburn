#!/usr/bin/env python3
"""Generalized clip builder (V6 job runner).

Same audio-grounded timing as V4/V5 (see render/align.py — no new timing
math, never weighted/proportional), but driven by a per-clip job config
instead of tes2 hardcodes:

- transcript segments + phrases come from the job config (any clip);
- cue grouping targets 3-5 spoken syllables (see thai_line_split);
- loanword whitelist + ASR normalization (see loanwords);
- width budget, fontsize, PlayRes and position derive from the real video
  (or explicit config values);

Usage:
    python build_clip.py --config job.json --transcript tr.json \\
        --ass out.ass --json out.json [--video clip.mp4]

"auto" fontsize/playres/margins/pos require --video (probed with ffprobe).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import align  # noqa: E402
import build_v4 as v4  # noqa: E402  (reuse compact/fmt/ass_escape, unmodified)
import loanwords as loanwords  # noqa: E402
import thai_line_split as tls  # noqa: E402
from thai_mark_fix import build_fixed_cue  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parents[1]

# Reference geometry the V5 golden output was authored at.
REF_W, REF_H = 464, 848
REF_FONTSIZE = 46
REF_MARGIN_LR = 20
REF_MARGIN_V = 70
REF_POS = (232, 760)


def probe_size(video: Path) -> tuple[int, int]:
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        text=True,
    )
    s = json.loads(raw)["streams"][0]
    return int(s["width"]), int(s["height"])


def _join_words(tokens: list[str]) -> str:
    """Thai joins without spaces; keep a space around digit runs
    ("6"+"ช่อง" -> "6 ช่อง") for readability."""
    out = ""
    for tok in tokens:
        if out and out[-1] != " " and tok[:1] != " ":
            if tok[:1].isdigit() != out[-1].isdigit():
                out += " "
        out += tok
    return out


def pack_syllables(phrases: list[str], lo: int, hi: int) -> list[str]:
    """Greedily pack word tokens into cues of lo-hi syllables.

    Loanwords are atomic (never split here). A single over-hi word is
    emitted alone and left for the width splitter (flagged if forced).
    Only regroups text — timing still comes from align.py afterwards."""
    words: list[str] = []
    for ph in phrases:
        words.extend(tls.tokenize_words(ph))
    # Greedy pack with per-word syllable counts (loanwords atomic).
    groups: list[list[str]] = []
    acc: list[str] = []
    acc_syl = 0
    for w in words:
        c = tls.count_syllables(w)
        if acc and acc_syl + c > hi:
            groups.append(acc)
            acc, acc_syl = [], 0
        acc.append(w)
        acc_syl += c
    if acc:
        groups.append(acc)
    # Rebalance a short tail: steal whole words from the previous cue while
    # the tail is below target and the previous cue stays above target;
    # otherwise merge the tail up. A single over-hi word is still emitted
    # alone and left for the width splitter (flagged if forced).
    counts = [sum(tls.count_syllables(w) for w in g) for g in groups]
    while len(groups) > 1 and counts[-1] < lo:
        if counts[-2] > lo:
            w = groups[-2].pop()
            counts[-2] -= tls.count_syllables(w)
            groups[-1].insert(0, w)
            counts[-1] += tls.count_syllables(w)
        else:
            groups[-2].extend(groups[-1])
            counts[-2] += counts[-1]
            groups.pop()
            counts.pop()
    # Don't strand clitics that cannot end a prosodic unit (negation,
    # modal, coordinators): move a trailing one to the next cue when it
    # still fits. ที่/ว่า/ใน/ของ are deliberately excluded — they close a
    # phrase naturally ("ลองดูที่" is fine) and usually open the next one.
    STRANDED = {"ไม่", "จะ", "ก็", "และ", "กับ", "หรือ", "แต่"}
    for i in range(len(groups) - 1):
        while groups[i] and groups[i][-1] in STRANDED:
            w = groups[i][-1]
            c = tls.count_syllables(w)
            if counts[i + 1] + c > hi or counts[i] - c < 2:
                break
            groups[i].pop()
            counts[i] -= c
            groups[i + 1].insert(0, w)
            counts[i + 1] += c
        if not groups[i]:
            break
    groups = [g for g in groups if g]
    return [_join_words(g) for g in groups]


def keyword_spans(text: str, keywords: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in sorted(keywords, key=len, reverse=True):
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(term)))
            start = idx + len(term)
    return spans


def colorize(text: str, keywords: list[str]) -> str:
    for term in sorted(keywords, key=len, reverse=True):
        if term in text:
            text = text.replace(term, r"{\c&H00F0FF&}" + term + r"{\c&HFFFFFF&}")
    return text


# Post-process thresholds (documented, perception-grounded):
SEG_SNAP_THRESHOLD = 0.30   # first-word earlier than seg.start by more than
# this (normal band is -0.10..+0.24s; observed back-dates are +0.50s) means
# Groq back-dated the segment's first word: rigid-shift the cue onto the
# segment's own start (still an ASR value, never invented).
GAP_WITNESS_S = 0.25        # silence before onset >= this anchors the onset:
# a segmenter can place a boundary late, but sound starts when it starts.
# gap_before >= GAP_WITNESS_S skips the snap (cut2 seg4: 0.52s).
OVERLAP_EPS = 0.01          # 1cs = ASS timestamp resolution (fmt rounds .2f)
OVERLAP_REVIEW_MS = 200.0   # resolved overlaps above this need human review
CASCADE_MAX_PUSH_S = 0.10   # pushing a neighbour further than this means the
# neighbourhood is pathological: fail loud, never silently wreck a healthy cue.
RATE_GUARD_SYL_PER_SEC = 8.0  # fastest plausible Thai speech is ~6-7 syl/s


def post_process(out_segments: list[dict]) -> dict:
    """Repair cross-cue timing AFTER align, BEFORE writing ASS.

    1. SEG_SNAP: a segment-first cue starting >SEG_SNAP_THRESHOLD before the
       segment's own start gets a rigid shift (start AND end) onto
       seg.start. Both values stay ASR-derived; durations are preserved.
    2. OVERLAP: adjacent cues with prev.end > next.start get prev.end
       trimmed to next.start - OVERLAP_EPS (onsets untouched). If that
       would invert prev, next.start is pushed to prev.end instead
       (forward cascade, recorded). Either way the result is monotonic.
    3. RATE_GUARD: cues faster than RATE_GUARD_SYL_PER_SEC are flagged only.

    Every cue gets metadata (defaults False/0/[]): timing_source,
    snap_delta_ms, overlap_resolved_ms, cascaded, speech_rate_syl_per_sec,
    needs_review, review_reason. Returns a summary dict (also printed).
    """
    flat: list[dict] = [c for b in out_segments for c in b["cues"]]
    for b in out_segments:
        for c in b["cues"]:
            c.setdefault("timing_source", "asr_word")
            c.setdefault("snap_delta_ms", 0.0)
            c.setdefault("overlap_resolved_ms", 0.0)
            c.setdefault("cascaded", False)
            c.setdefault("needs_review", False)
            c.setdefault("review_reason", [])
    summary = {"snapped": 0, "overlaps": 0, "cascaded": 0, "needs_review": []}

    for b in out_segments:
        if not b["cues"]:
            continue
        first = b["cues"][0]
        delta = float(b["start"]) - first["start"]
        gap = b.get("gap_before_s")
        if delta > SEG_SNAP_THRESHOLD and (gap is None or gap < GAP_WITNESS_S):
            first["start"] += delta
            first["end"] += delta
            first["timing_source"] = "asr_segment_snap"
            first["snap_delta_ms"] = round(delta * 1000, 1)
            first["needs_review"] = True
            first["review_reason"].append(
                "seg_snap+%.0fms" % (delta * 1000))
            summary["snapped"] += 1
            summary["needs_review"].append(first["text"])
        elif delta > SEG_SNAP_THRESHOLD:
            # Onset witnessed by preceding silence: keep word times, flag the
            # suspect interior instead of destroying a good onset.
            first["timing_source"] = "asr_word_anchored"
            first["needs_review"] = True
            first["review_reason"].append("onset_anchored_post_silence")
            summary["needs_review"].append(first["text"])

    for i in range(1, len(flat)):
        prev, cur = flat[i - 1], flat[i]
        ov = prev["end"] - cur["start"]
        if ov > 0:
            summary["overlaps"] += 1
            if cur["start"] - OVERLAP_EPS <= prev["start"]:
                # Trimming would invert prev: push cur.start to prev.end,
                # unless the push is pathological (fail loud instead of
                # silently wrecking a healthy neighbour).
                pushed = prev["end"] - cur["start"]
                if pushed > CASCADE_MAX_PUSH_S:
                    raise SystemExit(
                        "CASCADE-REFUSED: pushing %r (%.2f) by %.0fms to fix "
                        "overlap with %r (%.2f-%.2f); neighbourhood needs "
                        "human review" % (
                            cur["text"], cur["start"], pushed * 1000,
                            prev["text"], prev["start"], prev["end"]))
                cur["start"] = prev["end"]
                if cur["end"] < cur["start"]:
                    # Pushed past its own end: collapse (renders nothing)
                    # and let the rate guard flag it for human review.
                    cur["end"] = cur["start"]
                cur["overlap_resolved_ms"] = round(pushed * 1000, 1)
                cur["cascaded"] = True
                cur["needs_review"] = True
                cur["review_reason"].append(
                    "overlap_push+%.0fms" % (pushed * 1000))
                summary["cascaded"] += 1
            else:
                prev["end"] = cur["start"] - OVERLAP_EPS
                prev["overlap_resolved_ms"] = round(ov * 1000, 1)
                prev["review_reason"].append(
                    "overlap_trim+%.0fms" % (ov * 1000))
            if ov * 1000 > OVERLAP_REVIEW_MS:
                prev["needs_review"] = True
                cur["needs_review"] = True
                prev["review_reason"].append("big_overlap")
                cur["review_reason"].append("big_overlap")
                summary["needs_review"].append(prev["text"] + " | " + cur["text"])

    for c in flat:
        dur = c["end"] - c["start"]
        c["speech_rate_syl_per_sec"] = round(c["syllables"] / dur, 1) if dur > 0 else 0.0
        if dur <= 0:
            c["needs_review"] = True
            c["review_reason"].append("zero_duration")
            summary["needs_review"].append(c["text"])
        elif c["speech_rate_syl_per_sec"] > RATE_GUARD_SYL_PER_SEC:
            c["needs_review"] = True
            c["review_reason"].append(
                "fast_speech_%s/s" % c["speech_rate_syl_per_sec"])
            summary["needs_review"].append(c["text"])
    return summary


def apply_overrides(out_segments: list[dict], overrides: list[dict]) -> int:
    """Human- or witness-approved timing corrections (job config
    "timing_overrides": [{"seg": id, "cue": index, "start": s, "end": e,
    "source": "...", "why": "..."}]).

    Applied AFTER post_process, BEFORE ASS writing. Each override sets the
    cue span verbatim and is logged (timing_source="override:<source>",
    needs_review=True) — never silent. Afterwards monotonicity is
    re-asserted over the whole timeline: any introduced overlap fails the
    build loud instead of shipping garble.
    """
    if not overrides:
        return 0
    by_seg = {b["id"]: b for b in out_segments}
    for ov in overrides:
        block = by_seg.get(ov["seg"])
        if block is None:
            raise SystemExit(f"timing_override: unknown seg {ov['seg']!r}")
        cues = block["cues"]
        if not (0 <= ov["cue"] < len(cues)):
            raise SystemExit(
                f"timing_override: seg {ov['seg']} has {len(cues)} cues, "
                f"no index {ov['cue']}")
        if not ov["start"] < ov["end"]:
            raise SystemExit(f"timing_override: inverted span {ov}")
        c = cues[ov["cue"]]
        c["start"] = float(ov["start"])
        c["end"] = float(ov["end"])
        c["timing_source"] = "override:" + str(ov.get("source", "manual"))
        c["needs_review"] = True
        c["review_reason"].append("override:" + str(ov.get("why", ov.get("source", ""))))
        dur = c["end"] - c["start"]
        c["speech_rate_syl_per_sec"] = round(c["syllables"] / dur, 1)
    flat = [c for b in out_segments for c in b["cues"]]
    for i in range(1, len(flat)):
        if flat[i]["start"] < flat[i - 1]["end"]:
            raise SystemExit(
                "timing_override introduced overlap: %r (%.2f-%.2f) vs %r "
                "(%.2f-%.2f); fix the override, not the validator" % (
                    flat[i - 1]["text"], flat[i - 1]["start"], flat[i - 1]["end"],
                    flat[i]["text"], flat[i]["start"], flat[i]["end"]))
    return len(overrides)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--transcript", type=Path, required=True)
    ap.add_argument("--ass", type=Path, required=True)
    ap.add_argument("--json", type=Path, required=True)
    ap.add_argument("--video", type=Path, default=None)
    ap.add_argument("--strict-overlap", action="store_true",
                    help="fail the build if any cue needs timing review")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    data = json.loads(args.transcript.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    if data.get("words") and not any("words" in s for s in segments):
        align.attach_words(segments, data["words"])
    cfg_segments = cfg.get("segments", [])
    if len(segments) != len(cfg_segments):
        raise SystemExit(
            f"segment count mismatch: transcript={len(segments)} config={len(cfg_segments)}"
        )

    if cfg.get("playres") == "auto" or cfg.get("fontsize") == "auto" \
            or cfg.get("margins") == "auto" or cfg.get("pos") == "auto":
        if args.video is None:
            raise SystemExit("--video is required when geometry is 'auto'")
        play_w, play_h = probe_size(args.video)
    else:
        play_w, play_h = cfg["playres"]

    fontsize = cfg.get("fontsize")
    if fontsize == "auto":
        fontsize = round(REF_FONTSIZE * play_h / REF_H)
    margins = cfg.get("margins")
    if margins == "auto":
        m = round(REF_MARGIN_LR * play_w / REF_W)
        margins = [m, m]
    margin_v = cfg.get("margin_v", "auto")
    if margin_v == "auto":
        margin_v = round(REF_MARGIN_V * play_h / REF_H)
    pos = cfg.get("pos")
    if pos == "auto":
        pos = [play_w // 2, round(REF_POS[1] * play_h / REF_H)]
    safety = float(cfg.get("safety_factor", 0.94))
    max_width = (play_w - margins[0] - margins[1]) * safety

    font_file = Path(cfg.get("font_file", "fonts/Prompt-Bold.ttf"))
    if not font_file.is_absolute():
        font_file = SKILL_ROOT / font_file
    keywords: list[str] = cfg.get("keywords", [])
    lo, hi = cfg.get("syllable_target", [3, 5])

    tls.init(str(font_file), fontsize)

    out_segments = []
    ass_lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {play_w}", f"PlayResY: {play_h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,Prompt,{fontsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,{margins[0]},{margins[1]},{margin_v},1", "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]



    total_cues = 0
    forced_total = 0
    width_worst = 0.0

    for seg_i, (seg, seg_cfg) in enumerate(zip(segments, cfg_segments)):
        if "id" in seg_cfg and seg_cfg["id"] != seg.get("id"):
            raise SystemExit(f"segment id mismatch: config={seg_cfg['id']} transcript={seg.get('id')}")
        phrases = [loanwords.normalize(p) for p in seg_cfg["phrases"]]
        packed = pack_syllables(phrases, lo, hi)
        final_cues: list[str] = []
        forced_idx: set[int] = set()
        for cue_text in packed:
            pieces, forced = tls.split_phrase_tracked(
                cue_text, max_width, keyword_spans(cue_text, keywords)
            )
            if forced:
                forced_idx.update(range(len(final_cues), len(final_cues) + len(pieces)))
            final_cues.extend(pieces)

        src_text, src_spans = align.build_src_spans(seg)
        records = align.align_cues(seg.get("id"), src_text, src_spans, final_cues)
        for i, r in enumerate(records):
            r["syllables"] = tls.count_syllables(r["text"])
            r["forced_split"] = i in forced_idx
        seg_forced = len(forced_idx)
        # Silence gap before this segment's first routed word (witness for
        # the onset: a segmenter can place a boundary late, but sound starts
        # when it starts). First segment has no predecessor -> None.
        seg_words = seg.get("words", [])
        prev_words = segments[seg_i - 1].get("words", []) if seg_i > 0 else []
        if seg_words and prev_words:
            gap_before = float(seg_words[0]["start"]) - float(prev_words[-1]["end"])
        else:
            gap_before = None
        # forced flag belongs to the width split; attribute per cue text.
        forced_texts = seg_forced  # count only; per-cue attribution below
        _ = forced_texts
        out_segments.append({
            "id": seg.get("id"),
            "start": seg.get("start"),
            "end": seg.get("end"),
            "gap_before_s": gap_before,
            "source_asr_text": seg.get("text"),
            "corrected_text": "".join(final_cues),
            "alignment_method": "whisper-character-token-timing + monotonic DP text alignment (syllable packing + width-aware split)",
            "alignment_level": "MICRO-PHRASE",
            "width_forced_splits": seg_forced,
            "cues": records,
        })

    # Post-process AFTER align (cross-cue repairs only), BEFORE ASS writing
    # so Layer-1 overlay events automatically share the repaired times.
    pp = post_process(out_segments)
    n_overridden = apply_overrides(out_segments, cfg.get("timing_overrides", []))

    # Render ASS.
    for block in out_segments:
        for cue in block["cues"]:
            w = tls.width_px(cue["text"])
            width_worst = max(width_worst, w)
            if w > max_width:
                raise SystemExit(
                    f"cue exceeds width budget {w:.1f}>{max_width:.1f}: {cue['text']!r}"
                )
            plain = v4.ass_escape(cue["text"])
            colored = colorize(plain, keywords).replace(r"&H00FFFFFF&", r"&HFFFFFF&")
            n = total_cues
            s1 = 96 if n % 3 == 0 else 97
            pos_x, pos_y = pos
            an = r"\an2"
            scale_tags = rf"\fscx{s1}\fscy{s1}\t(0,120,\fscx103\fscy103)\t(120,210,\fscx100\fscy100)"
            tag = f"{{{an}\\pos({pos_x},{pos_y}){scale_tags}}}"
            main_text, overlays = build_fixed_cue(plain, colored, pos_x, pos_y, fontsize, an, scale_tags)
            start, end = v4.fmt(cue["start"]), v4.fmt(cue["end"])
            ass_lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{tag}{main_text}")
            for overlay_text in overlays:
                ass_lines.append(f"Dialogue: 1,{start},{end},Default,,0,0,0,,{overlay_text}")
            total_cues += 1

    forced_total = sum(b["width_forced_splits"] for b in out_segments)
    needs_total = sum(1 for b in out_segments for c in b["cues"] if c["needs_review"])
    result = {
        "source": str(args.transcript),
        "config": str(args.config),
        "alignment_level": "MICRO-PHRASE",
        "alignment_method": "Whisper per-character token timestamps aligned to corrected spoken text by monotonic dynamic programming (render/align.py, same math as build_v4); syllable packing + width-aware split only regroup text, never timing",
        "post_process": {
            "snapped_cues": pp["snapped"],
            "overlaps_resolved": pp["overlaps"],
            "cascaded": pp["cascaded"],
            "needs_review_cues": needs_total,
        },
        "geometry": {"playres": [play_w, play_h], "fontsize": fontsize,
                     "margins": margins, "margin_v": margin_v, "pos": pos,
                     "max_width_px": round(max_width, 1)},
        "segments": out_segments,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.ass.parent.mkdir(parents=True, exist_ok=True)
    args.ass.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")
    print(f"WROTE {args.json}")
    print(f"WROTE {args.ass}")
    print(f"CUES {total_cues} FORCED_WIDTH_SPLITS {forced_total} "
          f"WORST_WIDTH {width_worst:.1f}/{max_width:.1f}")
    print(f"POST-PROCESS snapped={pp['snapped']} overlaps={pp['overlaps']} "
          f"cascaded={pp['cascaded']} overridden={n_overridden} "
          f"needs_review={needs_total}")
    for block in out_segments:
        flag = " [FORCED-SPLIT]" if block["width_forced_splits"] else ""
        print(f"-- seg {block['id']}{flag}")
        for cue in block["cues"]:
            forced_tag = " FORCED" if cue["forced_split"] else ""
            review_tags = []
            if cue["timing_source"] == "asr_segment_snap":
                review_tags.append("snap+%sms" % cue["snap_delta_ms"])
            elif cue["timing_source"] != "asr_word":
                review_tags.append(cue["timing_source"])
            if cue["overlap_resolved_ms"]:
                review_tags.append("ovfix" + ("+cascade" if cue["cascaded"] else "") + str(cue["overlap_resolved_ms"]) + "ms")
            if cue["needs_review"]:
                review_tags.append("REVIEW")
            print(f"{cue['start']:.2f}-{cue['end']:.2f} ({cue['end']-cue['start']:.2f}s) "
                  f"[{cue['syllables']}syl r={cue['text_match_ratio']}{forced_tag}] "
                  f"{cue['text']} {' '.join(review_tags)}".rstrip())
    if args.strict_overlap and needs_total:
        raise SystemExit(f"STRICT-OVERLAP: {needs_total} cue(s) need review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
