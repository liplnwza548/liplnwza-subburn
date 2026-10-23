#!/usr/bin/env python3
"""Transcribe audio with Groq Whisper requesting WORD timestamps.

Usage:
    python transcribe_groq.py <audio.wav> <out.json>
    [model] [language]   (defaults: whisper-large-v3-turbo, th)

Gate (per renderer V5 rules): the response MUST contain word-level
timestamps. If Groq returns segments only (no words[]), exit non-zero
with WORD_TIMESTAMPS_MISSING instead of letting a later step invent
cue timing by proportional distribution.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from keys import load_groq_key  # noqa: E402


def transcribe_wav(audio_path, model="whisper-large-v3-turbo",
                   language="th", api_key=None):
    """Transcribe one wav file -> Groq verbose_json dict (words + segments).

    Extracted from main() so stt_guard can reuse it per chunk.
    Raises SystemExit WORD_TIMESTAMPS_MISSING when Groq returns no words.
    """
    from groq import Groq

    if api_key is None:
        api_key = load_groq_key()
    client = Groq(api_key=api_key)
    with open(audio_path, "rb") as fh:
        resp = client.audio.transcriptions.create(
            file=fh,
            model=model,
            language=language,
            temperature=0.0,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )

    # groq-sdk returns a pydantic model; normalize to plain dict.
    if hasattr(resp, "model_dump"):
        data = resp.model_dump()
    elif hasattr(resp, "to_dict"):
        data = resp.to_dict()
    else:
        data = json.loads(resp.model_dump_json()) if hasattr(resp, "model_dump_json") else dict(resp)

    if not (data.get("words") or []):
        segments = data.get("segments") or []
        raise SystemExit(
            "ERROR: WORD_TIMESTAMPS_MISSING — Groq returned "
            f"{len(segments)} segment(s) and zero word timestamps. "
            "Refusing to continue: cue timing must come from real word/token "
            "timestamps (renderer V5 rule), never estimated within segments."
        )
    return data


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: transcribe_groq.py <audio.wav> <out.json> [model] [language]")
    audio_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    model = sys.argv[3] if len(sys.argv) > 3 else "whisper-large-v3-turbo"
    language = sys.argv[4] if len(sys.argv) > 4 else "th"
    if not audio_path.is_file():
        raise SystemExit(f"ERROR: audio not found: {audio_path}")

    api_key = load_groq_key()
    data = transcribe_wav(audio_path, model=model, language=language,
                          api_key=api_key)

    words = data.get("words") or []
    segments = data.get("segments") or []
    if not words:
        raise SystemExit(
            "ERROR: WORD_TIMESTAMPS_MISSING — Groq returned "
            f"{len(segments)} segment(s) and zero word timestamps. "
            "Refusing to continue: cue timing must come from real word/token "
            "timestamps (renderer V5 rule), never estimated within segments."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE {out_path}")
    print(f"MODEL {model} LANG {language} SEGMENTS {len(segments)} WORDS {len(words)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
