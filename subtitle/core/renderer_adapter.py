"""P2-006 renderer adapter seam between orchestrator and legacy subtitle burners.

This module acts as the sole adapter seam between the Phase 2 orchestrator and
the existing legacy subtitle burners (`subtitle/viralpop/scripts/burn_script_aligned.py`
and `subtitle/paper-chips/chip_burn.py`).

Design and security constraints:
1. Subprocess execution only: legacy burners must be invoked exclusively via
   `subprocess.run(argv_tuple, shell=False)`. Direct in-process importing or calling
   of burner modules is strictly forbidden to prevent `SystemExit` from crashing
   the orchestrator process, ensure process and memory isolation, enforce timeouts,
   and maintain verifiable audit evidence.
2. Untouched legacy files invariant: legacy burner files and `approval.json` must
   never be modified, deleted, or bypassed. This adapter acts strictly as an
   external caller.
3. Appendix C2 residual constraint:
   In `subtitle/paper-chips/chip_burn.py` line 246, `tempfile.mkdtemp(prefix='chipstrip-', dir='/tmp')`
   explicitly hardcodes `dir='/tmp'`. Because `dir=` is explicitly supplied, Python's
   tempfile module completely ignores the `TMPDIR` environment variable. Therefore,
   passing `TMPDIR` to `chip_burn.py` does NOT change where strip frames are stored;
   they continue to be written to `/tmp` (tmpfs 512MB). Passing `TMPDIR` does not
   solve this issue. This residual risk has been formally acknowledged and accepted
   in writing with an empirical verification boundary: experimental runs are strictly
   limited to short clips (<= 60 seconds), and actual peak `/tmp` disk usage must be
   measured during trial runs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

from subtitle.core.job import JobSpec


class RendererAdapterError(Exception):
    """Raised when renderer adapter preparation, command building, or execution fails."""


@dataclass(frozen=True)
class RenderExecutionResult:
    """Result of an isolated subtitle burner subprocess execution."""

    success: bool
    command: tuple[str, ...]
    exit_code: int
    output_path: str
    output_sha256: str | None
    output_size_bytes: int
    duration_seconds: float
    stdout: str
    stderr: str
    error_message: str | None


def _reject_constant(value: str) -> None:
    """Reject non-standard JSON constants (NaN, Infinity, -Infinity)."""
    raise ValueError(f"Invalid JSON constant: {value!r}")


def prepare_mfa_words_timeline(
    mfa_artifact_bytes: bytes,
    script_text: str,
) -> dict:
    """Parse MFA alignment artifact bytes into words timeline dict for legacy burners.

    Parses MFA artifact using the exact structure validated by check_mfa_provenance (Gate G2):
    JSON object with 'tiers' -> 'words' -> 'entries', rejecting STT forgery markers.
    Returns: {"words": [{"word": str, "start": float, "end": float}, ...]} sorted by start.

    Args:
        mfa_artifact_bytes: Raw bytes of the MFA JSON alignment artifact.
        script_text: Canonical script text associated with the job.

    Returns:
        dict: {"words": [{"word": str, "start": float, "end": float}, ...]}

    Raises:
        RendererAdapterError: If artifact bytes are invalid, unparseable, forged,
            or contain no valid word entries.
    """
    if type(mfa_artifact_bytes) is not bytes:
        raise RendererAdapterError("mfa_artifact_bytes must be bytes")
    if type(script_text) is not str:
        raise RendererAdapterError("script_text must be str")

    try:
        artifact = json.loads(
            mfa_artifact_bytes.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError) as exc:
        raise RendererAdapterError(f"MFA artifact is not valid JSON: {exc}") from exc

    if type(artifact) is not dict:
        raise RendererAdapterError("MFA artifact must be a JSON object")

    if "segments" in artifact:
        raise RendererAdapterError("STT forgery detected: 'segments' key present in MFA artifact")

    if "tiers" not in artifact or type(artifact["tiers"]) is not dict:
        raise RendererAdapterError("MFA artifact missing 'tiers'")

    tiers = artifact["tiers"]
    if "words" not in tiers or type(tiers["words"]) is not dict:
        raise RendererAdapterError("MFA artifact missing 'tiers.words'")

    words_tier = tiers["words"]
    if "entries" not in words_tier or type(words_tier["entries"]) is not list:
        raise RendererAdapterError("MFA artifact missing 'tiers.words.entries'")

    raw_entries = words_tier["entries"]
    for entry in raw_entries:
        if type(entry) is dict and any(k in entry for k in ("probability", "avg_logprob", "temperature")):
            raise RendererAdapterError("STT forgery detected: entry contains probability/avg_logprob/temperature")

    kept_entries: list[tuple[float, float, str]] = []
    for entry in raw_entries:
        if type(entry) not in (list, tuple) or len(entry) != 3:
            raise RendererAdapterError(f"MFA entry must be list or tuple of 3 items: {entry!r}")
        start, end, word = entry
        if type(word) is not str:
            raise RendererAdapterError(f"MFA entry word must be str: {word!r}")
        if word in ("<eps>", ""):
            continue
        if (
            type(start) is bool
            or type(start) not in (int, float)
            or not math.isfinite(start)
            or type(end) is bool
            or type(end) not in (int, float)
            or not math.isfinite(end)
        ):
            raise RendererAdapterError(f"MFA entry timing must be finite int/float: {entry!r}")
        start_f = float(start)
        end_f = float(end)
        if start_f < 0.0 or end_f < 0.0 or end_f < start_f:
            raise RendererAdapterError(f"MFA entry timing invalid: start={start_f}, end={end_f}")
        kept_entries.append((start_f, end_f, word))

    if not kept_entries:
        raise RendererAdapterError("empty entries: no valid words in MFA artifact")

    kept_entries.sort(key=lambda item: (item[0], item[1]))

    return {
        "words": [
            {
                "word": word,
                "start": start,
                "end": end,
            }
            for start, end, word in kept_entries
        ]
    }


def build_renderer_command(
    spec: JobSpec,
    *,
    script_file_path: Path | str,
    words_json_path: Path | str,
    fonts_dir: Path | str,
) -> tuple[str, ...]:
    """Build argv command tuple for invoking the legacy burner subprocess.

    Uses sys.executable and relative script paths resolved from the repository root
    (Path(__file__).resolve().parents[2]).

    - 'viralpop': subtitle/viralpop/scripts/burn_script_aligned.py
      with --video, --script, --fonts, --out, --words-json, --profile vo
    - 'paper-chips': subtitle/paper-chips/chip_burn.py
      with --video, --script, --stt, --out, --fps 30, --crf 18 (no --fonts)

    Args:
        spec: JobSpec containing staging paths and style configuration.
        script_file_path: Path to the staged script file.
        words_json_path: Path to the cached MFA words timeline JSON file.
        fonts_dir: Path to directory containing required subtitle fonts.

    Returns:
        tuple[str, ...]: Command arguments tuple suitable for subprocess.run(shell=False).

    Raises:
        RendererAdapterError: If spec or arguments are invalid, or if style_id is unsupported.
    """
    if not isinstance(spec, JobSpec):
        raise RendererAdapterError(f"spec must be JobSpec, got {type(spec).__name__}")

    script_path_str = str(script_file_path).strip() if isinstance(script_file_path, (str, Path)) else ""
    if not script_path_str:
        raise RendererAdapterError("script_file_path must be a non-empty str or Path")

    words_path_str = str(words_json_path).strip() if isinstance(words_json_path, (str, Path)) else ""
    if not words_path_str:
        raise RendererAdapterError("words_json_path must be a non-empty str or Path")

    fonts_dir_str = str(fonts_dir).strip() if isinstance(fonts_dir, (str, Path)) else ""
    if not fonts_dir_str:
        raise RendererAdapterError("fonts_dir must be a non-empty str or Path")

    if not spec.staging or not spec.staging.staged_input_path or not spec.staging.candidate_output_path:
        raise RendererAdapterError("spec.staging paths cannot be empty")

    repo_root = Path(__file__).resolve().parents[2]

    if spec.style_id == "viralpop":
        script_target = repo_root / "subtitle" / "viralpop" / "scripts" / "burn_script_aligned.py"
        command = (
            sys.executable,
            str(script_target),
            "--video",
            str(spec.staging.staged_input_path),
            "--script",
            script_path_str,
            "--fonts",
            fonts_dir_str,
            "--out",
            str(spec.staging.candidate_output_path),
            "--words-json",
            words_path_str,
            "--profile",
            "vo",
        )
    elif spec.style_id == "paper-chips":
        script_target = repo_root / "subtitle" / "paper-chips" / "chip_burn.py"
        command = (
            sys.executable,
            str(script_target),
            "--video",
            str(spec.staging.staged_input_path),
            "--script",
            script_path_str,
            "--stt",
            words_path_str,
            "--out",
            str(spec.staging.candidate_output_path),
            "--fps",
            "30",
            "--crf",
            "18",
        )
    else:
        raise RendererAdapterError(f"Unsupported style_id: {spec.style_id!r}")

    for arg in command:
        if not isinstance(arg, str) or not arg:
            raise RendererAdapterError(f"Generated command contains empty or non-string argument: {arg!r}")

    return tuple(command)


def execute_renderer(
    command: tuple[str, ...],
    *,
    expected_output_path: Path | str,
    timeout_seconds: int = 600,
    env: dict[str, str] | None = None,
) -> RenderExecutionResult:
    """Execute the renderer command in an isolated subprocess and verify output.

    Invokes the command via subprocess.run(shell=False) with output capture and timeout.
    Monitors execution duration using time.monotonic().

    - On timeout (subprocess.TimeoutExpired): returns RenderExecutionResult with
      success=False, exit_code=-1, error_message='renderer timed out after <N>s',
      duration_seconds=<elapsed>, stdout='', stderr=''.
    - On OS error (OSError): returns RenderExecutionResult with success=False,
      exit_code=-1, error_message=str(exc), duration_seconds=<elapsed>,
      stdout='', stderr=''.
    - On process completion:
      - Truncates stdout and stderr to at most 4000 characters each.
      - If exit_code == 0: verifies that expected_output_path exists and is non-empty (>0 bytes).
        If missing or empty, returns success=False with error_message='missing or empty candidate output'.
        If valid, computes SHA-256 (reading in 64KB chunks) and returns success=True.
      - If exit_code != 0: returns success=False with output_sha256=None,
        actual output size if file exists (or 0), and error_message set to stderr
        truncated to at most 500 characters.

    Args:
        command: Argument tuple of non-empty strings.
        expected_output_path: Path or str pointing to the expected output video file.
        timeout_seconds: Maximum allowed execution time in seconds (default: 600).
        env: Optional environment variables dictionary for subprocess execution.

    Returns:
        RenderExecutionResult: Dataclass recording execution status, output digests,
            and captured process outputs.

    Raises:
        RendererAdapterError: If input arguments fail validation.
    """
    if not isinstance(command, tuple) or not command:
        raise RendererAdapterError("command must be a non-empty tuple of str")
    for arg in command:
        if not isinstance(arg, str) or not arg:
            raise RendererAdapterError(f"command contains empty or non-string argument: {arg!r}")

    if not isinstance(expected_output_path, (str, Path)):
        raise RendererAdapterError("expected_output_path must be a non-empty str or Path")
    expected_path_str = str(expected_output_path).strip()
    if not expected_path_str:
        raise RendererAdapterError("expected_output_path must be a non-empty str or Path")
    out_path = Path(expected_output_path)

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise RendererAdapterError("timeout_seconds must be a positive int")

    start_time = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        duration = float(time.monotonic() - start_time)
        return RenderExecutionResult(
            success=False,
            command=command,
            exit_code=-1,
            output_path=str(out_path),
            output_sha256=None,
            output_size_bytes=0,
            duration_seconds=duration,
            stdout="",
            stderr="",
            error_message=f"renderer timed out after {timeout_seconds}s",
        )
    except OSError as exc:
        duration = float(time.monotonic() - start_time)
        return RenderExecutionResult(
            success=False,
            command=command,
            exit_code=-1,
            output_path=str(out_path),
            output_sha256=None,
            output_size_bytes=0,
            duration_seconds=duration,
            stdout="",
            stderr="",
            error_message=str(exc),
        )

    duration = float(time.monotonic() - start_time)
    stdout_truncated = (proc.stdout or "")[:4000]
    stderr_truncated = (proc.stderr or "")[:4000]

    if proc.returncode == 0:
        output_size = out_path.stat().st_size if out_path.is_file() else 0
        if not out_path.is_file() or output_size == 0:
            return RenderExecutionResult(
                success=False,
                command=command,
                exit_code=0,
                output_path=str(out_path),
                output_sha256=None,
                output_size_bytes=output_size,
                duration_seconds=duration,
                stdout=stdout_truncated,
                stderr=stderr_truncated,
                error_message="missing or empty candidate output",
            )

        hasher = hashlib.sha256()
        with open(out_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        digest = hasher.hexdigest()

        return RenderExecutionResult(
            success=True,
            command=command,
            exit_code=0,
            output_path=str(out_path),
            output_sha256=digest,
            output_size_bytes=output_size,
            duration_seconds=duration,
            stdout=stdout_truncated,
            stderr=stderr_truncated,
            error_message=None,
        )

    output_size = out_path.stat().st_size if out_path.is_file() else 0
    return RenderExecutionResult(
        success=False,
        command=command,
        exit_code=proc.returncode,
        output_path=str(out_path),
        output_sha256=None,
        output_size_bytes=output_size,
        duration_seconds=duration,
        stdout=stdout_truncated,
        stderr=stderr_truncated,
        error_message=(proc.stderr or "")[:500],
    )


def render_one_clip(
    spec: JobSpec,
    *,
    script_text: str,
    mfa_artifact_bytes: bytes,
    fonts_dir: Path | str,
    timeout_seconds: int = 600,
) -> RenderExecutionResult:
    """Execute guarded single clip render through legacy burner subprocess.

    1. Validates input parameters (JobSpec, script_text, mfa_artifact_bytes, fonts_dir).
    2. Writes staged_script.txt (script_text UTF-8) into spec.staging.job_work_dir.
    3. Prepares MFA words timeline via prepare_mfa_words_timeline and writes
       mfa_words.json into spec.staging.job_work_dir with canonical formatting.
    4. Builds subprocess command tuple via build_renderer_command.
    5. Configures execution environment:
       - 'paper-chips': creates isolated TMPDIR under job_work_dir/tmp.
       - 'viralpop': env is None.
    6. Invokes execute_renderer and returns the RenderExecutionResult directly.

    Note:
        Does not perform file aliasing checks (delegated to intake/orchestrator).
        Does not call verify() or any evidence gates.

    Args:
        spec: JobSpec defining job configuration, style, and staging paths.
        script_text: Approved script text as non-empty str.
        mfa_artifact_bytes: Raw MFA alignment JSON bytes.
        fonts_dir: Non-empty path or str to fonts directory.
        timeout_seconds: Maximum execution time in seconds (default: 600).

    Returns:
        RenderExecutionResult: Direct result from execute_renderer.

    Raises:
        RendererAdapterError: If inputs are invalid or staging operations fail.
    """
    if not isinstance(spec, JobSpec):
        raise RendererAdapterError(f"spec must be JobSpec, got {type(spec).__name__}")
    if not isinstance(script_text, str) or not script_text:
        raise RendererAdapterError("script_text must be a non-empty str")
    if not isinstance(mfa_artifact_bytes, bytes):
        raise RendererAdapterError(f"mfa_artifact_bytes must be bytes, got {type(mfa_artifact_bytes).__name__}")
    fonts_dir_str = str(fonts_dir).strip() if isinstance(fonts_dir, (str, Path)) else ""
    if not fonts_dir_str:
        raise RendererAdapterError("fonts_dir must be a non-empty str or Path")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise RendererAdapterError("timeout_seconds must be a positive int")

    if not spec.staging or not spec.staging.job_work_dir:
        raise RendererAdapterError("spec.staging.job_work_dir cannot be empty")
    if not spec.staging.candidate_output_path:
        raise RendererAdapterError("spec.staging.candidate_output_path cannot be empty")

    job_work_dir = Path(spec.staging.job_work_dir)
    job_work_dir.mkdir(parents=True, exist_ok=True)

    staged_script_path = job_work_dir / "staged_script.txt"
    staged_script_path.write_text(script_text, encoding="utf-8")

    words = prepare_mfa_words_timeline(mfa_artifact_bytes, script_text)
    words_json_path = job_work_dir / "mfa_words.json"
    words_json_path.write_text(
        json.dumps(words, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    command = build_renderer_command(
        spec,
        script_file_path=staged_script_path,
        words_json_path=words_json_path,
        fonts_dir=fonts_dir,
    )

    env: dict[str, str] | None = None
    if spec.style_id == "paper-chips":
        env = os.environ.copy()
        tmp_dir = job_work_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp_dir)
        # Appendix C2 residual constraint note:
        # In subtitle/paper-chips/chip_burn.py line 246, `dir='/tmp'` is explicitly hardcoded
        # in `tempfile.mkdtemp(prefix='chipstrip-', dir='/tmp')`. Because `dir=` is explicitly
        # provided, Python's tempfile module completely ignores the TMPDIR environment variable.
        # Therefore, TMPDIR does NOT change where strip frames are stored; they continue to be written
        # to /tmp (tmpfs 512MB). Passing TMPDIR does not solve this issue. This residual risk has
        # been formally acknowledged and accepted in writing. Actual peak /tmp usage must be measured
        # during trial runs.
    elif spec.style_id == "viralpop":
        env = None

    result = execute_renderer(
        command,
        expected_output_path=Path(spec.staging.candidate_output_path),
        timeout_seconds=timeout_seconds,
        env=env,
    )
    return result
