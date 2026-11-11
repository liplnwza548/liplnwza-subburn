"""Offline tests for P2-007 (Measured Quality Control and READY_FOR_REVIEW report).

Test Suite Baseline:
- Baseline before this ticket: 111 Phase 2 tests + 6 Foundation tests = 117 tests (per Plan Correction C1).
- All tests in this module use synthetic data fixtures exclusively (no real video files,
  no private data, no network calls, and no secret keys).
- All subprocess executions (ffprobe, ffmpeg loudnorm, frame extraction) are deterministically
  mocked via unittest.mock.patch('subtitle.core.qc.subprocess.run'), except for dedicated
  offline synthetic E2E integration tests.
- Invariants: Automated checks never constitute human listening or typography proofreading.
  Human sensory checks (human_audio_verification, human_visual_verification) are unconditionally
  recorded as NOT_CHECKED in Phase 2A.
"""

from __future__ import annotations

from dataclasses import fields
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

from subtitle.core.authorization import Decision
from subtitle.core.evidence import (
    EvidenceError,
    create_evidence_record,
)
from subtitle.core.job import (
    JobSpec,
    SourceSnapshot,
    StagingPaths,
    create_job_spec,
)
from subtitle.core.qc import (
    CandidateMediaProperties,
    CheckRecord,
    LoudnessMetrics,
    QCFrameRecord,
    QCReport,
    QCResult,
    WARNING_NOTICE,
    run_qc,
)
from subtitle.core.renderer_adapter import RenderExecutionResult
from subtitle.core.state import (
    JobCheckpoint,
    STATE_SKIP_FORBIDDEN,
    TERMINAL_STATE_LOCKED,
    reduce_transition,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Deterministic synthetic ISO-8601 UTC timestamp for testing.
NOW: str = "2026-11-10T12:00:00Z"

#: Synthetic 64-character SHA-256 digest representing timed script artifact
#: (explicitly synthetic for testing; not a real file digest).
SCRIPT_SHA256: str = "e" * 64


# ---------------------------------------------------------------------------
# Test Fixture Helpers
# ---------------------------------------------------------------------------

def make_spec(
    job_id: str,
    source_sha256: str,
    work_dir: Path | str,
    staged_source_path: Path | str,
    output_dir: Path | str,
    candidate_path: Path | str,
    style_id: str = "viralpop",
) -> JobSpec:
    """Create a canonical JobSpec instance using synthetic test parameters."""
    source_path_obj = Path(staged_source_path)
    try:
        source_size_bytes = source_path_obj.stat().st_size if source_path_obj.is_file() else 1024
    except OSError:
        source_size_bytes = 1024

    return create_job_spec(
        job_id=job_id,
        grant_id=f"grant-{job_id}",
        source=SourceSnapshot(
            provider="test",
            id="source-1",
            revision="r1",
            sha256=source_sha256,
            size_bytes=source_size_bytes,
            canonical_source_path="/synthetic/source.mp4",
        ),
        style_id=style_id,
        style_overrides={},
        allowed_actions=["intake_copy", "render_candidate"],
        allowed_processors=[],
        review_destination={"provider": "test", "id": "review-1", "owner_id": "philip"},
        retry_allowance=2,
        staging=StagingPaths(
            str(work_dir),
            str(staged_source_path),
            str(output_dir),
            str(candidate_path),
        ),
        created_at=NOW,
    )


def make_render_execution(
    candidate_path: Path | str,
    candidate_bytes: bytes,
    exit_code: int = 0,
) -> RenderExecutionResult:
    """Create a RenderExecutionResult instance with synthetic media execution values."""
    candidate_path_str = str(candidate_path)
    output_digest = hashlib.sha256(candidate_bytes).hexdigest()
    output_size = len(candidate_bytes)

    return RenderExecutionResult(
        success=(exit_code == 0),
        command=("ffmpeg",),
        exit_code=exit_code,
        output_path=candidate_path_str,
        output_sha256=output_digest,
        output_size_bytes=output_size,
        duration_seconds=10.0,
        stdout="",
        stderr="",
        error_message=None if exit_code == 0 else f"Process failed with exit code {exit_code}",
    )


def make_ffprobe_json(
    *,
    width: int = 1080,
    height: int = 1920,
    duration: float = 10.0,
    fps: str = "30/1",
    has_audio: bool = True,
    channels: int = 2,
    sample_rate: int = 44100,
) -> dict:
    """Construct a synthetic ffprobe JSON dictionary structure."""
    video_stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": width,
        "height": height,
        "r_frame_rate": fps,
    }
    streams = [video_stream]
    if has_audio:
        streams.append({
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": channels,
            "sample_rate": sample_rate,
        })

    return {
        "format": {
            "duration": str(duration),
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "size": "12345",
        },
        "streams": streams,
    }


def make_loudnorm_stderr(
    *,
    input_i: str = "-16.2",
    input_tp: str = "-1.1",
) -> str:
    """Construct a synthetic FFmpeg loudnorm stderr output string."""
    loudnorm_payload = json.dumps({
        "input_i": input_i,
        "input_tp": input_tp,
        "input_lra": "5.0",
        "input_thresh": "-34.0",
    })
    return (
        "[Parsed_loudnorm_0 @ 0x55a730] Normalization pass starting...\n"
        "[Parsed_loudnorm_0 @ 0x55a730] Progress status lines...\n"
        f"{loudnorm_payload}\n"
        "[ffmpeg] frame=300 fps=0.0 q=-0.0 Lsize=N/A time=00:00:10.00\n"
    )


def mock_probe_and_loudnorm(
    mock_run: Mock,
    *,
    ffprobe_data: dict | None = None,
    loudnorm_stderr: str | None = None,
) -> None:
    """Configure mock_run side_effect to respond to ffprobe, loudnorm, and frame extraction."""
    if ffprobe_data is None:
        ffprobe_data = make_ffprobe_json()
    if loudnorm_stderr is None:
        loudnorm_stderr = make_loudnorm_stderr()

    def _side_effect(cmd, *args, **kwargs):
        cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else str(cmd).split()
        cmd_str = " ".join(cmd_list)

        # 1. ffprobe command
        if any("ffprobe" in arg for arg in cmd_list):
            return Mock(returncode=0, stdout=json.dumps(ffprobe_data), stderr="")

        # 2. FFmpeg loudnorm measurement command
        if any("loudnorm" in arg for arg in cmd_list):
            return Mock(returncode=0, stdout="", stderr=loudnorm_stderr)

        # 3. FFmpeg frame extraction command
        if "-frames:v" in cmd_list:
            out_frame_path = Path(cmd_list[-1])
            out_frame_path.parent.mkdir(parents=True, exist_ok=True)
            # Write valid PNG magic bytes + synthetic payload
            out_frame_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            return Mock(returncode=0, stdout="", stderr="")

        # Default fallback
        return Mock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = _side_effect


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class QCTestCase(unittest.TestCase):
    """Test suite for Measured Quality Control (QC) in P2-007."""

    def setUp(self) -> None:
        """Initialize synthetic staging directory and file fixtures."""
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.job_dir = self.root / "jobs" / "job-1"

        work_dir = self.job_dir / "work"
        output_dir = self.job_dir / "output"
        evidence_dir = self.job_dir / "evidence"

        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)

        self.source_bytes = b"fake-source-video-bytes" * 4096
        self.candidate_bytes = b"fake-candidate-mp4-bytes" * 4096

        self.source_path = work_dir / "source.mp4"
        self.candidate_path = output_dir / "candidate.mp4"

        self.source_path.write_bytes(self.source_bytes)
        self.candidate_path.write_bytes(self.candidate_bytes)

        self.source_sha256 = hashlib.sha256(self.source_bytes).hexdigest()
        self.candidate_sha256 = hashlib.sha256(self.candidate_bytes).hexdigest()

        self.spec = make_spec(
            job_id="job-1",
            source_sha256=self.source_sha256,
            work_dir=work_dir,
            staged_source_path=self.source_path,
            output_dir=output_dir,
            candidate_path=self.candidate_path,
            style_id="viralpop",
        )
        self.render = make_render_execution(
            candidate_path=self.candidate_path,
            candidate_bytes=self.candidate_bytes,
            exit_code=0,
        )
        self.allowed_decision = Decision(allowed=True, reason="authorized")

    # -----------------------------------------------------------------------
    # Category 1: Happy Path (§8.1)
    # -----------------------------------------------------------------------

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_happy_path_viralpop(self, mock_run: Mock) -> None:
        """Verify perfect technical QC pass for viralpop style advances to READY_FOR_REVIEW."""
        mock_probe_and_loudnorm(
            mock_run,
            ffprobe_data=make_ffprobe_json(
                width=1080,
                height=1920,
                duration=10.0,
                fps="30/1",
                has_audio=True,
                channels=2,
                sample_rate=44100,
            ),
            loudnorm_stderr=make_loudnorm_stderr(
                input_i="-16.2",
                input_tp="-1.1",
            ),
        )

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        # 1. Technical checks (8 checks) must all be PASSED
        tech_check_names = [
            "renderer_exit_code",
            "video_dimensions",
            "duration_consistency",
            "frame_rate_validity",
            "audio_presence",
            "audio_loudness",
            "cryptographic_digests",
            "qc_frame_extraction",
        ]
        checks_by_name = {c.name: c for c in result.report.checks}
        for name in tech_check_names:
            self.assertIn(name, checks_by_name)
            self.assertEqual(
                checks_by_name[name].result,
                "PASSED",
                f"Expected check {name!r} to be PASSED, got {checks_by_name[name].result}",
            )

        # 2. Human sensory checks (2 checks) must unconditionally be NOT_CHECKED
        human_check_names = [
            "human_audio_verification",
            "human_visual_verification",
        ]
        for name in human_check_names:
            self.assertIn(name, checks_by_name)
            self.assertEqual(
                checks_by_name[name].result,
                "NOT_CHECKED",
                f"Expected check {name!r} to be NOT_CHECKED, got {checks_by_name[name].result}",
            )
        self.assertEqual(result.report.human_audio_verification, "NOT_CHECKED")
        self.assertEqual(result.report.human_visual_verification, "NOT_CHECKED")

        # 3. Overall status and reducer target state
        self.assertEqual(result.report.status, "REVIEW_CANDIDATE")
        self.assertEqual(result.report.unsatisfied_checks, ())
        self.assertEqual(result.target_state, "READY_FOR_REVIEW")

        # 4. Mandatory warning notice indicates uninspected status
        self.assertIn("NOT_CHECKED", result.report.warning_notice)
        self.assertIn("REVIEW CANDIDATE ONLY", result.report.warning_notice)

        # 5. EvidenceRecord created with stage 'QC_EVALUATED'
        self.assertIsNotNone(result.evidence_record)
        self.assertEqual(result.evidence_record.stage, "QC_EVALUATED")

        # 6. Serialized evidence file exists in <job_dir>/evidence/
        ev_file = self.job_dir / "evidence" / f"ev_qc_{self.spec.job_id}.json"
        self.assertTrue(ev_file.is_file(), f"Evidence file was not saved: {ev_file}")

        # 7. 3 QC inspection frames exist in <job_dir>/evidence/qc_frames/
        qc_frames_dir = self.job_dir / "evidence" / "qc_frames"
        self.assertTrue((qc_frames_dir / "frame_start.png").is_file())
        self.assertTrue((qc_frames_dir / "frame_mid.png").is_file())
        self.assertTrue((qc_frames_dir / "frame_end.png").is_file())

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_happy_path_paper_chips(self, mock_run: Mock) -> None:
        """Verify perfect technical QC pass for paper-chips style with valid frame records."""
        mock_probe_and_loudnorm(
            mock_run,
            ffprobe_data=make_ffprobe_json(
                width=1080,
                height=1920,
                duration=10.0,
                fps="30/1",
                has_audio=True,
                channels=2,
                sample_rate=44100,
            ),
            loudnorm_stderr=make_loudnorm_stderr(
                input_i="-16.2",
                input_tp="-1.1",
            ),
        )

        spec_chips = make_spec(
            job_id="job-1",
            source_sha256=self.source_sha256,
            work_dir=self.job_dir / "work",
            staged_source_path=self.source_path,
            output_dir=self.job_dir / "output",
            candidate_path=self.candidate_path,
            style_id="paper-chips",
        )

        result = run_qc(
            spec_chips,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        self.assertEqual(result.report.status, "REVIEW_CANDIDATE")
        self.assertEqual(result.target_state, "READY_FOR_REVIEW")
        self.assertEqual(len(result.report.frame_evidence), 3)

        for frame_rec in result.report.frame_evidence:
            frame_file = self.job_dir / frame_rec.file_relpath
            self.assertTrue(frame_file.is_file(), f"Frame file missing: {frame_file}")
            actual_digest = hashlib.sha256(frame_file.read_bytes()).hexdigest()
            self.assertEqual(frame_rec.file_sha256, actual_digest)

    # -----------------------------------------------------------------------
    # Category 2: Deny Path / Critical Failures (§8.2)
    # -----------------------------------------------------------------------

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_renderer_exit_code_nonzero_blocks(self, mock_run: Mock) -> None:
        """Verify non-zero renderer exit code fails check and blocks advancing."""
        mock_probe_and_loudnorm(mock_run)

        failed_render = make_render_execution(
            candidate_path=self.candidate_path,
            candidate_bytes=self.candidate_bytes,
            exit_code=1,
        )

        result = run_qc(
            self.spec,
            failed_render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("renderer_exit_code", checks_by_name)
        self.assertEqual(checks_by_name["renderer_exit_code"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertIn("renderer_exit_code", result.report.unsatisfied_checks)
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")
        self.assertNotEqual(result.target_state, "READY_FOR_REVIEW")

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_output_file_missing_or_zero_bytes_blocks(self, mock_run: Mock) -> None:
        """Verify missing candidate output file blocks QC and marks check FAILED."""
        mock_probe_and_loudnorm(mock_run)

        # Unlink candidate output file before calling run_qc
        self.candidate_path.unlink()

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("renderer_exit_code", checks_by_name)
        self.assertEqual(checks_by_name["renderer_exit_code"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertIn("renderer_exit_code", result.report.unsatisfied_checks)
        self.assertNotEqual(result.target_state, "READY_FOR_REVIEW")

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_video_dimensions_mismatch_blocks(self, mock_run: Mock) -> None:
        """Verify video dimension mismatch between source and candidate blocks QC."""
        def _side_effect(cmd, *args, **kwargs):
            cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else str(cmd).split()
            cmd_str = " ".join(cmd_list)
            if any("ffprobe" in arg for arg in cmd_list):
                if any("candidate" in arg for arg in cmd_list) or str(self.candidate_path) in cmd_str:
                    return Mock(
                        returncode=0,
                        stdout=json.dumps(make_ffprobe_json(width=1080, height=1080)),
                        stderr="",
                    )
                return Mock(
                    returncode=0,
                    stdout=json.dumps(make_ffprobe_json(width=1080, height=1920)),
                    stderr="",
                )
            if any("loudnorm" in arg for arg in cmd_list):
                return Mock(returncode=0, stdout="", stderr=make_loudnorm_stderr())
            if "-frames:v" in cmd_list:
                out_frame_path = Path(cmd_list[-1])
                out_frame_path.parent.mkdir(parents=True, exist_ok=True)
                out_frame_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
                return Mock(returncode=0, stdout="", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _side_effect

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("video_dimensions", checks_by_name)
        self.assertEqual(checks_by_name["video_dimensions"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertIn("video_dimensions", result.report.unsatisfied_checks)
        self.assertNotEqual(result.target_state, "READY_FOR_REVIEW")
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_duration_drift_exceeds_tolerance_blocks(self, mock_run: Mock) -> None:
        """Verify candidate duration drift exceeding 0.1s tolerance blocks QC."""
        def _side_effect(cmd, *args, **kwargs):
            cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else str(cmd).split()
            cmd_str = " ".join(cmd_list)
            if any("ffprobe" in arg for arg in cmd_list):
                if any("candidate" in arg for arg in cmd_list) or str(self.candidate_path) in cmd_str:
                    return Mock(
                        returncode=0,
                        stdout=json.dumps(make_ffprobe_json(duration=10.15)),
                        stderr="",
                    )
                return Mock(
                    returncode=0,
                    stdout=json.dumps(make_ffprobe_json(duration=10.0)),
                    stderr="",
                )
            if any("loudnorm" in arg for arg in cmd_list):
                return Mock(returncode=0, stdout="", stderr=make_loudnorm_stderr())
            if "-frames:v" in cmd_list:
                out_frame_path = Path(cmd_list[-1])
                out_frame_path.parent.mkdir(parents=True, exist_ok=True)
                out_frame_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
                return Mock(returncode=0, stdout="", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _side_effect

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("duration_consistency", checks_by_name)
        self.assertEqual(checks_by_name["duration_consistency"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertIn("duration_consistency", result.report.unsatisfied_checks)
        self.assertNotEqual(result.target_state, "READY_FOR_REVIEW")
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_missing_audio_stream_when_source_had_audio_fails(self, mock_run: Mock) -> None:
        """Verify candidate missing audio stream when source has audio fails check."""
        def _side_effect(cmd, *args, **kwargs):
            cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else str(cmd).split()
            cmd_str = " ".join(cmd_list)
            if any("ffprobe" in arg for arg in cmd_list):
                if any("candidate" in arg for arg in cmd_list) or str(self.candidate_path) in cmd_str:
                    return Mock(
                        returncode=0,
                        stdout=json.dumps(make_ffprobe_json(has_audio=False)),
                        stderr="",
                    )
                return Mock(
                    returncode=0,
                    stdout=json.dumps(make_ffprobe_json(has_audio=True)),
                    stderr="",
                )
            if any("loudnorm" in arg for arg in cmd_list):
                return Mock(returncode=0, stdout="", stderr=make_loudnorm_stderr())
            if "-frames:v" in cmd_list:
                out_frame_path = Path(cmd_list[-1])
                out_frame_path.parent.mkdir(parents=True, exist_ok=True)
                out_frame_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
                return Mock(returncode=0, stdout="", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _side_effect

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("audio_presence", checks_by_name)
        self.assertEqual(checks_by_name["audio_presence"].result, "FAILED")
        self.assertNotEqual(checks_by_name["audio_presence"].result, "NOT_CHECKED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertIn("audio_presence", result.report.unsatisfied_checks)
        self.assertNotEqual(result.target_state, "READY_FOR_REVIEW")
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_output_digest_mismatch_detects_mutation_blocks(self, mock_run: Mock) -> None:
        """Verify candidate media mutation on disk fails cryptographic_digests check."""
        mock_probe_and_loudnorm(mock_run)

        # Mutate candidate file content on disk after render execution creation
        self.candidate_path.write_bytes(b"X" * len(self.candidate_bytes))

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("cryptographic_digests", checks_by_name)
        self.assertEqual(checks_by_name["cryptographic_digests"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertIn("cryptographic_digests", result.report.unsatisfied_checks)
        self.assertNotEqual(result.target_state, "READY_FOR_REVIEW")
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_source_digest_mismatch_detects_mutation_blocks(self, mock_run: Mock) -> None:
        """Verify staged source media mutation on disk fails cryptographic_digests check."""
        mock_probe_and_loudnorm(mock_run)

        # Mutate source file content on disk after job specification creation
        self.source_path.write_bytes(b"Y" * len(self.source_bytes))

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("cryptographic_digests", checks_by_name)
        self.assertEqual(checks_by_name["cryptographic_digests"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertIn("cryptographic_digests", result.report.unsatisfied_checks)
        self.assertNotEqual(result.target_state, "READY_FOR_REVIEW")
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")

    # -----------------------------------------------------------------------
    # Category 3: Unmeasurable (§8.3)
    # -----------------------------------------------------------------------

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_loudnorm_unparseable_fails_closed(self, mock_run: Mock) -> None:
        """Verify unparseable loudnorm stderr fails-closed to BLOCKED without guessed values."""
        mock_probe_and_loudnorm(
            mock_run,
            loudnorm_stderr=(
                "[Parsed_loudnorm_0 @ 0x55a730] Normalization pass starting...\n"
                "[Parsed_loudnorm_0 @ 0x55a730] Error parsing audio frames\n"
                "[ffmpeg] frame=300 fps=0.0 q=-0.0 Lsize=N/A time=00:00:10.00\n"
            ),
        )

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        self.assertIsNotNone(result.report.loudness_metrics)
        assert result.report.loudness_metrics is not None
        self.assertFalse(result.report.loudness_metrics.measurement_success)
        self.assertIsNone(result.report.loudness_metrics.measured_i)
        self.assertIsNone(result.report.loudness_metrics.measured_tp)

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("audio_loudness", checks_by_name)
        self.assertEqual(checks_by_name["audio_loudness"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("audio_loudness", result.report.unsatisfied_checks)
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_ffprobe_failure_or_corrupt_json_fails_closed(self, mock_run: Mock) -> None:
        """Verify ffprobe process failure fails-closed without unhandled exceptions."""
        def _side_effect(cmd, *args, **kwargs):
            cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else str(cmd).split()
            if any("ffprobe" in arg for arg in cmd_list):
                return Mock(returncode=1, stdout="", stderr="ffprobe error: corrupt or invalid file")
            if any("loudnorm" in arg for arg in cmd_list):
                return Mock(returncode=0, stdout="", stderr=make_loudnorm_stderr())
            if "-frames:v" in cmd_list:
                out_frame_path = Path(cmd_list[-1])
                out_frame_path.parent.mkdir(parents=True, exist_ok=True)
                out_frame_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
                return Mock(returncode=0, stdout="", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _side_effect

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertEqual(checks_by_name["video_dimensions"].result, "FAILED")
        self.assertEqual(checks_by_name["duration_consistency"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")
        self.assertIn("video_dimensions", result.report.unsatisfied_checks)
        self.assertIn("duration_consistency", result.report.unsatisfied_checks)

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_missing_duration_metadata_fails_closed(self, mock_run: Mock) -> None:
        """Verify ffprobe returning 'N/A' duration fails duration_consistency check."""
        corrupt_probe = make_ffprobe_json()
        corrupt_probe["format"]["duration"] = "N/A"

        mock_probe_and_loudnorm(mock_run, ffprobe_data=corrupt_probe)

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        checks_by_name = {c.name: c for c in result.report.checks}
        self.assertIn("duration_consistency", checks_by_name)
        self.assertEqual(checks_by_name["duration_consistency"].result, "FAILED")
        self.assertEqual(result.report.status, "BLOCKED")
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("duration_consistency", result.report.unsatisfied_checks)
        self.assertEqual(result.target_state, "BLOCKED_NEEDS_REVIEW")

    # -----------------------------------------------------------------------
    # Category 4: Anti-Fake (§8.4)
    # -----------------------------------------------------------------------

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_rejects_forged_passed_flag(self, mock_run: Mock) -> None:
        """Verify QCReport and QCResult cannot be forged with global 'passed' boolean flag."""
        mock_probe_and_loudnorm(mock_run)

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        self.assertIn(result.status, ("REVIEW_CANDIDATE", "BLOCKED"))
        self.assertIn(result.report.status, ("REVIEW_CANDIDATE", "BLOCKED"))
        self.assertFalse(hasattr(result.report, "passed"))
        self.assertNotIn("passed", {f.name for f in fields(QCReport)})
        self.assertFalse(hasattr(result, "passed"))
        self.assertNotIn("passed", {f.name for f in fields(QCResult)})

    def test_qc_rejects_fabricated_human_audio_verification_passed(self) -> None:
        """Verify create_evidence_record rejects fabricated human audio verification passed."""
        fabricated_checks = (
            CheckRecord(
                name="human_audio_verification",
                result="PASSED",
                observed_value=None,
                threshold="audited_by_human",
                evidence_ref=None,
                notes="",
            ),
            CheckRecord(
                name="renderer_exit_code",
                result="PASSED",
                observed_value=0,
                threshold=0,
                evidence_ref=None,
                notes="",
            ),
        )

        with self.assertRaises(EvidenceError):
            create_evidence_record(
                evidence_id="ev-test-1",
                job_id="job-1",
                stage="QC_EVALUATED",
                tool="test",
                tool_version="1",
                method="subprocess_execution",
                command=("ffprobe", "ffmpeg"),
                input_digest_refs={
                    "source_media_sha256": "0" * 64,
                    "script_sha256": "e" * 64,
                    "candidate_sha256": "f" * 64,
                },
                artifact_sha256="f" * 64,
                artifact_relpath="output/candidate.mp4",
                captured_at=NOW,
                exit_code=0,
                checks=fabricated_checks,
                media_duration_seconds=10.0,
            )

    # -----------------------------------------------------------------------
    # Category 5: No-Listening Unattended Path (§8.5)
    # -----------------------------------------------------------------------

    @patch("subtitle.core.qc.subprocess.run")
    def test_qc_no_listening_records_honest_not_checked(self, mock_run: Mock) -> None:
        """Verify perfect technical pass records human sensory modalities honestly as NOT_CHECKED."""
        mock_probe_and_loudnorm(mock_run)

        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        self.assertEqual(result.report.human_audio_verification, "NOT_CHECKED")
        self.assertEqual(result.report.human_visual_verification, "NOT_CHECKED")
        self.assertIsNone(result.report.listener_identity)
        self.assertIsNone(result.report.viewer_identity)
        self.assertEqual(result.report.status, "REVIEW_CANDIDATE")
        self.assertIn("NOT_CHECKED", result.report.warning_notice)

    # -----------------------------------------------------------------------
    # Category 6: State Transitions (§8.6)
    # -----------------------------------------------------------------------

    def test_qc_state_transitions_via_reduce_transition(self) -> None:
        """Verify sequential mainline transitions from RENDERED to QC_EVALUATED to READY_FOR_REVIEW."""
        ckpt_rendered = JobCheckpoint(
            schema_version="1",
            job_id=self.spec.job_id,
            grant_id=self.spec.grant_id,
            current_state="RENDERED",
            state_revision=7,
            last_event_id="evt_render_job-1",
            attempt_counts=MappingProxyType({}),
            updated_at=NOW,
            blocked_from_state=None,
        )

        decision_eval = reduce_transition(
            ckpt_rendered,
            "QC_EVALUATED",
            event_id="evt_qc_eval_job-1",
            evidence_refs=("ev_qc_job-1",),
            actor="runner",
            decision="QC_EVALUATED",
            now=NOW,
            reason="QC evaluation completed",
        )
        self.assertTrue(decision_eval.accepted)
        self.assertEqual(decision_eval.next_state, "QC_EVALUATED")
        self.assertEqual(decision_eval.new_revision, 8)

        ckpt_eval = JobCheckpoint(
            schema_version="1",
            job_id=self.spec.job_id,
            grant_id=self.spec.grant_id,
            current_state=decision_eval.next_state,
            state_revision=decision_eval.new_revision,
            last_event_id=decision_eval.event.event_id,
            attempt_counts=ckpt_rendered.attempt_counts,
            updated_at=NOW,
            blocked_from_state=None,
        )

        decision_ready = reduce_transition(
            ckpt_eval,
            "READY_FOR_REVIEW",
            event_id="evt_ready_review_job-1",
            evidence_refs=("ev_qc_job-1",),
            actor="runner",
            decision="READY_FOR_REVIEW",
            now=NOW,
            reason="Candidate ready for review",
        )
        self.assertTrue(decision_ready.accepted)
        self.assertEqual(decision_ready.next_state, "READY_FOR_REVIEW")
        self.assertEqual(decision_ready.new_revision, 9)

    def test_qc_state_skip_from_rendered_to_ready_for_review_forbidden(self) -> None:
        """Verify skipping QC_EVALUATED directly to READY_FOR_REVIEW is rejected with STATE_SKIP_FORBIDDEN."""
        ckpt_rendered = JobCheckpoint(
            schema_version="1",
            job_id=self.spec.job_id,
            grant_id=self.spec.grant_id,
            current_state="RENDERED",
            state_revision=7,
            last_event_id="evt_render_job-1",
            attempt_counts=MappingProxyType({}),
            updated_at=NOW,
            blocked_from_state=None,
        )

        decision = reduce_transition(
            ckpt_rendered,
            "READY_FOR_REVIEW",
            event_id="evt_skip_attempt_job-1",
            evidence_refs=("ev_qc_job-1",),
            actor="runner",
            decision="READY_FOR_REVIEW",
            now=NOW,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, STATE_SKIP_FORBIDDEN)

    def test_qc_ready_for_review_is_terminal_locked(self) -> None:
        """Verify READY_FOR_REVIEW is a locked terminal state rejecting subsequent transitions."""
        ckpt_ready = JobCheckpoint(
            schema_version="1",
            job_id=self.spec.job_id,
            grant_id=self.spec.grant_id,
            current_state="READY_FOR_REVIEW",
            state_revision=9,
            last_event_id="evt_ready_review_job-1",
            attempt_counts=MappingProxyType({}),
            updated_at=NOW,
            blocked_from_state=None,
        )

        decision = reduce_transition(
            ckpt_ready,
            "QC_EVALUATED",
            event_id="evt_reopen_attempt_job-1",
            evidence_refs=("ev_qc_job-1",),
            actor="runner",
            decision="QC_EVALUATED",
            now=NOW,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, TERMINAL_STATE_LOCKED)

    # -----------------------------------------------------------------------
    # Category 7: Synthetic E2E Offline Integration (§8.7)
    # -----------------------------------------------------------------------

    @unittest.skipUnless(
        shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None,
        "FFmpeg and ffprobe must be installed on host to run synthetic QC integration test",
    )
    def test_qc_synthetic_mp4_integration_when_ffmpeg_available(self) -> None:
        """Verify full unmocked run_qc integration with real FFmpeg/ffprobe synthetic video."""
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=44100",
            "-t",
            "1.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ]

        res_src = subprocess.run(cmd + [str(self.source_path)], capture_output=True)
        if res_src.returncode != 0:
            self.fail(
                f"FFmpeg failed to create synthetic source file: {res_src.stderr.decode('utf-8', errors='replace')}"
            )

        res_cand = subprocess.run(cmd + [str(self.candidate_path)], capture_output=True)
        if res_cand.returncode != 0:
            self.fail(
                f"FFmpeg failed to create synthetic candidate file: {res_cand.stderr.decode('utf-8', errors='replace')}"
            )

        real_source_sha256 = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        real_candidate_bytes = self.candidate_path.read_bytes()
        real_candidate_sha256 = hashlib.sha256(real_candidate_bytes).hexdigest()
        real_candidate_size = len(real_candidate_bytes)

        spec = make_spec(
            job_id="job-1",
            source_sha256=real_source_sha256,
            work_dir=self.job_dir / "work",
            staged_source_path=self.source_path,
            output_dir=self.job_dir / "output",
            candidate_path=self.candidate_path,
            style_id="viralpop",
        )
        render = make_render_execution(
            candidate_path=self.candidate_path,
            candidate_bytes=real_candidate_bytes,
            exit_code=0,
        )

        result = run_qc(
            spec,
            render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=self.allowed_decision,
            script_sha256=SCRIPT_SHA256,
        )

        self.assertIsNotNone(result.report.media_properties)
        props = result.report.media_properties
        assert props is not None
        self.assertEqual(props.width, 320)
        self.assertEqual(props.height, 240)
        self.assertLessEqual(abs(props.duration_seconds - 1.0), 0.1)

        qc_frames_dir = self.job_dir / "evidence" / "qc_frames"
        for frame_name in ("frame_start.png", "frame_mid.png", "frame_end.png"):
            frame_file = qc_frames_dir / frame_name
            self.assertTrue(frame_file.is_file(), f"Expected frame file {frame_name} to exist")
            self.assertGreater(frame_file.stat().st_size, 0, f"Frame file {frame_name} is empty")

        self.assertEqual(result.report.status, "REVIEW_CANDIDATE")

    # --- P2-009 section 8.3: early return when authorization is denied ---
    # The code exists in run_qc (qc.py:1134); the 19 tests above never
    # passed a denied authorization_decision in (a P2-007 gap).

    def test_early_return_when_authorization_denied(self) -> None:
        """A denied authorization decision blocks QC before any probe."""
        result = run_qc(
            self.spec,
            self.render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=Decision(False, 'AUTH_REVOKED'),
            script_sha256=SCRIPT_SHA256,
        )

        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.target_state, 'BLOCKED_NEEDS_REVIEW')
        self.assertIsNone(result.evidence_record)
        self.assertEqual(result.evidence_id, '')
        self.assertIn('AUTH_REVOKED', result.reason)
        # All 10 checks (8 technical + 2 human) are NOT_CHECKED.
        self.assertEqual(len(result.report.checks), 10)
        for check in result.report.checks:
            self.assertEqual(check.result, 'NOT_CHECKED')
        self.assertEqual(
            result.report.human_audio_verification, 'NOT_CHECKED',
        )
        self.assertEqual(
            result.report.human_visual_verification, 'NOT_CHECKED',
        )
        # No evidence file was created for a blocked QC.
        self.assertEqual(list((self.job_dir / 'evidence').glob('ev_qc_*.json')), [])

    def test_early_return_does_not_run_probes(self) -> None:
        """A denied decision + a missing candidate + missing binaries
        still returns the same blocked result: the probes never ran.
        If they had run, the missing file/binary would have raised or
        failed instead of returning this clean early return."""
        missing_render = make_render_execution(
            self.job_dir / 'does-not-exist.mp4',
            b'',
            exit_code=0,
        )
        result = run_qc(
            self.spec,
            missing_render,
            now=NOW,
            job_dir=self.job_dir,
            authorization_decision=Decision(False, 'AUTH_REVOKED'),
            script_sha256=SCRIPT_SHA256,
            ffmpeg_bin='/nonexistent/ffmpeg',
            ffprobe_bin='/nonexistent/ffprobe',
        )

        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.target_state, 'BLOCKED_NEEDS_REVIEW')
        self.assertIsNone(result.evidence_record)
        self.assertEqual(result.evidence_id, '')
        self.assertIn('AUTH_REVOKED', result.reason)
        self.assertEqual(len(result.report.checks), 10)
        for check in result.report.checks:
            self.assertEqual(check.result, 'NOT_CHECKED')


if __name__ == "__main__":
    unittest.main()
