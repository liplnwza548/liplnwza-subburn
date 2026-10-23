#!/usr/bin/env python3
"""Guarded Groq transcription: detect collapsed segments, re-transcribe, merge.

Proven case (client-batch2 C4, 2026-09-29): full-file Groq collapsed 11-26s
into ONE 20.4s segment with a garbage sentence. Re-transcribing just that
range returned 5 clean segments / 166 words, which merged back seamlessly.

Usage:
    python stt_guard.py <audio.wav> <out.json> [model] [language]

Rules:
- Detection is density-based, never text-based: a segment longer than
  SUSPECT_MIN_DUR whose word density falls below SUSPECT_MIN_DENSITY, or a
  long segment with zero words, is suspect — regardless of what it says.
- Each suspect range is re-transcribed ONCE (chunk cut with PAD padding).
  If the chunk is still suspect, the ORIGINAL audio is kept for that range
  and the range is reported — never an infinite retry loop, never silent.
- Merge replaces only segments/words overlapping the retried range;
  everything else keeps its original timestamps byte-for-byte.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SUSPECT_MIN_DUR = 6.0       # segments longer than this get density-checked
SUSPECT_MIN_DENSITY = 1.0   # words/sec below this => collapsed segment
EMPTY_MIN_DUR = 3.0         # long segment with zero words => missed speech
DESERT_MAX_GAP = 5.0        # longest word desert allowed inside one segment
PAD = 0.5                   # chunk padding (s) around a suspect range
MERGE_GAP = 1.0             # suspect ranges closer than this merge into one


def _words_in(words, start, end, tol=0.3):
    return [w for w in words
            if float(w.get("start", 0)) >= start - tol
            and float(w.get("end", 0)) <= end + tol]


def find_suspect_ranges(data):
    """Return [(start, end, reason)] for collapsed/missed-speech segments."""
    words = data.get("words") or []
    # Self-calibrating density: healthy Thai STT (char-fragments) packs
    # ~10+/s; a collapsed segment drips ~1-2/s. Compare each long segment
    # against the clip's own median so no absolute magic number decides.
    densities = []
    for s in data.get("segments") or []:
        dur = float(s.get("end", 0)) - float(s.get("start", 0))
        if dur > 2.0:
            densities.append(len(_words_in(words, float(s.get("start", 0)),
                                           float(s.get("end", 0)))) / dur)
    median = sorted(densities)[len(densities) // 2] if densities else 0.0
    ranges = []
    for s in data.get("segments") or []:
        start, end = float(s.get("start", 0)), float(s.get("end", 0))
        dur = end - start
        if dur <= 0:
            continue
        n = len(_words_in(words, start, end))
        density = n / dur
        if dur > SUSPECT_MIN_DUR and (density < SUSPECT_MIN_DENSITY
                                      or density < 0.25 * median):
            ranges.append((start, end,
                           f"collapsed: {dur:.1f}s with {n} words "
                           f"({density:.2f}/s vs clip median {median:.2f}/s)"))
        elif dur > EMPTY_MIN_DUR and n == 0:
            ranges.append((start, end, f"missed speech: {dur:.1f}s, 0 words"))
            continue
        # Word desert: fiction timestamps cram all words at the head,
        # leaving most of the segment empty (proven C4: ~18s desert).
        inside = sorted(_words_in(words, start, end),
                        key=lambda w: float(w.get("start", 0)))
        if len(inside) >= 2:
            gaps = [float(inside[i + 1].get("start", 0))
                    - float(inside[i].get("end", 0))
                    for i in range(len(inside) - 1)]
            peak = max(gaps)
            if peak > DESERT_MAX_GAP:
                ranges.append((start, end,
                               f"desert: {peak:.1f}s word gap in "
                               f"{dur:.1f}s segment"))
    # Merge ranges separated by less than MERGE_GAP.
    ranges.sort()
    merged = []
    for start, end, reason in ranges:
        if merged and start - merged[-1][1] < MERGE_GAP:
            prev = merged.pop()
            merged.append((prev[0], max(end, prev[1]),
                           prev[2] + " + " + reason))
        else:
            merged.append((start, end, reason))
    return merged


def merge_chunk(full, chunk, start, end, pad=PAD):
    """Replace full's segs/words in a suspect range with chunk's (offset).

    Chunk audio is cut starting at (start - pad), so chunk times shift by
    base = start - pad. Segments drop on strict overlap with the UNPADDED
    range (adjacent neighbors survive); words drop by midpoint inside the
    PADDED range (fiction words straddling the edge go). Small overlaps
    between kept and chunk material are trimmed downstream by
    trim_overlaps — same as ordinary STT jitter.
    """
    base = max(0.0, start - pad)
    lo, hi = base, end + pad
    kept_segs = [s for s in full.get("segments", [])
                 if not (float(s.get("start", 0)) < end
                         and float(s.get("end", 0)) > start)]
    kept_words = [w for w in full.get("words", [])
                  if (float(w.get("start", 0)) + float(w.get("end", 0))) / 2 < lo
                  or (float(w.get("start", 0)) + float(w.get("end", 0))) / 2 >= hi]
    new_segs, new_words = [], []
    for s in chunk.get("segments", []):
        s2 = dict(s)
        s2["start"] = round(float(s.get("start", 0)) + base, 2)
        s2["end"] = round(float(s.get("end", 0)) + base, 2)
        new_segs.append(s2)
    for w in chunk.get("words", []):
        w2 = dict(w)
        w2["start"] = round(float(w.get("start", 0)) + base, 2)
        w2["end"] = round(float(w.get("end", 0)) + base, 2)
        new_words.append(w2)
    full["segments"] = sorted(kept_segs + new_segs,
                              key=lambda s: float(s.get("start", 0)))
    full["words"] = sorted(kept_words + new_words,
                           key=lambda w: float(w.get("start", 0)))
    full["text"] = " ".join((s.get("text") or "").strip()
                            for s in full["segments"])
    return full


def cut_chunk(wav_path, start, end, out_path, pad=PAD):
    base = max(0.0, start - pad)
    dur = (end + pad) - base
    p = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{base:.2f}", "-t", f"{dur:.2f}", "-i", str(wav_path),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_path)],
        capture_output=True, text=True)
    if p.returncode != 0 or not out_path.is_file():
        raise RuntimeError(f"chunk cut failed [{base:.2f},{base + dur:.2f}]: "
                           f"{(p.stderr or '').strip()[-200:]}")


def transcribe_guarded(wav_path, out_path=None, model="whisper-large-v3-turbo",
                       language="th", transcriber=None):
    """Full transcribe with automatic retry of collapsed ranges.

    transcriber: callable(audio_path: Path) -> verbose_json dict (injectable
    for tests; defaults to live Groq transcribe_wav).
    Returns {"data": merged_dict, "repairs": [{"range", "reason", "status"}]}.
    """
    from transcribe_groq import transcribe_wav  # noqa: E402

    wav_path = Path(wav_path)
    if transcriber is None:
        transcriber = partial(transcribe_wav, model=model, language=language)
    data = transcriber(wav_path)
    repairs = []
    with tempfile.TemporaryDirectory(prefix="stt_guard_") as tmp:
        for start, end, reason in find_suspect_ranges(data):
            chunk_wav = Path(tmp) / f"chunk_{start:.0f}_{end:.0f}.wav"
            cut_chunk(wav_path, start, end, chunk_wav)
            chunk = transcriber(chunk_wav)
            still = [r for r in find_suspect_ranges(chunk)
                     if r[1] - r[0] > (end - start) * 0.5]
            if still:
                repairs.append({"range": [start, end], "reason": reason,
                                "status": "kept-original (chunk still bad)"})
                continue
            data = merge_chunk(data, chunk, start, end)
            repairs.append({"range": [start, end], "reason": reason,
                            "status": "retranscribed+merged"})
    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return {"data": data, "repairs": repairs}


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: stt_guard.py <audio.wav> <out.json> [model] [language]")
    wav_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    model = sys.argv[3] if len(sys.argv) > 3 else "whisper-large-v3-turbo"
    language = sys.argv[4] if len(sys.argv) > 4 else "th"
    if not wav_path.is_file():
        raise SystemExit(f"ERROR: audio not found: {wav_path}")
    result = transcribe_guarded(wav_path, out_path, model, language)
    data = result["data"]
    print(f"WROTE {out_path}")
    print(f"MODEL {model} LANG {language} "
          f"SEGMENTS {len(data.get('segments', []))} "
          f"WORDS {len(data.get('words', []))}")
    for r in result["repairs"]:
        print(f"REPAIR {r['range'][0]:.2f}-{r['range'][1]:.2f}: "
              f"{r['reason']} => {r['status']}")
    if not result["repairs"]:
        print("REPAIR none (no suspect segments)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
