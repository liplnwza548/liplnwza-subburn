#!/usr/bin/env python3
"""Ticket 04: FFmpeg burn with Lip traps + ffprobe/QA frames.

Single entrypoint:

    burn_script_aligned(video, alignment, fonts_dir, out_mp4, ...) -> dict

Lip-locked traps (SPEC: Style / ffmpeg):
- Gate MUST be clear first: ``script_gate.assert_gate`` raises
  ``MismatchGateError`` when mismatches are non-empty. No ASS is
  written and ffmpeg never runs in that case.
- ASS is written via ``script_ass.build_ass`` into a TEMP dir only.
- Fonts are copied into ``<tmp>/fdir/``; ASS ``FontName`` is
  ``Prompt Bold``; filter carries ``fontsdir=fdir``.
- ffmpeg runs with ``cwd=<tmp>`` and BARE filenames only:
  ``-vf "ass=sub.ass:fontsdir=fdir"``. The string ``subtitles=``
  never appears; no drive letters (``C:``/``D:``) and no path
  separators appear inside ``-vf``.
- Audio: ``-c:a copy`` always. If the mux needs a re-encode, raise
  ``AudioReencodeRequiredError`` (stop-and-ask) instead of
  silently re-encoding.
- Video: libx264 + yuv420p + ``+faststart``.

Post-burn QA (``qa_probe_and_frames``):
- ffprobe: output WxH must equal source WxH (real clips are
  1080x1920, so both hold there; synthetic smoke clips pass as
  long as burn preserved geometry).
- duration delta vs source <= 0.1s.
- audio: when the source has an audio stream, the output must
  keep one.
- 3 stills at ~10% / 50% / 90% of duration.

Writes: <tmp>/sub.ass + <tmp>/fdir/* (temp), out_mp4, 3 PNG frames.
Raises ``MismatchGateError`` / ``BurnError`` / ``QaError``.
Stdlib + ffmpeg/ffprobe only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from script_ass import build_ass
from script_gate import MismatchGateError, assert_gate

#: Bare filenames inside the temp dir (never absolute, never drive-letter).
ASS_FILENAME = "sub.ass"
FONTS_SUBDIR = "fdir"

#: The one allowed libass filter shape. No ``original_size`` suffix:
#: with bare filenames libass sizes from the frame and PlayRes matches
#: the frame on the portrait path, so the extra option only adds a
#: second place for geometry to disagree.
FILTER_STRING = f"ass={ASS_FILENAME}:fontsdir={FONTS_SUBDIR}"

#: Duration tolerance (seconds) between source and burned output.
DURATION_TOLERANCE = 0.1

#: QA frame positions as fractions of duration.
FRAME_FRACTIONS = (0.10, 0.50, 0.90)

#: Font extensions copied into fdir/.
FONT_EXTENSIONS = (".ttf", ".otf", ".ttc")


class BurnError(Exception):
    """ffmpeg burn failed (command, exit code and stderr in args)."""


class AudioReencodeRequiredError(BurnError):
    """Audio stream could not be copied; stop and ask Lip.

    Raised instead of silently re-encoding. Lip decides whether a
    re-encode (e.g. ``-c:a aac``) is acceptable for the clip.
    """


class QaError(Exception):
    """Post-burn ffprobe / QA-frame check failed."""


def build_filter_string(
    ass_filename: str = ASS_FILENAME,
    fonts_subdir: str = FONTS_SUBDIR,
) -> str:
    """Return the one allowed ``-vf`` value and trap forbidden shapes.

    Refuses (raises ``ValueError``) when:
    - the legacy ``subtitles=`` filter is requested or present,
    - a component carries a drive letter (``C:``/``D:``) or a path
      separator (only bare filenames are allowed; ffmpeg runs with
      ``cwd=<tmp>`` so libass never sees an absolute path).
    """
    if ass_filename.strip().lower().startswith("subtitles="):
        raise ValueError("refusing 'subtitles=' filter — use ass= only")
    for label, part in (("ass filename", ass_filename),
                        ("fontsdir", fonts_subdir)):
        if len(part) >= 2 and part[1] == ":":
            raise ValueError(f"refusing drive-letter path in {label}: {part!r}")
        if "/" in part or "\\" in part:
            raise ValueError(f"refusing path separator in {label}: {part!r}")
    vf = f"ass={ass_filename}:fontsdir={fonts_subdir}"
    if "subtitles=" in vf:
        raise ValueError("refusing 'subtitles=' filter — use ass= only")
    return vf


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None,
        check=False,
    )


def probe_streams(path: Path) -> dict:
    """ffprobe streams+format for *path*; raises ``QaError`` on failure."""
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format",
           "-of", "json", str(path)]
    try:
        proc = _run(cmd)
    except FileNotFoundError as exc:
        raise QaError(f"ffprobe not found: {exc}")
    if proc.returncode != 0:
        raise QaError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QaError(f"ffprobe returned non-JSON for {path}: {exc}")


def media_info(path: Path) -> dict:
    """Pick {width, height, duration, has_audio, n_streams} from ffprobe."""
    info = probe_streams(path)
    video = next(
        (s for s in info.get("streams", [])
         if s.get("codec_type") == "video"), None)
    if video is None:
        raise QaError(f"no video stream in {path}")
    try:
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise QaError(f"unreadable video geometry in {path}: {exc}")
    try:
        duration = float(info.get("format", {}).get("duration", "nan"))
    except (TypeError, ValueError):
        duration = float("nan")
    has_audio = any(s.get("codec_type") == "audio"
                    for s in info.get("streams", []))
    return {"width": width, "height": height, "duration": duration,
            "has_audio": has_audio,
            "n_streams": len(info.get("streams", []))}


def copy_fonts_to_fdir(fonts_dir: Path, tmp_dir: Path) -> Path:
    """Copy font files from *fonts_dir* into ``<tmp>/fdir/``.

    Requires at least one font file and requires a Prompt Bold face
    (the ASS ``FontName`` is ``Prompt Bold``), otherwise libass would
    silently fall back to another face and Thai marks break.
    """
    src = Path(fonts_dir)
    if not src.is_dir():
        raise BurnError(f"fonts dir not found: {src}")
    candidates = [p for p in sorted(src.iterdir())
                  if p.is_file() and p.suffix.lower() in FONT_EXTENSIONS]
    if not candidates:
        raise BurnError(f"no font files (*{', '.join(FONT_EXTENSIONS)}) in {src}")
    if not any("prompt" in p.stem.lower() and "bold" in p.stem.lower()
               for p in candidates):
        raise BurnError(
            f"Prompt Bold font missing in {src} "
            f"(ASS FontName is 'Prompt Bold'; found {[p.name for p in candidates]})"
        )
    fdir = Path(tmp_dir) / FONTS_SUBDIR
    fdir.mkdir(parents=True, exist_ok=True)
    for font in candidates:
        shutil.copy2(font, fdir / font.name)
    return fdir


def _looks_like_audio_copy_failure(stderr: str) -> bool:
    hay = stderr.lower()
    return ("-c:a copy" in hay or "audio" in hay) and any(
        key in hay for key in (
            "could not write header",
            "invalid data found",
            "codec not currently supported in container",
            "only aac",
            "malformed",
            "conversion failed",
            "muxer does not support",
        )
    )


def run_ffmpeg_burn(video: Path, out_mp4: Path, tmp_dir: Path,
                    vf: str) -> None:
    """Run the locked ffmpeg burn from inside *tmp_dir*.

    ``-i`` takes the absolute source path (outside ``-vf`` drive
    letters there are harmless); ``-vf`` itself stays bare filenames.
    Audio is always ``-c:a copy``. On failure raises ``BurnError``,
    or ``AudioReencodeRequiredError`` when the log points at an
    audio-copy incompatibility (stop-and-ask: never auto re-encode).
    """
    if "subtitles=" in vf:
        raise BurnError("refusing 'subtitles=' filter — use ass= only")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(Path(video).resolve()),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(Path(out_mp4).resolve()),
    ]
    try:
        proc = _run(cmd, cwd=tmp_dir)
    except FileNotFoundError as exc:
        raise BurnError(f"ffmpeg not found: {exc}")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if _looks_like_audio_copy_failure(stderr):
            raise AudioReencodeRequiredError(
                "ffmpeg burn failed on audio copy (-c:a copy). "
                "STOP and ask Lip whether an audio re-encode "
                "(e.g. -c:a aac) is acceptable — do NOT silently "
                f"re-encode.\nffmpeg stderr:\n{stderr}"
            )
        raise BurnError(
            f"ffmpeg render failed with exit code {proc.returncode}\n"
            f"cmd: {' '.join(cmd)}\nffmpeg stderr:\n{stderr}"
        )
    if not out_mp4.is_file() or out_mp4.stat().st_size == 0:
        raise BurnError(
            "ffmpeg returned success but output MP4 is missing/empty: "
            f"{out_mp4}")


def export_qa_frames(out_mp4: Path, duration: float,
                     frames_dir: Path) -> list[Path]:
    """Export 3 stills (~10%/50%/90%) from the burned output."""
    import math

    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(duration) or duration <= 0:
        raise QaError(f"cannot place QA frames: bad duration {duration!r}")
    paths: list[Path] = []
    for n, frac in enumerate(FRAME_FRACTIONS, 1):
        stamp = max(0.0, duration * frac)
        frame = frames_dir / f"qa_{n:02d}_{int(frac * 100):02d}pct.png"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{stamp:.3f}", "-i", str(out_mp4.resolve()),
               "-frames:v", "1", str(frame.resolve())]
        try:
            proc = _run(cmd)
        except FileNotFoundError as exc:
            raise QaError(f"ffmpeg not found: {exc}")
        if proc.returncode != 0 or not frame.is_file():
            raise QaError(
                f"QA frame {n} (t={stamp:.3f}s) failed: "
                f"{(proc.stderr or '').strip()}")
        paths.append(frame)
    return paths


def qa_probe_and_frames(out_mp4: Path, source_mp4: Path,
                        frames_dir: Path) -> dict:
    """ffprobe gates + 3 QA stills. Raises ``QaError`` on any failure."""
    src = media_info(Path(source_mp4))
    out = media_info(Path(out_mp4))
    if (out["width"], out["height"]) != (src["width"], src["height"]):
        raise QaError(
            f"geometry changed by burn: source "
            f"{src['width']}x{src['height']} vs output "
            f"{out['width']}x{out['height']}")
    delta = abs(out["duration"] - src["duration"])
    if not delta <= DURATION_TOLERANCE:
        raise QaError(
            f"duration drift {delta:.3f}s exceeds "
            f"{DURATION_TOLERANCE:.1f}s "
            f"(source {src['duration']:.3f}s vs output {out['duration']:.3f}s)")
    if src["has_audio"] and not out["has_audio"]:
        raise QaError("source had audio but output has no audio stream")
    frames = export_qa_frames(Path(out_mp4), src["duration"], frames_dir)
    return {"src": src, "out": out, "duration_delta": delta, "frames": frames}


def burn_script_aligned(video, alignment: dict, fonts_dir, out_mp4,
                        frames_dir=None, keep_temp: bool = False,
                        keywords=None, profile: str = "general") -> dict:
    """Gate -> ASS (temp) -> fonts (temp fdir) -> ffmpeg burn -> QA.

    Returns a BurnReport dict::

        {"ok": True, "out_mp4": str, "filter": str,
         "width": int, "height": int,
         "duration_src": float, "duration_out": float,
         "duration_delta": float, "has_audio": bool,
         "frames": [str, str, str], "report": str}

    Raises ``MismatchGateError`` (gate blocked: nothing written,
    ffmpeg never runs), ``BurnError`` /
    ``AudioReencodeRequiredError``, or ``QaError``.
    """
    # 1. Gate first — refuse before any file is written.
    gate_report = assert_gate(alignment)

    video = Path(video)
    if not video.is_file():
        raise BurnError(f"input video not found: {video}")
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = (Path(frames_dir) if frames_dir is not None
                  else out_mp4.parent / (out_mp4.stem + "_qa"))
    vf = build_filter_string()

    tmp_holder = None
    try:
        if keep_temp:
            tmp = Path(tempfile.mkdtemp(prefix="burn_script_"))
        else:
            tmp_holder = tempfile.TemporaryDirectory(prefix="burn_script_")
            tmp = Path(tmp_holder.name)
        # 2. ASS via ticket-02 builder into the temp dir.
        # v1.1 T3: keywords colorize SCRIPT substrings (None/[] = no
        # yellow); T4 overlays ride along inside build_ass.
        ass_path = tmp / ASS_FILENAME
        build_ass(alignment, out_path=ass_path, keywords=keywords,
                  profile=profile)
        # 3. Fonts into temp fdir/.
        copy_fonts_to_fdir(Path(fonts_dir), tmp)
        # 4. Burn from inside temp (bare filenames in -vf).
        run_ffmpeg_burn(video, out_mp4, tmp, vf)
        # 5. Post-burn QA.
        qa = qa_probe_and_frames(out_mp4, video, frames_dir)
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()

    report = BurnReport(
        out_mp4=out_mp4, vf=vf, gate_report=gate_report, qa=qa)
    return report


def BurnReport(out_mp4: Path, vf: str, gate_report: str, qa: dict) -> dict:
    """Assemble the plain-dict burn report (kept a function for tests)."""
    src, out = qa["src"], qa["out"]
    lines = [
        "SCRIPT-BURN REPORT: OK",
        gate_report,
        f"filter: -vf {vf!r} (ass= only, bare filenames, cwd=temp)",
        f"output: {out_mp4}",
        f"geometry: {out['width']}x{out['height']} "
        f"(source {src['width']}x{src['height']})",
        f"duration: src {src['duration']:.3f}s vs out "
        f"{out['duration']:.3f}s (delta {qa['duration_delta']:.3f}s "
        f"<= {DURATION_TOLERANCE:.1f}s)",
        f"audio: {'present' if out['has_audio'] else 'absent'} "
        f"(source {'present' if src['has_audio'] else 'absent'})",
        "frames:",
    ]
    lines.extend(f"  - {p}" for p in qa["frames"])
    return {
        "ok": True,
        "out_mp4": str(out_mp4),
        "filter": vf,
        "width": out["width"],
        "height": out["height"],
        "duration_src": src["duration"],
        "duration_out": out["duration"],
        "duration_delta": qa["duration_delta"],
        "has_audio": out["has_audio"],
        "frames": [str(p) for p in qa["frames"]],
        "report": "\n".join(lines),
    }


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Burn script-aligned ASS onto a finished clip "
                    "(gate + ass= + fontsdir + -c:a copy + QA).")
    ap.add_argument("video", type=Path)
    ap.add_argument("words_json", type=Path,
                    help="Groq verbose_json (or {'words': [...]}) for timing")
    ap.add_argument("script_txt", type=Path,
                    help="Lip's script, one cue per line")
    ap.add_argument("output", type=Path)
    ap.add_argument("--fonts-dir", type=Path, default=None)
    ap.add_argument("--frames-dir", type=Path, default=None)
    args = ap.parse_args()

    from script_align import align_script_to_timeline

    root = Path(__file__).resolve().parents[1]
    fonts_dir = args.fonts_dir or (root / "fonts")
    script_lines = [ln.strip() for ln in
                    args.script_txt.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
    words = json.loads(args.words_json.read_text(encoding="utf-8"))
    alignment = align_script_to_timeline(script_lines, words)
    try:
        result = burn_script_aligned(
            args.video, alignment, fonts_dir, args.output,
            frames_dir=args.frames_dir)
    except MismatchGateError as exc:
        print(str(exc))
        return 2
    print(result["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
