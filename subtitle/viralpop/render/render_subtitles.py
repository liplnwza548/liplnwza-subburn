#!/usr/bin/env python3
"""Execute an AI-generated ASS subtitle file and render a verified MP4."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "fonts"
FONT_FILE = FONT_DIR / "Prompt-Bold.ttf"


def die(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def escape_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return value.replace("\\", "/").replace(":", r"\:").replace("'", r"'\''")


def probe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path)
    ]
    try:
        raw = subprocess.check_output(cmd, text=True, encoding="utf-8")
        return json.loads(raw)
    except Exception as exc:
        die(f"ffprobe failed for {path}: {exc}")


def read_playres(ass: Path) -> tuple[int, int]:
    text = ass.read_text(encoding="utf-8")
    mx = re.search(r"PlayResX:\s*(\d+)", text)
    my = re.search(r"PlayResY:\s*(\d+)", text)
    if not mx or not my:
        die(f"ASS has no PlayResX/PlayResY: {ass}")
    return int(mx.group(1)), int(my.group(1))


def ffmpeg_has_filter(name: str) -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, check=False,
        ).stdout
    except Exception as exc:
        die(f"could not list ffmpeg filters: {exc}")
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.MULTILINE) is not None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("ass", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--filter", choices=["subtitles", "ass"], default="subtitles",
                    help="libass ingestion path (default subtitles = QA-proven path)")
    args = ap.parse_args()

    video = args.video.resolve()
    ass = args.ass.resolve()
    output = args.output.resolve()

    if not video.is_file():
        die(f"input video not found: {video}")
    if not ass.is_file():
        die(f"ASS file not found: {ass}")
    if not FONT_FILE.is_file():
        die(f"required font not found: {FONT_FILE}")
    if FONT_FILE.read_bytes()[:4] != b"\x00\x01\x00\x00":
        die("Prompt-Bold.ttf is not a valid TrueType font")

    output.parent.mkdir(parents=True, exist_ok=True)
    if not ffmpeg_has_filter(args.filter):
        die(f"this ffmpeg build has no '{args.filter}' filter (no silent fallback)")
    if args.filter == "ass":
        # ass= sizes fonts from the frame unless original_size is explicit;
        # pin it to the ASS PlayRes so \pos() never drifts silently.
        play_w, play_h = read_playres(ass)
        vf = (
            "ass='" + escape_filter_path(ass) + "'"
            ":fontsdir='" + escape_filter_path(FONT_DIR) + "'"
            f":original_size={play_w}x{play_h}"
        )
    else:
        vf = (
            "subtitles='" + escape_filter_path(ass) + "'"
            ":fontsdir='" + escape_filter_path(FONT_DIR) + "'"
        )
    print(f"Filter: {args.filter}  VF: {vf}")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(video), "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
        str(output),
    ]
    print("Rendering with Prompt Bold...")
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        die(f"ffmpeg render failed with exit code {result.returncode}")
    if not output.is_file() or output.stat().st_size == 0:
        die("render command returned success but output MP4 is missing/empty")

    info = probe(output)
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        die("output has no video stream")
    input_info = probe(video)
    if any(s.get("codec_type") == "audio" for s in input_info.get("streams", [])):
        if not any(s.get("codec_type") == "audio" for s in streams):
            die("input had audio but output has no audio stream")

    print(f"OK: {output}")
    print(f"SIZE: {output.stat().st_size} bytes")
    print(f"DURATION: {info.get('format', {}).get('duration', 'unknown')}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
