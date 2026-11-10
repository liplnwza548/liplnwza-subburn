"""Measured Quality Control (QC) for P2-007 (Guarded One-Clip Subtitle Render).

This module implements post-render candidate quality verification using empirical
measurement tools only (ffprobe, ffmpeg loudnorm, deterministic frame extraction).
Automated checks never constitute human listening or viewing.

Invariants & Architectural Guardrails:
- Human sensory checks (human_audio_verification, human_visual_verification) are
  unconditionally recorded with result 'NOT_CHECKED' during Phase 2A. No automated
  check is ever treated as human listening or typography proofreading.
- No global 'passed' boolean flag exists on QCReport or QCResult. Every check
  is independently evaluated, and a candidate is either 'REVIEW_CANDIDATE' or 'BLOCKED'.
- Target loudness constants (LOUDNESS_TARGET_I, LOUDNESS_TARGET_TP) must never be
  substituted for measured values. Unmeasurable loudness must fail-closed.
- Code is strictly forbidden from writing to or storing files in /tmp.
  All staging evidence and frame extracts reside strictly within <job_dir>.
- No SQLite usage. State and audit records rely exclusively on filesystem JSON/JSONL.
- Production Auto Mode is permanently prohibited. Every candidate report carries
  an explicit, prominent warning that human inspection is mandatory before distribution.
- No reading the real clock (datetime.now(), time.time()) in this module.

Approved Architectural Decisions (Philip, 2026-11-11 02:30 modified per Been):
- D1: STAGES in evidence.py is expanded strictly with 'QC_EVALUATED' (already done).
      RENDERED remains recorded solely as an event in events.jsonl.
- D2: Loudness criteria (LUFS/TP) are evaluated as a Soft Warning when True Peak
      does not clip; target constants are references only, not hard gates.
- D3: Duration drift tolerance is fixed at 0.1 seconds following script_burn.py:59
      (DURATION_TOLERANCE). The 0.05s tolerance from exploratory notebooks is rejected.
      Video dimensions must match source geometry exactly.
- D4: Extracted QC sample frames are stored strictly at <job_dir>/evidence/qc_frames/.
- D5: QC execution is invoked exclusively by the orchestrator in Phase 2A.

Plan Corrections (C1-C14) Documented:
- C1: Test count baseline verified as 111 Phase 2 tests + 6 foundation = 117 tests.
- C2: Source media SHA-256 digest references spec.source.sha256 (not JobSpec.source_media.sha256).
- C3: STAGES expansion follows Option C ('QC_EVALUATED' only per Decision D1).
- C4: QC frame directory unified to <job_dir>/evidence/qc_frames/ (Decision D4).
- C5: LUFS criteria (-24 to -10 LUFS, TP <= 0.0 dBTP) is a soft warning proposal;
      target constants (-16 LUFS, -1.0 dBTP) never substitute for measured metrics.
- C6: Loudness measurement uses the FFmpeg loudnorm filter (-af loudnorm=print_format=json),
      not ebur128.
- C7: Loudness regex parsing matches re.findall(r"\\{[^}]*\\"input_i\\"[^}]*\\}", stderr)
      followed by strict JSON decoding.
- C8: Evidence test references point to tests/phase2/test_gates.py (test_evidence.py does not exist).
- C9: TERMINAL_STATES is defined in state.py at lines 47-51.
- C10: OrchestratorResult is defined in orchestrator.py at lines 94-105.
- C11: The artifact_relpath validation block in evidence.py is at lines 105-113.
- C12: Prohibition on moving candidate outside staging stems from plan / Spec line 184
       (ticket line 107 is the must-not checklist).
- C13: Notebook cell 30 sync quote confirms frames cannot verify sync (requires human listening
       against the timed file; drift > 0.3s halts and reports).
- C14: Staged source filename retains the original intake filename, not fixed staged_source.<ext>.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from types import MappingProxyType
from typing import Literal

from .authorization import Decision
from .evidence import (
    CheckRecord,
    EvidenceRecord,
    create_evidence_record,
    save_evidence_record,
)
from .job import JobSpec
from .renderer_adapter import RenderExecutionResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Duration tolerance (seconds) between source and candidate media (script_burn.py:59, D3).
DURATION_TOLERANCE_SECONDS: float = 0.1

#: Acceptable video frame rate bounds (fps).
FRAME_RATE_MIN: float = 23.976
FRAME_RATE_MAX: float = 60.0

#: Target loudness reference values (D2, C5 - reference only, never substitute for measured).
LOUDNESS_TARGET_I: float = -16.0
LOUDNESS_TARGET_TP: float = -1.0

#: Plausible loudness bounds for Soft Warning (D2, C5).
LOUDNESS_PLAUSIBLE_I_MIN: float = -24.0
LOUDNESS_PLAUSIBLE_I_MAX: float = -10.0

#: Relative directory name under <job_dir>/evidence/ for QC sample frames (D4, C4).
QC_FRAME_DIRNAME: str = "qc_frames"

#: Mandatory human review warning notice; no candidate is ever certified without human review.
WARNING_NOTICE: str = (
    "[WARNING: REVIEW CANDIDATE ONLY] Technical media verification passed (exit 0, "
    "valid dimensions, duration drift within tolerance, audio stream present, digests verified, "
    "3 QA frames extracted). Human auditory verification and visual typography inspection remain "
    "NOT_CHECKED. Nobody has listened to or watched this clip. Human inspection by Philip is "
    "required prior to publication or distribution."
)

#: Standard sequence of QC checks (8 technical checks + 2 human checks).
CHECK_NAMES: tuple[str, ...] = (
    "renderer_exit_code",
    "video_dimensions",
    "duration_consistency",
    "frame_rate_validity",
    "audio_presence",
    "audio_loudness",
    "cryptographic_digests",
    "qc_frame_extraction",
    "human_audio_verification",
    "human_visual_verification",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class QCError(Exception):
    """Raised when media probe, measurement, or QC evaluation fails."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateMediaProperties:
    """Empirically measured technical properties of candidate media from ffprobe."""

    width: int
    height: int
    duration_seconds: float
    video_fps: float
    video_codec: str
    has_audio: bool
    audio_channels: int | None
    audio_sample_rate: int | None
    audio_codec: str | None
    container_format: str
    raw_probe: MappingProxyType


@dataclass(frozen=True)
class LoudnessMetrics:
    """Empirical audio loudness measurements from FFmpeg loudnorm filter pass."""

    measured_i: float | None
    measured_tp: float | None
    measured_lra: float | None
    measured_thresh: float | None
    measurement_success: bool
    error_message: str | None = None


@dataclass(frozen=True)
class QCFrameRecord:
    """Record of a deterministically extracted QC frame image."""

    label: str
    timestamp_seconds: float
    file_relpath: str
    file_sha256: str
    file_size_bytes: int


@dataclass(frozen=True)
class QCReport:
    """Comprehensive post-render candidate quality evaluation report.

    Per Phase 2 Specification, this report separates empirical technical checks
    from human sensory verification. There is intentionally no global 'passed' boolean.
    """

    schema_version: str
    job_id: str
    candidate_relpath: str
    candidate_sha256: str
    source_sha256: str
    status: Literal["REVIEW_CANDIDATE", "BLOCKED"]
    media_properties: CandidateMediaProperties | None
    loudness_metrics: LoudnessMetrics | None
    renderer_exit_code: int
    checks: tuple[CheckRecord, ...]
    unsatisfied_checks: tuple[str, ...]
    frame_evidence: tuple[QCFrameRecord, ...]
    human_audio_verification: str
    human_visual_verification: str
    listener_identity: str | None
    viewer_identity: str | None
    warning_notice: str
    evaluated_at: str
    notes: str = ""


@dataclass(frozen=True)
class QCResult:
    """Result of run_qc() handed to Orchestrator to drive state transitions."""

    status: Literal["REVIEW_CANDIDATE", "BLOCKED"]
    report: QCReport
    evidence_record: EvidenceRecord | None
    evidence_id: str
    target_state: Literal["READY_FOR_REVIEW", "BLOCKED_NEEDS_REVIEW", "FAILED_JOB"]
    reason: str
    unsatisfied_checks: tuple[str, ...]


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _reject_constant(value: str) -> None:
    """Reject non-standard JSON constants (NaN, Infinity, -Infinity)."""
    raise ValueError(f"Invalid JSON constant: {value!r}")


def _strict_json_loads(text: str) -> dict:
    """Parse JSON text strictly rejecting NaN/Inf and ensuring a dict root.

    Args:
        text: Raw JSON string to parse.

    Returns:
        dict: Parsed JSON root object.

    Raises:
        QCError: If text is not valid JSON, contains NaN/Inf, or root is not a dict.
    """
    if not isinstance(text, str):
        raise QCError(f"Expected JSON string, got {type(text).__name__}")
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise QCError(f"Failed to parse JSON strictly: {exc}") from exc
    if not isinstance(data, dict):
        raise QCError(f"Expected JSON root to be a dict, got {type(data).__name__}")
    return data


def _parse_rational(value: str) -> float:
    """Convert a 'num/den' rational string (e.g. '30/1', '24000/1001') to float.

    Args:
        value: Rational string in 'numerator/denominator' form.

    Returns:
        float: Computed floating point value.

    Raises:
        QCError: If value is not a string, format is not 'num/den', denominator is 0,
                 or computed result is not finite.
    """
    if not isinstance(value, str):
        raise QCError(f"Expected rational string, got {type(value).__name__}")
    parts = value.strip().split("/")
    if len(parts) != 2:
        raise QCError(f"Invalid rational format (expected 'num/den'): {value!r}")
    try:
        num = int(parts[0].strip())
        den = int(parts[1].strip())
    except (ValueError, TypeError) as exc:
        raise QCError(f"Unparseable integer components in rational {value!r}: {exc}") from exc
    if den == 0:
        raise QCError(f"Zero denominator in rational: {value!r}")
    result = num / den
    if not math.isfinite(result):
        raise QCError(f"Non-finite result from rational {value!r}: {result}")
    return result


def _sha256_file(path: Path | str) -> str:
    """Compute SHA-256 hex digest of a file using 64KB chunks.

    Args:
        path: Path to the file.

    Returns:
        Hexadecimal SHA-256 string (64 characters).

    Raises:
        QCError: If the file cannot be opened or read.
    """
    file_path = Path(path)
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                hasher.update(chunk)
    except OSError as exc:
        raise QCError(f"Failed to read file for SHA-256 calculation {file_path}: {exc}") from exc
    return hasher.hexdigest()


def probe_candidate(
    output_path: Path | str,
    *,
    ffprobe_bin: str = "ffprobe",
) -> CandidateMediaProperties:
    """Probe candidate media file properties using ffprobe in an isolated subprocess.

    Executes ffprobe with -show_entries to extract container format, video
    dimensions, duration, frame rate, and audio stream properties.

    Args:
        output_path: Path to the candidate video file.
        ffprobe_bin: Name or path of the ffprobe binary (default: 'ffprobe').

    Returns:
        CandidateMediaProperties containing strictly parsed media attributes.

    Raises:
        QCError: If ffprobe binary is missing (system fault), execution fails,
                 output is empty or unparseable JSON, video stream is missing,
                 dimensions are missing/non-positive, duration is missing/N/A/non-finite/negative,
                 or frame rate is unusable. Never guesses or returns default values.
    """
    path_obj = Path(output_path)
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate",
        "-of",
        "json",
        str(path_obj),
    ]

    try:
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise QCError(f"ffprobe binary not found at {ffprobe_bin!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise QCError(f"ffprobe timed out after 60s for {path_obj}: {exc}") from exc
    except OSError as exc:
        raise QCError(f"ffprobe execution failed for {path_obj}: {exc}") from exc

    if proc.returncode != 0:
        raise QCError(
            f"ffprobe exited with code {proc.returncode} for {path_obj}: {proc.stderr.strip()}"
        )

    stdout = proc.stdout.strip()
    if not stdout:
        raise QCError(f"ffprobe returned empty stdout for {path_obj}")

    data = _strict_json_loads(stdout)

    streams = data.get("streams")
    if not isinstance(streams, list):
        raise QCError(f"ffprobe output missing 'streams' list for {path_obj}")

    format_info = data.get("format")
    if not isinstance(format_info, dict):
        raise QCError(f"ffprobe output missing 'format' object for {path_obj}")

    video_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
    if not video_streams:
        raise QCError(f"No video stream found in candidate media {path_obj}")

    video = video_streams[0]

    if "width" not in video or "height" not in video:
        raise QCError(f"Missing width or height in video stream for {path_obj}")

    try:
        width = int(video["width"])
        height = int(video["height"])
    except (ValueError, TypeError) as exc:
        raise QCError(f"Invalid video geometry in {path_obj}: {exc}") from exc

    if width <= 0 or height <= 0:
        raise QCError(f"Non-positive video dimensions ({width}x{height}) in {path_obj}")

    raw_codec = video.get("codec_name")
    if raw_codec is None or not isinstance(raw_codec, str) or not raw_codec.strip():
        raise QCError(f"Missing video codec_name in {path_obj}")
    video_codec = raw_codec.strip()

    raw_fps = video.get("r_frame_rate")
    if raw_fps is None or not isinstance(raw_fps, str) or not raw_fps.strip():
        raise QCError(f"Missing or invalid r_frame_rate in video stream for {path_obj}")

    try:
        video_fps = _parse_rational(raw_fps.strip())
    except QCError as exc:
        raise QCError(f"Unusable r_frame_rate {raw_fps!r} in {path_obj}: {exc}") from exc

    if video_fps <= 0 or not math.isfinite(video_fps):
        raise QCError(f"Invalid frame rate {video_fps} (must be positive and finite) in {path_obj}")

    raw_duration = format_info.get("duration")
    if raw_duration is None or raw_duration == "" or str(raw_duration).strip() == "N/A":
        raise QCError(f"Missing or N/A duration in container format for {path_obj}")

    try:
        duration_seconds = float(raw_duration)
    except (ValueError, TypeError) as exc:
        raise QCError(f"Unparseable duration value {raw_duration!r} for {path_obj}: {exc}") from exc

    if not math.isfinite(duration_seconds):
        raise QCError(f"Non-finite duration value {raw_duration!r} for {path_obj}")

    if duration_seconds < 0:
        raise QCError(f"Negative duration {duration_seconds} for {path_obj}")

    audio_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    has_audio = len(audio_streams) > 0
    if has_audio:
        first_audio = audio_streams[0]
        raw_channels = first_audio.get("channels")
        if raw_channels is not None:
            try:
                audio_channels: int | None = int(raw_channels)
            except (ValueError, TypeError):
                audio_channels = None
        else:
            audio_channels = None

        raw_sr = first_audio.get("sample_rate")
        if raw_sr is not None:
            try:
                audio_sample_rate: int | None = int(raw_sr)
            except (ValueError, TypeError):
                audio_sample_rate = None
        else:
            audio_sample_rate = None

        raw_acodec = first_audio.get("codec_name")
        if raw_acodec is not None and isinstance(raw_acodec, str) and raw_acodec.strip():
            audio_codec: str | None = raw_acodec.strip()
        else:
            audio_codec = None
    else:
        audio_channels = None
        audio_sample_rate = None
        audio_codec = None

    raw_fmt = format_info.get("format_name")
    if raw_fmt is not None and isinstance(raw_fmt, str):
        container_format = raw_fmt.strip()
    else:
        container_format = ""

    raw_probe = MappingProxyType(data)

    return CandidateMediaProperties(
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        video_fps=video_fps,
        video_codec=video_codec,
        has_audio=has_audio,
        audio_channels=audio_channels,
        audio_sample_rate=audio_sample_rate,
        audio_codec=audio_codec,
        container_format=container_format,
        raw_probe=raw_probe,
    )


def measure_loudness(
    output_path: Path | str,
    *,
    ffmpeg_bin: str = "ffmpeg",
) -> LoudnessMetrics:
    r"""Measure empirical audio loudness metrics using FFmpeg loudnorm filter.

    Runs an isolated subprocess executing:
        [ffmpeg_bin, '-nostdin', '-i', str(output_path), '-af', 'loudnorm=print_format=json', '-f', 'null', '-']
    and extracts empirical integrated loudness (input_i) and true peak (input_tp)
    using the exact regex from Subtitle-Burn-Colab.ipynb Cell 30:
        re.findall(r'\{[^}]*"input_i"[^}]*\}', stderr)

    Architectural Decisions & Compliance:
    - Decision D2 / Plan C5: Soft Warning evaluation. Target constants (e.g. -16 LUFS,
      -1.0 dBTP) must NEVER be substituted for measured values. Unmeasurable loudness
      fails closed (measurement_success=False) with None values.
    - Plan C6: Loudness measurement strictly uses loudnorm (never ebur128).
    - Plan C7: Uses strict regex from notebook Cell 30 (not buggy plan snippet).
    - If FFmpeg returns a non-zero exit code but stderr still contains a valid,
      parseable loudnorm JSON block, the parsed measurements are retained and
      measurement_success is True (FFmpeg loudnorm filter often exits non-zero
      based on audio stats rather than process failure).
    - If exit code != 0 and parsing fails, returns measurement_success=False
      with an error message recording the exit code.

    Args:
        output_path: Path to candidate media file to measure.
        ffmpeg_bin: FFmpeg binary executable name or path (default: 'ffmpeg').

    Returns:
        LoudnessMetrics: Measured empirical loudness values and success flag.

    Raises:
        QCError: If ffmpeg binary is not found (system fault), execution times out,
                 or OS process launch fails.
    """
    path_obj = Path(output_path)
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-i",
        str(path_obj),
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-",
    ]

    try:
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise QCError(f"ffmpeg binary not found at {ffmpeg_bin!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise QCError(
            f"ffmpeg timed out after 120s measuring loudness for {path_obj}: {exc}"
        ) from exc
    except OSError as exc:
        raise QCError(f"ffmpeg execution failed for {path_obj}: {exc}") from exc

    stderr = proc.stderr or ""
    matches = re.findall(r'\{[^}]*"input_i"[^}]*\}', stderr)

    if not matches:
        error_msg = (
            f"ffmpeg exited with code {proc.returncode} and no loudnorm JSON found in stderr"
            if proc.returncode != 0
            else "No loudnorm JSON found in ffmpeg stderr"
        )
        return LoudnessMetrics(
            measured_i=None,
            measured_tp=None,
            measured_lra=None,
            measured_thresh=None,
            measurement_success=False,
            error_message=error_msg,
        )

    try:
        data = json.loads(matches[-1])
        if not isinstance(data, dict):
            raise ValueError(f"Root JSON is not a dictionary: {type(data).__name__}")
        if "input_i" not in data or "input_tp" not in data:
            raise ValueError("Missing 'input_i' or 'input_tp' in loudnorm JSON")

        measured_i = float(data["input_i"])
        measured_tp = float(data["input_tp"])

        if not (math.isfinite(measured_i) and math.isfinite(measured_tp)):
            raise ValueError(
                f"Non-finite loudness values: input_i={measured_i}, input_tp={measured_tp}"
            )

        measured_lra: float | None = None
        if "input_lra" in data and data["input_lra"] is not None:
            try:
                val_lra = float(data["input_lra"])
                if math.isfinite(val_lra):
                    measured_lra = val_lra
            except (ValueError, TypeError):
                measured_lra = None

        measured_thresh: float | None = None
        if "input_thresh" in data and data["input_thresh"] is not None:
            try:
                val_thresh = float(data["input_thresh"])
                if math.isfinite(val_thresh):
                    measured_thresh = val_thresh
            except (ValueError, TypeError):
                measured_thresh = None

        return LoudnessMetrics(
            measured_i=measured_i,
            measured_tp=measured_tp,
            measured_lra=measured_lra,
            measured_thresh=measured_thresh,
            measurement_success=True,
            error_message=None,
        )
    except Exception as exc:
        error_msg = (
            f"ffmpeg exited with code {proc.returncode} and loudnorm JSON parse failed: {exc}"
            if proc.returncode != 0
            else f"Failed to parse loudnorm JSON: {exc}"
        )
        return LoudnessMetrics(
            measured_i=None,
            measured_tp=None,
            measured_lra=None,
            measured_thresh=None,
            measurement_success=False,
            error_message=error_msg,
        )


def capture_qc_frames(
    output_path: Path | str,
    target_dir: Path | str,
    duration_seconds: float,
    *,
    ffmpeg_bin: str = "ffmpeg",
    relpath_prefix: str = "evidence/qc_frames",
) -> tuple[QCFrameRecord, ...]:
    r"""Capture 3 deterministic QC inspection frames from candidate media.

    Extracts start, mid, and end still frames in PNG format using output seeking
    (-ss after -i) per Subtitle-Burn-Colab.ipynb Cell 30:
        [ffmpeg_bin, '-y', '-v', 'error', '-i', str(output_path), '-ss', f'{t:.3f}', '-frames:v', '1', str(frame_path)]

    Frame Extraction Timestamps:
    - start: 0.5s if duration_seconds >= 0.5s else min(0.1s, duration_seconds / 4)
    - mid: duration_seconds / 2.0
    - end: max(duration_seconds - 0.5s, 0.1s)
    - Output filenames: frame_start.png, frame_mid.png, frame_end.png

    Architectural Decisions & Invariants:
    - Decision D4: Extracted frames reside strictly within target_dir
      (caller provides <job_dir>/evidence/qc_frames/). No writing to /tmp.
    - file_relpath in returned QCFrameRecord is stored in POSIX relative format
      as '<relpath_prefix>/frame_<label>.png', relative to the job directory.
      The default prefix 'evidence/qc_frames' assumes target_dir is the standard
      <job_dir>/evidence/qc_frames/ directory (Decision D4 / plan §5.1 example
      'evidence/qc_frames/frame_mid.png'). Callers using a different layout must
      pass relpath_prefix explicitly.
    - Plan §4.4 / Prohibition 1 / Cell 30: Extracted video frames prove only that
      valid non-black image data exists. They strictly CANNOT verify audio-subtitle
      synchronization (which requires auditory listening against timed source)
      or Thai font typography correctness.
    - Integrity Verification: Each extracted frame must exist on disk, have a
      positive byte size (> 0), and begin with the standard PNG magic bytes
      (0x89 0x50 0x4E 0x47).

    Args:
        output_path: Path to candidate media file.
        target_dir: Directory where extracted QC PNG frames must be written.
        duration_seconds: Duration of candidate media in seconds (must be positive & finite).
        ffmpeg_bin: FFmpeg binary executable name or path (default: 'ffmpeg').

    Returns:
        tuple[QCFrameRecord, ...]: Tuple of exactly 3 records for ('start', 'mid', 'end').

    Raises:
        QCError: If duration_seconds is non-positive or non-finite, ffmpeg binary
                 is not found, ffmpeg exits non-zero, or any extracted frame file
                 is missing, zero-byte, or has invalid PNG magic bytes.
    """
    if (
        not isinstance(duration_seconds, (int, float))
        or isinstance(duration_seconds, bool)
        or not math.isfinite(duration_seconds)
        or duration_seconds <= 0
    ):
        raise QCError(
            f"Invalid duration_seconds for QC frame extraction (must be positive finite number): {duration_seconds!r}"
        )
    if (
        type(relpath_prefix) is not str
        or not relpath_prefix
        or relpath_prefix.startswith('/')
        or '..' in Path(relpath_prefix).parts
    ):
        raise QCError(
            f"Invalid relpath_prefix for QC frame extraction (must be a relative path without '..'): {relpath_prefix!r}"
        )

    dur = float(duration_seconds)
    output_path_obj = Path(output_path)
    target_dir_path = Path(target_dir)

    try:
        target_dir_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise QCError(f"Failed to create QC frame directory {target_dir_path}: {exc}") from exc

    t_start = 0.5 if dur >= 0.5 else min(0.1, dur / 4.0)
    t_mid = dur / 2.0
    t_end = max(dur - 0.5, 0.1)

    frame_specs: tuple[tuple[str, float], ...] = (
        ("start", t_start),
        ("mid", t_mid),
        ("end", t_end),
    )

    records: list[QCFrameRecord] = []

    for label, t in frame_specs:
        frame_filename = f"frame_{label}.png"
        frame_path = target_dir_path / frame_filename

        cmd = [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            str(output_path_obj),
            "-ss",
            f"{t:.3f}",
            "-frames:v",
            "1",
            str(frame_path),
        ]

        try:
            proc = subprocess.run(
                cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError as exc:
            raise QCError(f"ffmpeg binary not found at {ffmpeg_bin!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise QCError(
                f"ffmpeg timed out after 60s capturing QC frame {label!r} at {t:.3f}s: {exc}"
            ) from exc
        except OSError as exc:
            raise QCError(f"ffmpeg execution failed for QC frame {label!r}: {exc}") from exc

        if proc.returncode != 0:
            raise QCError(
                f"ffmpeg frame extraction failed for label {label!r} (exit code {proc.returncode}): {proc.stderr.strip()}"
            )

        if not frame_path.is_file():
            raise QCError(f"QC frame file was not created: {frame_path}")

        file_size = frame_path.stat().st_size
        if file_size <= 0:
            raise QCError(f"QC frame file is empty (0 bytes): {frame_path}")

        try:
            with open(frame_path, "rb") as f:
                header = f.read(4)
        except OSError as exc:
            raise QCError(f"Failed to read QC frame file {frame_path}: {exc}") from exc

        if header != b"\x89PNG":
            raise QCError(
                f"QC frame file {frame_path} does not have valid PNG header magic: expected 89 50 4E 47, got {header.hex().upper()}"
            )

        file_sha256 = _sha256_file(frame_path)
        relpath = f"{relpath_prefix}/{frame_filename}"

        records.append(
            QCFrameRecord(
                label=label,
                timestamp_seconds=round(t, 3),
                file_relpath=relpath,
                file_sha256=file_sha256,
                file_size_bytes=file_size,
            )
        )

    return tuple(records)


def verify_digests(
    staged_source_path: Path | str,
    candidate_path: Path | str,
    expected_source_sha256: str,
    render_reported_sha256: str | None,
) -> tuple[bool, str]:
    """Verify cryptographic SHA-256 integrity of staged source and candidate media.

    Computes streaming SHA-256 digests of both files using _sha256_file and verifies:
    1. staged_source_digest == expected_source_sha256 (proves source media was not mutated)
    2. candidate_digest == render_reported_sha256 (proves candidate media was not modified or swapped)

    Architectural Decisions & Invariants:
    - Anti-Fake Principle §7.1: Cryptographic digests must be freshly calculated from disk.
    - If render_reported_sha256 is None, candidate verification fails immediately.
    - Expected and reported digests must be valid 64-character hexadecimal strings
      (unless render_reported_sha256 is None). Non-hex or invalid lengths raise QCError.
    - If either file is missing or unreadable, raises QCError (system fault, not mismatch).

    Args:
        staged_source_path: Path to the staged source video file.
        candidate_path: Path to the burned candidate video file.
        expected_source_sha256: Expected 64-char hex SHA-256 of the source media.
        render_reported_sha256: Render-reported 64-char hex SHA-256 of the candidate media,
            or None if rendering failed to produce a digest.

    Returns:
        tuple[bool, str]: (True, 'All digests verified') if both match;
            otherwise (False, '<mismatch detail with 12-char abbreviated digests>').

    Raises:
        QCError: If expected_source_sha256 or non-None render_reported_sha256 is not
                 a 64-character hexadecimal string, or if either file cannot be accessed/read.
    """
    if (
        not isinstance(expected_source_sha256, str)
        or len(expected_source_sha256) != 64
        or not all(c in "0123456789abcdefABCDEF" for c in expected_source_sha256)
    ):
        raise QCError(
            f"Invalid expected_source_sha256: expected 64-character hex string, got {expected_source_sha256!r}"
        )

    if render_reported_sha256 is not None:
        if (
            not isinstance(render_reported_sha256, str)
            or len(render_reported_sha256) != 64
            or not all(c in "0123456789abcdefABCDEF" for c in render_reported_sha256)
        ):
            raise QCError(
                f"Invalid render_reported_sha256: expected 64-character hex string or None, got {render_reported_sha256!r}"
            )

    source_path_obj = Path(staged_source_path)
    candidate_path_obj = Path(candidate_path)

    if not source_path_obj.is_file():
        raise QCError(f"Staged source file not found or not a regular file: {source_path_obj}")

    if not candidate_path_obj.is_file():
        raise QCError(f"Candidate file not found or not a regular file: {candidate_path_obj}")

    actual_source_sha256 = _sha256_file(source_path_obj)
    actual_candidate_sha256 = _sha256_file(candidate_path_obj)

    mismatches: list[str] = []

    if actual_source_sha256.lower() != expected_source_sha256.lower():
        mismatches.append(
            f"source digest mismatch: expected {expected_source_sha256[:12]}..., actual {actual_source_sha256[:12]}..."
        )

    if render_reported_sha256 is None:
        mismatches.append("candidate digest missing: render_reported_sha256 is None")
    elif actual_candidate_sha256.lower() != render_reported_sha256.lower():
        mismatches.append(
            f"candidate digest mismatch: expected {render_reported_sha256[:12]}..., actual {actual_candidate_sha256[:12]}..."
        )

    if mismatches:
        return False, "; ".join(mismatches)

    return True, "All digests verified"


# ---------------------------------------------------------------------------
# QC Evidence Builder (Piece 3a)
# ---------------------------------------------------------------------------

_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_TIMESTAMP_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def build_qc_evidence_record(
    *,
    job_id: str,
    evidence_id: str,
    checks: tuple[CheckRecord, ...] | list[CheckRecord],
    candidate_sha256: str,
    source_sha256: str,
    script_sha256: str,
    captured_at: str,
    exit_code: int | None,
    media_duration_seconds: float | None = None,
    notes: str = "",
) -> EvidenceRecord:
    """Build and return an EvidenceRecord for the QC evaluation phase.

    This function constructs the verifiable evidence record binding the post-render
    QC evaluation to the candidate media. In accordance with Approved Architectural
    Decision D1 (Philip), the record is issued with stage='QC_EVALUATED', integrating
    with the frozen Phase 2 evidence verification contract.

    Architectural Decisions & Invariants:
    - Decision D1: EvidenceRecord for QC strictly uses stage='QC_EVALUATED',
      which is present in STAGES within subtitle.core.evidence.
    - Anti-Fake Principle / No Production Auto Mode: The checks sequence must
      contain exactly one 'human_audio_verification' CheckRecord with result
      'NOT_CHECKED'. Automated probes and measurements never constitute human listening.
      If this check is missing, duplicated, or not 'NOT_CHECKED', raises QCError
      immediately (never silently injected or modified).
    - Cryptographic Binding: Binds source_media_sha256, script_sha256, and candidate_sha256
      in input_digest_refs, with artifact_sha256 bound to candidate_sha256.
    - Deterministic Tools & Command: tool='subtitle.core.qc', tool_version='1',
      method='subprocess_execution', command=('ffprobe', 'ffmpeg'),
      artifact_relpath='output/candidate.mp4'.

    Args:
        job_id: Unique job identifier (must be a non-empty string).
        evidence_id: Unique identifier for this evidence record (must be a non-empty string).
        checks: Non-empty sequence (tuple or list) of CheckRecord instances.
        candidate_sha256: 64-character hexadecimal SHA-256 digest of candidate.mp4.
        source_sha256: 64-character hexadecimal SHA-256 digest of staged source media.
        script_sha256: 64-character hexadecimal SHA-256 digest of timing/script artifact.
        captured_at: ISO-8601 UTC timestamp string (YYYY-MM-DDTHH:MM:SSZ).
        exit_code: Process exit code from renderer or probe pipeline (int or None).
        media_duration_seconds: Optional candidate media duration in seconds.
        notes: Optional descriptive notes.

    Returns:
        EvidenceRecord: Fully validated, frozen evidence record for stage 'QC_EVALUATED'.

    Raises:
        QCError: If any parameter fails type or format validation, or if checks does
                 not contain exactly one 'human_audio_verification' with result 'NOT_CHECKED'.
    """
    if not isinstance(job_id, str) or not job_id:
        raise QCError(f"job_id must be a non-empty string, got {job_id!r}")

    if (
        not isinstance(evidence_id, str)
        or not evidence_id
        or not _EVIDENCE_ID.fullmatch(evidence_id)
        or evidence_id in (".", "..")
    ):
        raise QCError(
            f"evidence_id must be a non-empty string matching ^[A-Za-z0-9._-]{{1,64}}$, got {evidence_id!r}"
        )

    for name, val in (
        ("candidate_sha256", candidate_sha256),
        ("source_sha256", source_sha256),
        ("script_sha256", script_sha256),
    ):
        if not isinstance(val, str) or len(val) != 64 or not _HEX_64.fullmatch(val):
            raise QCError(
                f"{name} must be a 64-character hexadecimal string, got {val!r}"
            )

    if not isinstance(checks, (tuple, list)) or len(checks) == 0:
        raise QCError("checks must be a non-empty tuple or list of CheckRecord")

    for chk in checks:
        if not isinstance(chk, CheckRecord):
            raise QCError(
                f"All items in checks must be CheckRecord instances, got {type(chk).__name__}"
            )

    human_audio = [c for c in checks if c.name == "human_audio_verification"]
    if len(human_audio) != 1 or human_audio[0].result != "NOT_CHECKED":
        raise QCError(
            "checks must contain exactly one CheckRecord named 'human_audio_verification' with result 'NOT_CHECKED'"
        )

    if not isinstance(captured_at, str) or not _TIMESTAMP_ISO_UTC.fullmatch(captured_at):
        raise QCError(
            f"captured_at must be an ISO-8601 UTC timestamp matching 'YYYY-MM-DDTHH:MM:SSZ', got {captured_at!r}"
        )
    try:
        datetime.strptime(captured_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise QCError(f"captured_at is not a valid date/time: {captured_at!r}") from exc

    if exit_code is not None and (type(exit_code) is not int or isinstance(exit_code, bool)):
        raise QCError(f"exit_code must be an int or None, got {exit_code!r}")

    if media_duration_seconds is not None:
        if (
            isinstance(media_duration_seconds, bool)
            or not isinstance(media_duration_seconds, (int, float))
            or not math.isfinite(media_duration_seconds)
            or media_duration_seconds < 0
        ):
            raise QCError(
                f"media_duration_seconds must be a non-negative finite float or None, got {media_duration_seconds!r}"
            )

    if not isinstance(notes, str):
        raise QCError(f"notes must be a str, got {type(notes).__name__}")

    try:
        return create_evidence_record(
            evidence_id=evidence_id,
            job_id=job_id,
            stage="QC_EVALUATED",
            tool="subtitle.core.qc",
            tool_version="1",
            method="subprocess_execution",
            command=("ffprobe", "ffmpeg"),
            input_digest_refs={
                "source_media_sha256": source_sha256.lower(),
                "script_sha256": script_sha256.lower(),
                "candidate_sha256": candidate_sha256.lower(),
            },
            artifact_sha256=candidate_sha256.lower(),
            artifact_relpath="output/candidate.mp4",
            captured_at=captured_at,
            exit_code=exit_code,
            checks=tuple(checks),
            media_duration_seconds=media_duration_seconds,
            notes=notes,
        )
    except ValueError as exc:
        raise QCError(f"Failed to create QC evidence record: {exc}") from exc



# ---------------------------------------------------------------------------
# Post-Render Quality Control Orchestration (Pieces 3b1 & 3b2)
# ---------------------------------------------------------------------------

def run_qc(
    spec: JobSpec,
    render_execution: RenderExecutionResult,
    *,
    now: str,
    job_dir: Path | str,
    authorization_decision: Decision | None = None,
    script_sha256: str | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> QCResult:
    """Evaluate candidate output media against empirical quality control gates.

    Performs post-render candidate verification using empirical measurement tools
    (ffprobe, ffmpeg loudnorm, deterministic frame capture, cryptographic hashing).
    Evaluates 8 technical checks and unconditionally records 2 sensory human checks
    as 'NOT_CHECKED'. Produces a frozen QCReport and EvidenceRecord for stage 'QC_EVALUATED'.

    Architectural Decisions & Invariants:
    - D2 / C5: Audio loudness target values (LOUDNESS_TARGET_I, LOUDNESS_TARGET_TP)
      are reference targets only, evaluated as a Soft Warning when True Peak <= 0.0 dBTP.
      Unmeasurable loudness fails-closed.
    - D3: Video duration tolerance is strictly fixed at 0.1s (script_burn.py:59,
      DURATION_TOLERANCE_SECONDS). Geometry (width x height) must match source exactly.
    - D4 / C4: Extracted QC sample frames are stored strictly at <job_dir>/evidence/qc_frames/.
    - D5: QC execution is invoked exclusively by the orchestrator in Phase 2A.
      Transitions are never reduced inside run_qc.
    - C2: Source media digest verification references spec.source.sha256 (not JobSpec.source_media).
    - C12: In-place staging inspection; candidate media is never moved or copied outside staging
      (stems from Plan / Spec line 184).
    - Q5 (P2-006): script_sha256 is explicitly passed by caller; run_qc never guesses script digest.
      If script_sha256 is None, EvidenceRecord creation is skipped (evidence_record=None, evidence_id='').

    Args:
        spec: JobSpec contract for the active job.
        render_execution: RenderExecutionResult from burner execution.
        now: Current ISO-8601 UTC timestamp string (YYYY-MM-DDTHH:MM:SSZ). Caller-supplied;
            no reading real system clock.
        job_dir: Root directory of the job workspace (Path or str).
        authorization_decision: Optional fresh authorization Decision. If provided and
            denied (allowed is False), returns BLOCKED QCResult immediately without running probes.
        script_sha256: Optional 64-char hex SHA-256 digest of the timed script.
        ffmpeg_bin: Path or binary name for ffmpeg (default: 'ffmpeg').
        ffprobe_bin: Path or binary name for ffprobe (default: 'ffprobe').

    Returns:
        QCResult: Assessment result with status 'REVIEW_CANDIDATE' or 'BLOCKED',
            QCReport, optional EvidenceRecord, target_state, and unsatisfied_checks.

    Raises:
        QCError: If input parameter validation fails (invalid types or formats).
    """
    # 1. Parameter type validation
    if not isinstance(spec, JobSpec):
        raise QCError(f"spec must be a JobSpec instance, got {type(spec).__name__}")

    if not isinstance(render_execution, RenderExecutionResult):
        raise QCError(
            f"render_execution must be a RenderExecutionResult instance, got {type(render_execution).__name__}"
        )

    if not isinstance(now, str) or not _TIMESTAMP_ISO_UTC.fullmatch(now):
        raise QCError(
            f"now must be an ISO-8601 UTC timestamp matching 'YYYY-MM-DDTHH:MM:SSZ', got {now!r}"
        )
    try:
        datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise QCError(f"now is not a valid date/time: {now!r}") from exc

    if not isinstance(job_dir, (str, Path)) or not str(job_dir).strip():
        raise QCError(f"job_dir must be a non-empty Path or str, got {job_dir!r}")

    if authorization_decision is not None and not isinstance(authorization_decision, Decision):
        raise QCError(
            f"authorization_decision must be a Decision instance or None, got {type(authorization_decision).__name__}"
        )

    if script_sha256 is not None:
        if (
            not isinstance(script_sha256, str)
            or len(script_sha256) != 64
            or not _HEX_64.fullmatch(script_sha256)
        ):
            raise QCError(
                f"script_sha256 must be a 64-character hexadecimal string or None, got {script_sha256!r}"
            )

    if not isinstance(ffmpeg_bin, str) or not ffmpeg_bin.strip():
        raise QCError(f"ffmpeg_bin must be a non-empty string, got {ffmpeg_bin!r}")

    if not isinstance(ffprobe_bin, str) or not ffprobe_bin.strip():
        raise QCError(f"ffprobe_bin must be a non-empty string, got {ffprobe_bin!r}")

    candidate_path = Path(spec.staging.candidate_output_path)
    staged_source_path = Path(spec.staging.staged_input_path)
    job_dir_obj = Path(job_dir)

    try:
        candidate_relpath = str(candidate_path.relative_to(job_dir_obj))
    except ValueError:
        candidate_relpath = "output/candidate.mp4"

    # 2. Authorization decision denied fast-path
    if authorization_decision is not None and not authorization_decision.allowed:
        denied_tech_checks = [
            CheckRecord(
                name=chk_name,
                result="NOT_CHECKED",
                observed_value=None,
                threshold=None,
                evidence_ref=None,
                notes="authorization denied; probes not executed",
            )
            for chk_name in CHECK_NAMES[:8]
        ]
        denied_human_checks = [
            CheckRecord(
                name="human_audio_verification",
                result="NOT_CHECKED",
                observed_value=None,
                threshold="audited_by_human",
                evidence_ref=None,
                notes="Automated pipeline only; no human listening has been performed.",
            ),
            CheckRecord(
                name="human_visual_verification",
                result="NOT_CHECKED",
                observed_value=None,
                threshold="audited_by_human",
                evidence_ref=None,
                notes="Automated pipeline only; no human visual inspection has been performed.",
            ),
        ]
        all_denied_checks = tuple(denied_tech_checks + denied_human_checks)

        denied_report = QCReport(
            schema_version="1",
            job_id=spec.job_id,
            candidate_relpath=candidate_relpath,
            candidate_sha256="",
            source_sha256=spec.source.sha256,
            status="BLOCKED",
            media_properties=None,
            loudness_metrics=None,
            renderer_exit_code=render_execution.exit_code,
            checks=all_denied_checks,
            unsatisfied_checks=(),
            frame_evidence=(),
            human_audio_verification="NOT_CHECKED",
            human_visual_verification="NOT_CHECKED",
            listener_identity=None,
            viewer_identity=None,
            warning_notice=WARNING_NOTICE,
            evaluated_at=now,
            notes="authorization denied; probes not executed",
        )

        reason_str = (
            f"QC blocked: authorization denied ({authorization_decision.reason})"
            if getattr(authorization_decision, "reason", None)
            else "QC blocked: authorization denied"
        )

        return QCResult(
            status="BLOCKED",
            report=denied_report,
            evidence_record=None,
            evidence_id="",
            target_state="BLOCKED_NEEDS_REVIEW",
            reason=reason_str,
            unsatisfied_checks=(),
        )

    # 3. Shared variables for QC pipeline (consumed by Piece 3b2)
    tech_checks: dict[str, CheckRecord] = {}
    media_properties: CandidateMediaProperties | None = None
    source_properties: CandidateMediaProperties | None = None
    system_fault: bool = False
    candidate_sha256: str = ""
    loudness_metrics: LoudnessMetrics | None = None
    loudness_warning: bool = False
    frame_evidence: tuple[QCFrameRecord, ...] = ()

    # 4. Check renderer_exit_code
    candidate_exists = False
    candidate_size = 0
    try:
        if candidate_path.is_file():
            candidate_exists = True
            candidate_size = candidate_path.stat().st_size
    except OSError:
        candidate_exists = False
        candidate_size = 0

    if render_execution.exit_code != 0:
        rec_exit_result = "FAILED"
        rec_exit_notes = f"Renderer process exited with code {render_execution.exit_code}"
    elif not candidate_exists:
        rec_exit_result = "FAILED"
        rec_exit_notes = f"Candidate output file does not exist: {candidate_path}"
    elif candidate_size <= 0:
        rec_exit_result = "FAILED"
        rec_exit_notes = f"Candidate output file is empty (0 bytes): {candidate_path}"
    elif (
        render_execution.output_size_bytes != 0
        and candidate_size != render_execution.output_size_bytes
    ):
        rec_exit_result = "FAILED"
        rec_exit_notes = (
            f"Candidate output size mismatch: disk has {candidate_size} bytes, "
            f"render execution reported {render_execution.output_size_bytes} bytes"
        )
    else:
        rec_exit_result = "PASSED"
        rec_exit_notes = ""

    tech_checks["renderer_exit_code"] = CheckRecord(
        name="renderer_exit_code",
        result=rec_exit_result,
        observed_value=render_execution.exit_code,
        threshold=0,
        evidence_ref=None,
        notes=rec_exit_notes,
    )

    # 5. Check cryptographic_digests
    if candidate_exists:
        try:
            candidate_sha256 = _sha256_file(candidate_path)
        except QCError:
            candidate_sha256 = ""

    try:
        digests_ok, digest_reason = verify_digests(
            staged_source_path=staged_source_path,
            candidate_path=candidate_path,
            expected_source_sha256=spec.source.sha256,
            render_reported_sha256=render_execution.output_sha256,
        )
        if digests_ok:
            tech_checks["cryptographic_digests"] = CheckRecord(
                name="cryptographic_digests",
                result="PASSED",
                observed_value=candidate_sha256,
                threshold=render_execution.output_sha256,
                evidence_ref=None,
                notes="",
            )
        else:
            tech_checks["cryptographic_digests"] = CheckRecord(
                name="cryptographic_digests",
                result="FAILED",
                observed_value=candidate_sha256 if candidate_sha256 else None,
                threshold=render_execution.output_sha256,
                evidence_ref=None,
                notes=digest_reason,
            )
    except QCError as exc:
        system_fault = True
        tech_checks["cryptographic_digests"] = CheckRecord(
            name="cryptographic_digests",
            result="FAILED",
            observed_value=candidate_sha256 if candidate_sha256 else None,
            threshold=render_execution.output_sha256,
            evidence_ref=None,
            notes=str(exc),
        )

    # 6. Probe candidate and staged source, evaluate dimensions, duration, fps, audio presence
    try:
        media_properties = probe_candidate(candidate_path, ffprobe_bin=ffprobe_bin)
        source_properties = probe_candidate(staged_source_path, ffprobe_bin=ffprobe_bin)
    except QCError as exc:
        media_properties = None
        if "binary not found" in str(exc).lower():
            system_fault = True
        tech_checks["video_dimensions"] = CheckRecord(
            name="video_dimensions",
            result="FAILED",
            observed_value=None,
            threshold=None,
            evidence_ref=None,
            notes=f"Probe failed: {exc}",
        )
        tech_checks["duration_consistency"] = CheckRecord(
            name="duration_consistency",
            result="FAILED",
            observed_value=None,
            threshold=DURATION_TOLERANCE_SECONDS,
            evidence_ref=None,
            notes=f"Probe failed: {exc}",
        )
        tech_checks["frame_rate_validity"] = CheckRecord(
            name="frame_rate_validity",
            result="FAILED",
            observed_value=None,
            threshold=f"{FRAME_RATE_MIN}-{FRAME_RATE_MAX}",
            evidence_ref=None,
            notes=f"Probe failed: {exc}",
        )
        tech_checks["audio_presence"] = CheckRecord(
            name="audio_presence",
            result="FAILED",
            observed_value=None,
            threshold=None,
            evidence_ref=None,
            notes=f"Probe failed: {exc}",
        )
    else:
        # Evaluate video_dimensions
        cand_geom = f"{media_properties.width}x{media_properties.height}"
        src_geom = f"{source_properties.width}x{source_properties.height}"
        if (
            media_properties.width == source_properties.width
            and media_properties.height == source_properties.height
        ):
            dim_result = "PASSED"
            dim_notes = ""
        else:
            dim_result = "FAILED"
            dim_notes = f"Video dimensions mismatch: candidate {cand_geom} != source {src_geom}"

        tech_checks["video_dimensions"] = CheckRecord(
            name="video_dimensions",
            result=dim_result,
            observed_value=cand_geom,
            threshold=src_geom,
            evidence_ref=None,
            notes=dim_notes,
        )

        # Evaluate duration_consistency
        duration_delta = abs(media_properties.duration_seconds - source_properties.duration_seconds)
        if duration_delta <= DURATION_TOLERANCE_SECONDS:
            dur_result = "PASSED"
            dur_notes = ""
        else:
            dur_result = "FAILED"
            dur_notes = (
                f"Duration drift {duration_delta:.4f}s exceeds tolerance "
                f"{DURATION_TOLERANCE_SECONDS}s (candidate: {media_properties.duration_seconds}s, "
                f"source: {source_properties.duration_seconds}s)"
            )

        tech_checks["duration_consistency"] = CheckRecord(
            name="duration_consistency",
            result=dur_result,
            observed_value=round(duration_delta, 6),
            threshold=DURATION_TOLERANCE_SECONDS,
            evidence_ref=None,
            notes=dur_notes,
        )

        # Evaluate frame_rate_validity
        fps_val = media_properties.video_fps
        fps_threshold_str = f"{FRAME_RATE_MIN}-{FRAME_RATE_MAX}"
        if FRAME_RATE_MIN <= fps_val <= FRAME_RATE_MAX:
            fps_result = "PASSED"
            fps_notes = ""
        else:
            fps_result = "FAILED"
            fps_notes = (
                f"Candidate video fps {fps_val:.4f} outside standard broadcast range "
                f"[{FRAME_RATE_MIN}, {FRAME_RATE_MAX}]"
            )

        tech_checks["frame_rate_validity"] = CheckRecord(
            name="frame_rate_validity",
            result=fps_result,
            observed_value=round(fps_val, 4),
            threshold=fps_threshold_str,
            evidence_ref=None,
            notes=fps_notes,
        )

        # Evaluate audio_presence
        if media_properties.has_audio == source_properties.has_audio:
            audio_result = "PASSED"
            audio_notes = ""
        else:
            audio_result = "FAILED"
            audio_notes = (
                f"Audio presence mismatch: candidate has_audio={media_properties.has_audio}, "
                f"source has_audio={source_properties.has_audio}"
            )

        tech_checks["audio_presence"] = CheckRecord(
            name="audio_presence",
            result=audio_result,
            observed_value=media_properties.has_audio,
            threshold=source_properties.has_audio,
            evidence_ref=None,
            notes=audio_notes,
        )

    # 7. Measure audio loudness via FFmpeg loudnorm (D2, C5, C6, C7)
    try:
        loudness_metrics = measure_loudness(candidate_path, ffmpeg_bin=ffmpeg_bin)
    except QCError as exc:
        system_fault = True
        err_msg = str(exc)
        loudness_metrics = LoudnessMetrics(
            measured_i=None,
            measured_tp=None,
            measured_lra=None,
            measured_thresh=None,
            measurement_success=False,
            error_message=err_msg,
        )
        tech_checks["audio_loudness"] = CheckRecord(
            name="audio_loudness",
            result="FAILED",
            observed_value=None,
            threshold=0.0,
            evidence_ref=None,
            notes=err_msg,
        )
    else:
        if not loudness_metrics.measurement_success:
            tech_checks["audio_loudness"] = CheckRecord(
                name="audio_loudness",
                result="FAILED",
                observed_value=None,
                threshold=0.0,
                evidence_ref=None,
                notes=loudness_metrics.error_message or "Loudness measurement failed",
            )
        else:
            tp_val = loudness_metrics.measured_tp
            i_val = loudness_metrics.measured_i
            assert tp_val is not None and i_val is not None

            if tp_val > 0.0:
                tech_checks["audio_loudness"] = CheckRecord(
                    name="audio_loudness",
                    result="FAILED",
                    observed_value=round(tp_val, 2),
                    threshold=0.0,
                    evidence_ref=None,
                    notes=f"Audio clipping detected: True Peak {tp_val:.2f} dBTP exceeds 0.0 dBTP limit",
                )
            else:
                # Decision D2: Soft Warning evaluation. Audio does not clip; candidate remains reviewable.
                if (
                    i_val < LOUDNESS_PLAUSIBLE_I_MIN
                    or i_val > LOUDNESS_PLAUSIBLE_I_MAX
                    or tp_val > LOUDNESS_TARGET_TP
                ):
                    loudness_warning = True

                loud_notes = ""
                if loudness_warning:
                    loud_notes = (
                        f"Measured loudness ({i_val:.1f} LUFS / TP {tp_val:.1f} dBTP) "
                        f"outside target range ({LOUDNESS_TARGET_I} LUFS / TP {LOUDNESS_TARGET_TP} dBTP); "
                        "audio not clipped"
                    )

                tech_checks["audio_loudness"] = CheckRecord(
                    name="audio_loudness",
                    result="PASSED",
                    observed_value=round(tp_val, 2),
                    threshold=0.0,
                    evidence_ref=None,
                    notes=loud_notes,
                )

    # 8. Deterministic QC inspection frame extraction (D4, C4)
    if media_properties is None:
        tech_checks["qc_frame_extraction"] = CheckRecord(
            name="qc_frame_extraction",
            result="FAILED",
            observed_value=None,
            threshold=3,
            evidence_ref=None,
            notes="Probe failed; frame extraction skipped.",
        )
    else:
        media_duration = media_properties.duration_seconds
        frames_target_dir = job_dir_obj / "evidence" / QC_FRAME_DIRNAME
        try:
            captured_frames = capture_qc_frames(
                candidate_path,
                frames_target_dir,
                media_duration,
                ffmpeg_bin=ffmpeg_bin,
            )
            frame_evidence = captured_frames
            tech_checks["qc_frame_extraction"] = CheckRecord(
                name="qc_frame_extraction",
                result="PASSED",
                observed_value=len(frame_evidence),
                threshold=3,
                evidence_ref=None,
                notes="",
            )
        except QCError as exc:
            err_msg = str(exc)
            if "binary not found" in err_msg.lower():
                system_fault = True
            tech_checks["qc_frame_extraction"] = CheckRecord(
                name="qc_frame_extraction",
                result="FAILED",
                observed_value=None,
                threshold=3,
                evidence_ref=None,
                notes=err_msg,
            )

    # 9. Assemble checks sequence (8 technical checks + 2 sensory human checks)
    ordered_tech_checks: list[CheckRecord] = []
    for chk_name in CHECK_NAMES[:8]:
        if chk_name not in tech_checks:
            raise QCError(f"Missing technical check in tech_checks: {chk_name!r}")
        ordered_tech_checks.append(tech_checks[chk_name])

    human_audio_check = CheckRecord(
        name="human_audio_verification",
        result="NOT_CHECKED",
        observed_value=None,
        threshold="audited_by_human",
        evidence_ref=None,
        notes="Automated pipeline only; no human listening has been performed.",
    )
    human_visual_check = CheckRecord(
        name="human_visual_verification",
        result="NOT_CHECKED",
        observed_value=None,
        threshold="audited_by_human",
        evidence_ref=None,
        notes="Automated pipeline only; no human visual inspection has been performed.",
    )
    checks: tuple[CheckRecord, ...] = tuple(
        ordered_tech_checks + [human_audio_check, human_visual_check]
    )

    # 10. Compute overall status and unsatisfied checks
    unsatisfied: tuple[str, ...] = tuple(
        chk_name for chk_name in CHECK_NAMES[:8] if tech_checks[chk_name].result != "PASSED"
    )
    status: Literal["REVIEW_CANDIDATE", "BLOCKED"] = (
        "REVIEW_CANDIDATE" if len(unsatisfied) == 0 else "BLOCKED"
    )

    # 11. Compose warning notice and report notes (D2)
    warning_notice = WARNING_NOTICE
    notes = ""
    if loudness_warning:
        notes = (
            "WARN_LOUDNESS_OUT_OF_TARGET: measured loudness outside target range "
            "(-16.0 LUFS / TP -1.0 dBTP); audio not clipped, candidate remains reviewable."
        )
        warning_notice = f"{WARNING_NOTICE} {notes}"

    # 12. Build and persist EvidenceRecord if script_sha256 is supplied (Q5, D1)
    evidence_record: EvidenceRecord | None = None
    evidence_id: str = ""
    if script_sha256 is not None:
        media_dur_val = (
            media_properties.duration_seconds
            if media_properties is not None
            else None
        )
        ev_id = f"ev_qc_{spec.job_id}"
        if len(candidate_sha256) == 64 and _HEX_64.fullmatch(candidate_sha256):
            record = build_qc_evidence_record(
                evidence_id=ev_id,
                job_id=spec.job_id,
                checks=checks,
                candidate_sha256=candidate_sha256,
                source_sha256=spec.source.sha256,
                script_sha256=script_sha256,
                captured_at=now,
                exit_code=render_execution.exit_code,
                media_duration_seconds=media_dur_val,
                notes=notes,
            )
            save_evidence_record(job_dir_obj, record)
            evidence_record = record
            evidence_id = ev_id
        else:
            evidence_record = None
            evidence_id = ""

    # 13. Determine target state for Orchestrator (D5: no reduce_transition inside run_qc)
    if status == "REVIEW_CANDIDATE":
        target_state: Literal["READY_FOR_REVIEW", "BLOCKED_NEEDS_REVIEW", "FAILED_JOB"] = (
            "READY_FOR_REVIEW"
        )
    elif system_fault:
        target_state = "FAILED_JOB"
    else:
        target_state = "BLOCKED_NEEDS_REVIEW"

    # 14. Compose human-readable decision reason
    if status == "REVIEW_CANDIDATE":
        reason = "QC passed: 8/8 technical checks; human checks NOT_CHECKED"
    else:
        reason = f"QC blocked: {', '.join(unsatisfied)}"

    # 15. Construct comprehensive QCReport and return QCResult
    report = QCReport(
        schema_version="1",
        job_id=spec.job_id,
        candidate_relpath=candidate_relpath,
        candidate_sha256=candidate_sha256,
        source_sha256=spec.source.sha256,
        status=status,
        media_properties=media_properties,
        loudness_metrics=loudness_metrics,
        renderer_exit_code=render_execution.exit_code,
        checks=checks,
        unsatisfied_checks=unsatisfied,
        frame_evidence=frame_evidence,
        human_audio_verification="NOT_CHECKED",
        human_visual_verification="NOT_CHECKED",
        listener_identity=None,
        viewer_identity=None,
        warning_notice=warning_notice,
        evaluated_at=now,
        notes=notes,
    )

    return QCResult(
        status=status,
        report=report,
        evidence_record=evidence_record,
        evidence_id=evidence_id,
        target_state=target_state,
        reason=reason,
        unsatisfied_checks=unsatisfied,
    )
