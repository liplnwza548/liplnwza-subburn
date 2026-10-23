#!/usr/bin/env python3
"""Ticket 05: single agent-facing entrypoint for script-aligned burn (v1).

Single entrypoint::

    run_script_pipeline(video, script_lines, fonts_dir, out_mp4, ...) -> dict

Stages wired (tickets 01 -> 04, same seam):

1. ``extract_word_timeline(video)`` — STT word timestamps ONLY via the
   existing Groq helper path (``render/transcribe_groq.py`` semantics:
   whisper-large-v3-turbo, verbose_json, word+segment granularities).
   STT strings are discarded for display; only ``start``/``end`` feed
   alignment. Refuses with ``SttError`` (WORD_TIMESTAMPS_MISSING /
   GROQ_API_KEY_MISSING) instead of inventing timing.
2. ``align_script_to_timeline(script_lines, words)`` (ticket 01).
3. ``gate_before_burn(alignment)`` (ticket 03) — mismatches non-empty
   -> STOP, return the all-mismatches report; no ASS written, ffmpeg
   never runs, Lip reviews once.
4. ``build_ass`` (ticket 02) + ``burn_script_aligned`` (ticket 04:
   ``ass=`` + ``fontsdir`` + ``-c:a copy`` + ffprobe/QA frames).

Script-aligned mode is the DEFAULT v1 happy path. The old ASR-as-text
path (STT transcript as cue text) is DEPRECATED for COS/Muse — see
SKILL.md / QUICKSTART.md.

CLI::

    python render/script_pipeline.py --video <clip>.mp4 \\
        --script <script>.txt --fonts <fontsdir> --out <out>.mp4 \\
        [--frames-dir <qa/>] [--words-json <asr>.json]

``--words-json`` skips STT (offline/tests): a Groq verbose_json file or
``{"words": [...]}`` whose timestamps are reused for alignment.
Otherwise the pipeline extracts audio and calls Groq (needs
``GROQ_API_KEY`` in env or ``.env`` — never logged).

Exit codes: 0 = burned + QA passed; 2 = mismatch gate blocked (report
printed, nothing written); 1 = STT/burn/QA error.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from script_align import align_script_to_timeline  # noqa: E402
from script_burn import (  # noqa: E402
    AudioReencodeRequiredError,
    BurnError,
    QaError,
    burn_script_aligned,
)
from script_gate import MismatchGateError, gate_before_burn  # noqa: E402
from script_keywords import load_keywords_job, normalize_keywords  # noqa: E402 v1.1 T3
from script_split import split_alignment  # noqa: E402 v1.1 T2
from script_timing import trim_overlaps  # noqa: E402 v1.1 T5-fix
from script_width import assert_fit  # noqa: E402 v1.1 T1

#: STT defaults — identical to render/transcribe_groq.py.
DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_LANGUAGE = "th"


class SttError(Exception):
    """Word-timeline extraction failed (no key, no words, no ffmpeg)."""


def load_script_lines(script_txt: Path) -> list[str]:
    """Read Lip's script: one cue per line, verbatim, blanks dropped."""
    path = Path(script_txt)
    if not path.is_file():
        raise SttError(f"script file not found: {path}")
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if not lines:
        raise SttError(f"script file has no usable lines: {path}")
    return lines


def extract_word_timeline(
    video: Path,
    *,
    model: str = DEFAULT_MODEL,
    language: str = DEFAULT_LANGUAGE,
) -> dict:
    """Video -> Groq verbose_json dict (word timestamps for alignment).

    Mirrors ``render/transcribe_groq.py``: 16 kHz mono WAV via ffmpeg,
    then ``client.audio.transcriptions.create`` with
    ``timestamp_granularities=["word", "segment"]``. Raises ``SttError``
    with WORD_TIMESTAMPS_MISSING when Groq returns segments only —
    the pipeline must never estimate timing within segments.
    """
    from keys import load_groq_key  # lazy: only needed for live STT

    video = Path(video)
    if not video.is_file():
        raise SttError(f"input video not found: {video}")
    api_key = load_groq_key()  # SystemExit GROQ_API_KEY_MISSING if absent
    try:
        from groq import Groq
    except ImportError as exc:
        raise SttError(
            "groq SDK not installed (pip install -r requirements.txt)"
        ) from exc

    with tempfile.TemporaryDirectory(prefix="script_pipeline_wav_") as tmp:
        wav = Path(tmp) / "audio_16k.wav"
        conv = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(video.resolve()),
             "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(wav)],
            capture_output=True, text=True)
        if conv.returncode != 0 or not wav.is_file():
            raise SttError(
                f"audio extraction failed for {video}: "
                f"{(conv.stderr or '').strip()}")
        client = Groq(api_key=api_key)
        with open(wav, "rb") as fh:
            resp = client.audio.transcriptions.create(
                file=fh,
                model=model,
                language=language,
                temperature=0.0,
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )
        if hasattr(resp, "model_dump"):
            data = resp.model_dump()
        elif hasattr(resp, "to_dict"):
            data = resp.to_dict()
        else:
            data = json.loads(resp.model_dump_json())
    words = data.get("words") or []
    segments = data.get("segments") or []
    if not words:
        raise SttError(
            "WORD_TIMESTAMPS_MISSING — Groq returned "
            f"{len(segments)} segment(s) and zero word timestamps. "
            "Refusing to continue: cue timing must come from real "
            "word/token timestamps, never estimated within segments."
        )
    return data


def load_words_json(words_json: Path) -> dict:
    """Load a cached Groq verbose_json (or {"words": [...]}) for alignment."""
    path = Path(words_json)
    if not path.is_file():
        raise SttError(f"words JSON not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SttError(f"words JSON unreadable: {path}: {exc}") from exc
    words = data.get("words") if isinstance(data, dict) else None
    if not words:
        raise SttError(
            f"WORD_TIMESTAMPS_MISSING — {path} has no words[]; "
            "re-run transcription before alignment.")
    return data


def resolve_keywords(keywords=None, job_json=None) -> list[str]:
    """Resolve T3 keywords: explicit list wins; else load from a per-clip
    job file (jobs/clipNN.json ``keywords``); else [] (no yellow)."""
    if keywords:
        if isinstance(keywords, str):
            raise ValueError(
                "keywords must be a LIST of phrases, not a comma-string: "
                "list(keywords) char-splits (client-batch1 2026-09-25: "
                "single-char terms killed all yellow + false R4 refusals). "
                "CLI callers: split on comma first (see _cli).")
        return normalize_keywords(list(keywords))
    if job_json is not None:
        return load_keywords_job(job_json)
    return []


def run_script_pipeline(
    video,
    script_lines: list[str] | Path | str,
    fonts_dir,
    out_mp4,
    *,
    frames_dir=None,
    words=None,
    words_json=None,
    model: str = DEFAULT_MODEL,
    language: str = DEFAULT_LANGUAGE,
    keep_temp: bool = False,
    keywords=None,
    job_json=None,
    profile: str = "general",
) -> dict:
    """Run the full script-aligned pipeline (tickets 01 -> 04 + v1.1 T2/T3).

    Returns either::

        {"ok": True, ...burn report..., "alignment": alignment}
        {"ok": False, "report": <all-mismatches block>,
         "mismatch_count": N, "alignment": alignment}

    ``ok False`` writes NOTHING (no ASS, no MP4) — Lip fixes the script
    lines (or audio), then re-runs. Raises ``SttError`` / ``BurnError`` /
    ``QaError`` on STT/burn/QA failure.
    """
    video = Path(video)
    fonts_dir = Path(fonts_dir)
    out_mp4 = Path(out_mp4)
    if isinstance(script_lines, (str, Path)) and Path(script_lines).is_file():
        lines = load_script_lines(Path(script_lines))
    else:
        lines = [ln for ln in (script_lines or []) if str(ln).strip()]
        if not lines:
            raise SttError("no script lines: pass script_lines or a script .txt")
    if words is None and words_json is not None:
        words = load_words_json(words_json)
    if words is None:
        words = extract_word_timeline(video, model=model, language=language)

    alignment = align_script_to_timeline(lines, words)
    gate = gate_before_burn(alignment)
    if not gate["ok"]:
        return {
            "ok": False,
            "report": gate["report"],
            "mismatch_count": gate["mismatch_count"],
            "alignment": alignment,
        }
    # v1.1 T3: keywords resolved BEFORE the split (not only for yellow):
    # every occurrence becomes a protected span the cutter may not sever,
    # so Lip never hand-approves a split keyword. Colorize still applies
    # inside build_ass per sub-cue piece.
    keywords = resolve_keywords(keywords, job_json)
    # KEYWORD-COVERAGE GATE (2026-09-29): a keyword with zero verbatim hits
    # burns zero yellow with no error — refuse here, before ASS/burn, with
    # the per-keyword counts attached. Runs only when keywords are passed.
    if keywords:
        from script_keywords import check_keyword_hits as _hits
        hits = _hits(lines, keywords)
        missing = [k for k, c in hits.items() if c == 0]
        if missing:
            return {
                "ok": False,
                "report": ("KEYWORD-COVERAGE GATE: no burn — these keywords "
                           f"occur 0x in the script: {missing}. "
                           f"hits={hits}. Fix the spelling or drop the term."),
                "mismatch_count": 0,
                "keyword_misses": missing,
                "alignment": alignment,
            }
    # v1.1 T2: split over-budget script lines into width-safe sub-cues
    # (within-line only; concat == script line exactly; timings subdivided
    # from each line's own STT span). v1.1 T1: FAIL-LOUD when anything
    # still exceeds the measured budget — before ASS/burn.
    # Word-snap 2026-09-24: pass the STT word timeline so internal
    # sub-cue boundaries snap to real word edges (fixes ~0.1-0.2s
    # early-pop/late-linger on split cues).
    alignment = split_alignment(alignment, words=words, keywords=keywords)
    # v1.1 T5-fix: trim STT-jitter overlaps after T2 split, before
    # colorize/overlays/write (split_alignment already trims; this is the
    # explicit pipeline-stage guarantee — idempotent). Unresolvable pairs
    # raise here, before ASS/burn.
    alignment["matched"] = trim_overlaps(alignment["matched"])
    width_report = assert_fit(alignment["matched"])
    # (keywords already resolved above; colorize applies inside build_ass
    # per sub-cue piece.)
    try:
        result = burn_script_aligned(
            video, alignment, fonts_dir, out_mp4,
            frames_dir=frames_dir, keep_temp=keep_temp,
            keywords=keywords, profile=profile)
    except MismatchGateError:  # cannot happen post-gate; keep the refusal loud
        raise
    result["alignment"] = alignment
    result["width_report"] = width_report
    result["keywords"] = keywords
    return result


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Script-aligned burn (v1 default): script=text, "
                    "STT=timing, gate before burn.")
    ap.add_argument("--video", required=True, type=Path,
                    help="finished clip (audio untouched: -c:a copy)")
    ap.add_argument("--script", required=True, type=Path,
                    help="Lip's script .txt, one cue per line, verbatim")
    ap.add_argument("--fonts", "--fonts-dir", dest="fonts_dir",
                    required=True, type=Path,
                    help="fonts dir holding Prompt-Bold (copied to temp fdir/)")
    ap.add_argument("--out", "--output", dest="out", required=True,
                    type=Path, help="burned output MP4")
    ap.add_argument("--frames-dir", type=Path, default=None,
                    help="QA stills dir (default: <out_stem>_qa next to --out)")
    ap.add_argument("--words-json", type=Path, default=None,
                    help="cached Groq verbose_json: skip live STT")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--language", default=DEFAULT_LANGUAGE)
    ap.add_argument("--keep-temp", action="store_true",
                    help="keep temp ASS/fdir for debugging")
    ap.add_argument("--keywords", default=None,
                    help="comma-separated T3 keywords (SCRIPT substrings "
                    "render yellow; overrides --job-json)")
    ap.add_argument("--job-json", type=Path, default=None,
                    help="per-clip job file (jobs/clipNN.json): loads its "
                    "\"keywords\" array when --keywords is absent")
    ap.add_argument("--profile", default="general",
                    choices=["general", "vo"],
                    help="readability profile: general (strict, default) or "
                    "vo (voiceover full-auto: phrase-complete cues up to "
                    "8 syllables pass without per-cue Lip sign-off)")
    args = ap.parse_args()

    try:
        lines = load_script_lines(args.script)
    except SttError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        words = (load_words_json(args.words_json)
                 if args.words_json is not None else None)
    except SttError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if words is None:
        try:
            words = extract_word_timeline(
                args.video, model=args.model, language=args.language)
        except SttError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except SystemExit as exc:  # keys.load_groq_key: missing key/env
            print(str(exc), file=sys.stderr)
            return 1
    try:
        result = run_script_pipeline(
            args.video, lines, args.fonts_dir, args.out,
            frames_dir=args.frames_dir, words=words, keep_temp=args.keep_temp,
            keywords=(args.keywords.split(",") if args.keywords else None),
            job_json=args.job_json, profile=args.profile)
    except (BurnError, AudioReencodeRequiredError, QaError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(result["report"])
    if not result.get("ok"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
