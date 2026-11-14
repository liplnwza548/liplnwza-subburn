"""Tests for P2-006 renderer adapter seam between orchestrator and legacy subtitle burners."""

import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

from subtitle.core.audit import save_checkpoint
from subtitle.core.authorization import Decision
from subtitle.core.job import JobSpec, SourceSnapshot, StagingPaths
from subtitle.core.orchestrator import _step_auto_mode_render
from subtitle.core.renderer_adapter import (
    RendererAdapterError,
    RenderExecutionResult,
    build_renderer_command,
    execute_renderer,
    prepare_mfa_words_timeline,
    render_one_clip,
)
from subtitle.core.state import JobCheckpoint

SCRIPT = 'สวัสดีตอนเช้า\nวันนี้อากาศดีมาก'
SCRIPTS = ['สวัสดีตอนเช้า', 'วันนี้อากาศดีมาก']


class RendererAdapterTests(unittest.TestCase):
    """Test suite for Phase 2 renderer adapter seam and subprocess isolation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.job_work_dir = self.root / 'staging' / 'jobs' / 'job-1' / 'work'
        self.job_output_dir = self.root / 'staging' / 'jobs' / 'job-1' / 'output'
        self.candidate_output_path = self.job_output_dir / 'candidate.mp4'
        self.job_work_dir.mkdir(parents=True, exist_ok=True)
        self.job_output_dir.mkdir(parents=True, exist_ok=True)
        self.fonts_dir = Path(__file__).resolve().parents[2] / 'subtitle' / 'fonts'
        self.SCRIPT = SCRIPT
        self.SCRIPTS = SCRIPTS

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_spec(self, style_id: str = 'viralpop') -> JobSpec:
        return JobSpec(
            schema_version='1',
            job_id='job-1',
            grant_id='grant-1',
            source=SourceSnapshot(
                provider='test',
                id='source-1',
                revision='r1',
                sha256='a' * 64,
                size_bytes=123,
                canonical_source_path='/trusted/input.mp4',
            ),
            style_id=style_id,
            style_overrides=MappingProxyType({}),
            allowed_actions=frozenset({'intake_copy', 'render_candidate'}),
            allowed_processors=frozenset({'groq'}),
            review_destination=MappingProxyType({
                'provider': 'test',
                'id': 'review-1',
                'owner_id': 'philip',
            }),
            retry_allowance=2,
            staging=StagingPaths(
                job_work_dir=str(self.job_work_dir),
                staged_input_path=str(self.job_work_dir / 'source.mp4'),
                job_output_dir=str(self.job_output_dir),
                candidate_output_path=str(self.candidate_output_path),
            ),
            created_at='2026-11-10T12:00:00Z',
        )

    @staticmethod
    def make_mfa_bytes() -> bytes:
        words = ['สวัสดี', 'ตอนเช้า', 'วันนี้', 'อากาศ', 'ดีมาก']
        starts = [0.10, 0.28, 0.60, 0.81, 1.08]
        ends = [0.28, 0.60, 0.81, 1.08, 1.32]
        entries = [[s, e, w] for s, e, w in zip(starts, ends, words)]
        payload = {
            'tiers': {
                'words': {
                    'entries': entries,
                }
            }
        }
        return json.dumps(payload, ensure_ascii=False).encode('utf-8')

    def test_prepare_mfa_words_timeline_format(self) -> None:
        # (a) Returns dict with 'words' list sorted by start, containing 'word', 'start', 'end', correct count and last end == 1.32
        mfa_bytes = self.make_mfa_bytes()
        timeline = prepare_mfa_words_timeline(mfa_bytes, self.SCRIPT)
        self.assertIn('words', timeline)
        words = timeline['words']
        self.assertEqual(len(words), 5)
        for item in words:
            self.assertIn('word', item)
            self.assertIn('start', item)
            self.assertIn('end', item)
        starts = [w['start'] for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertAlmostEqual(words[-1]['end'], 1.32, places=2)

        # (b) Artifact with '<eps>' entry is skipped without affecting valid word count
        raw = json.loads(mfa_bytes.decode('utf-8'))
        raw['tiers']['words']['entries'].insert(2, [0.55, 0.60, '<eps>'])
        eps_bytes = json.dumps(raw, ensure_ascii=False).encode('utf-8')
        timeline_eps = prepare_mfa_words_timeline(eps_bytes, self.SCRIPT)
        self.assertEqual(len(timeline_eps['words']), 5)
        self.assertNotIn('<eps>', [w['word'] for w in timeline_eps['words']])

        # (c) STT forgery markers raise RendererAdapterError
        # (c.1) 'segments' key present
        raw_segments = {
            'segments': [{'id': 0, 'text': 'test'}],
            'tiers': raw['tiers'],
        }
        with self.assertRaises(RendererAdapterError):
            prepare_mfa_words_timeline(json.dumps(raw_segments).encode('utf-8'), self.SCRIPT)

        # (c.2) entry containing 'probability'
        raw_prob = {
            'tiers': {
                'words': {
                    'entries': [
                        {'word': 'สวัสดี', 'start': 0.10, 'end': 0.28, 'probability': 0.95}
                    ]
                }
            }
        }
        with self.assertRaises(RendererAdapterError):
            prepare_mfa_words_timeline(json.dumps(raw_prob).encode('utf-8'), self.SCRIPT)

    @patch('subtitle.core.renderer_adapter.subprocess.run')
    def test_build_renderer_command_viralpop_uses_words_json_not_groq_stt(self, mock_run: Mock) -> None:
        spec = self.make_spec(style_id='viralpop')
        script_file_path = self.job_work_dir / 'staged_script.txt'
        words_json_path = self.job_work_dir / 'mfa_words.json'

        command = build_renderer_command(
            spec,
            script_file_path=script_file_path,
            words_json_path=words_json_path,
            fonts_dir=self.fonts_dir,
        )

        self.assertIn('--words-json', command)
        self.assertIn('--profile', command)
        profile_idx = command.index('--profile')
        self.assertEqual(command[profile_idx + 1], 'vo')
        self.assertIn('--video', command)
        self.assertIn('--script', command)
        self.assertTrue(any('burn_script_aligned.py' in arg for arg in command))
        self.assertNotIn('groq', ' '.join(command).lower())

        # Test render_one_clip executes and stages files
        def fake_run(cmd, *args, **kwargs):
            out_idx = cmd.index('--out') + 1
            out_file = Path(cmd[out_idx])
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(b'fake candidate video content')
            return Mock(returncode=0, stdout='', stderr='')

        mock_run.side_effect = fake_run

        result = render_one_clip(
            spec,
            script_text=self.SCRIPT,
            mfa_artifact_bytes=self.make_mfa_bytes(),
            fonts_dir=self.fonts_dir,
        )

        self.assertTrue(result.success)
        self.assertTrue((self.job_work_dir / 'staged_script.txt').is_file())
        self.assertEqual((self.job_work_dir / 'staged_script.txt').read_text(encoding='utf-8'), self.SCRIPT)

        mfa_words_path = self.job_work_dir / 'mfa_words.json'
        self.assertTrue(mfa_words_path.is_file())
        saved_words_data = json.loads(mfa_words_path.read_text(encoding='utf-8'))
        self.assertIn('words', saved_words_data)
        self.assertEqual(len(saved_words_data['words']), 5)
        self.assertEqual(
            [w['word'] for w in saved_words_data['words']],
            ['สวัสดี', 'ตอนเช้า', 'วันนี้', 'อากาศ', 'ดีมาก'],
        )

    @patch('subtitle.core.renderer_adapter.subprocess.run')
    def test_build_renderer_command_paper_chips_uses_stt_and_tmpdir(self, mock_run: Mock) -> None:
        spec = self.make_spec(style_id='paper-chips')
        script_file_path = self.job_work_dir / 'staged_script.txt'
        words_json_path = self.job_work_dir / 'mfa_words.json'

        command = build_renderer_command(
            spec,
            script_file_path=script_file_path,
            words_json_path=words_json_path,
            fonts_dir=self.fonts_dir,
        )

        self.assertIn('--stt', command)
        self.assertNotIn('--fonts', command)

        def fake_run(cmd, *args, **kwargs):
            out_idx = cmd.index('--out') + 1
            out_file = Path(cmd[out_idx])
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(b'fake paper chips candidate video')
            return Mock(returncode=0, stdout='', stderr='')

        mock_run.side_effect = fake_run

        result = render_one_clip(
            spec,
            script_text=self.SCRIPT,
            mfa_artifact_bytes=self.make_mfa_bytes(),
            fonts_dir=self.fonts_dir,
        )

        self.assertTrue(result.success)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        env_passed = call_kwargs.get('env')
        self.assertIsNotNone(env_passed)
        expected_tmpdir = str(self.job_work_dir / 'tmp')
        self.assertEqual(env_passed.get('TMPDIR'), expected_tmpdir)
        # Note: Passing TMPDIR does not solve chip_burn's hardcoded dir='/tmp' in mkdtemp (residual risk C2 accepted in writing).

    @patch('subtitle.core.renderer_adapter.subprocess.run')
    @patch('subtitle.core.orchestrator.verify', return_value=Decision(True, 'OK'))
    def test_execute_renderer_nonzero_exit_fails_and_orchestrator_records_failed_job(self, mock_verify: Mock, mock_run: Mock) -> None:
        # (a) execute_renderer on nonzero exit without output file fails
        mock_run.return_value = Mock(returncode=1, stdout='', stderr='boom')
        if self.candidate_output_path.exists():
            self.candidate_output_path.unlink()

        result = execute_renderer(
            command=('python', 'fake.py'),
            expected_output_path=self.candidate_output_path,
            timeout_seconds=10,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(result.output_sha256)

        # (b) Orchestrator _step_auto_mode_render records FAILED_JOB on nonzero exit
        spec = self.make_spec(style_id='viralpop')
        job_dir = Path(spec.staging.job_work_dir).parent
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / 'events.jsonl').touch()

        checkpoint = JobCheckpoint(
            schema_version='1',
            job_id=spec.job_id,
            grant_id=spec.grant_id,
            current_state='RENDER_PERMITTED',
            state_revision=6,
            last_event_id='evt_g3_job-1',
            attempt_counts=MappingProxyType({'intake_copy': 1, 'render_candidate': 1}),
            updated_at='2026-11-10T12:00:00Z',
            blocked_from_state=None,
        )
        save_checkpoint(job_dir / 'checkpoint.json', checkpoint)

        orch_result = _step_auto_mode_render(
            spec=spec,
            checkpoint=checkpoint,
            script_text=self.SCRIPT,
            mfa_artifact_bytes=self.make_mfa_bytes(),
            fonts_dir=self.fonts_dir,
            auto_mode=True,
            evidence_ids=('ev_transcript_job-1', 'ev_g1_job-1', 'ev_g2_job-1', 'ev_g3_job-1'),
            now_iso='2026-11-10T12:00:00Z',
            now=datetime(2026, 11, 10, 12, tzinfo=timezone.utc),
            # P2-009: the retry loop re-verifies before attempts 2+; the
            # fixture checkpoint already counts render_candidate=1, so
            # attempt 2 re-verifies first. verify() is mocked above to
            # return OK, so the re-verify passes and this test isolates
            # the renderer-failure recording (not verify). The grant
            # bytes are never read from disk by the step (P2-003 C1).
            grant_bytes=b'{}',
            status_bytes=b'{}',
            trusted_grant_keys={},
            trusted_status_keys={},
            # Fake sleep: the retry backoff must not pause the test run.
            sleep_fn=lambda _seconds: None,
        )

        self.assertFalse(orch_result.success)
        self.assertEqual(orch_result.final_state, 'FAILED_JOB')
        self.assertEqual(orch_result.deny_code, 'RENDER_EXECUTION_FAILED')
        self.assertTrue(orch_result.renderer_invoked)
        # P2-009: the fixture already counted 1 attempt, so this call
        # made attempts 2 and 3, then exhausted the quota (1 + 2 = 3).
        self.assertEqual(
            orch_result.checkpoint.attempt_counts['render_candidate'], 3,
        )
        self.assertTrue(
            (job_dir / 'evidence' / 'retry_render_candidate_002.json').is_file(),
        )
        self.assertTrue(
            (job_dir / 'evidence' / 'retry_render_candidate_003.json').is_file(),
        )

    @patch('subtitle.core.renderer_adapter.subprocess.run')
    @patch('subtitle.core.orchestrator.verify', return_value=Decision(True, 'OK'))
    def test_execute_renderer_timeout_fails_and_orchestrator_records_failed_job(self, mock_verify: Mock, mock_run: Mock) -> None:
        # (a) execute_renderer on timeout fails with exit_code=-1
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=('python', 'fake.py'), timeout=10)
        if self.candidate_output_path.exists():
            self.candidate_output_path.unlink()

        result = execute_renderer(
            command=('python', 'fake.py'),
            expected_output_path=self.candidate_output_path,
            timeout_seconds=10,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, -1)
        self.assertIsNone(result.output_sha256)

        # (b) Orchestrator _step_auto_mode_render records FAILED_JOB on timeout
        spec = self.make_spec(style_id='viralpop')
        job_dir = Path(spec.staging.job_work_dir).parent
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / 'events.jsonl').touch()

        checkpoint = JobCheckpoint(
            schema_version='1',
            job_id=spec.job_id,
            grant_id=spec.grant_id,
            current_state='RENDER_PERMITTED',
            state_revision=6,
            last_event_id='evt_g3_job-1',
            attempt_counts=MappingProxyType({'intake_copy': 1, 'render_candidate': 1}),
            updated_at='2026-11-10T12:00:00Z',
            blocked_from_state=None,
        )
        save_checkpoint(job_dir / 'checkpoint.json', checkpoint)

        orch_result = _step_auto_mode_render(
            spec=spec,
            checkpoint=checkpoint,
            script_text=self.SCRIPT,
            mfa_artifact_bytes=self.make_mfa_bytes(),
            fonts_dir=self.fonts_dir,
            auto_mode=True,
            evidence_ids=('ev_transcript_job-1', 'ev_g1_job-1', 'ev_g2_job-1', 'ev_g3_job-1'),
            now_iso='2026-11-10T12:00:00Z',
            now=datetime(2026, 11, 10, 12, tzinfo=timezone.utc),
            # P2-009: the retry loop re-verifies before attempts 2+; the
            # fixture checkpoint already counts render_candidate=1, so
            # attempt 2 re-verifies first. verify() is mocked above to
            # return OK, so the re-verify passes and this test isolates
            # the renderer-failure recording (not verify). The grant
            # bytes are never read from disk by the step (P2-003 C1).
            grant_bytes=b'{}',
            status_bytes=b'{}',
            trusted_grant_keys={},
            trusted_status_keys={},
            # Fake sleep: the retry backoff must not pause the test run.
            sleep_fn=lambda _seconds: None,
        )

        self.assertFalse(orch_result.success)
        self.assertEqual(orch_result.final_state, 'FAILED_JOB')
        self.assertEqual(orch_result.deny_code, 'RENDER_EXECUTION_FAILED')
        self.assertTrue(orch_result.renderer_invoked)
        # P2-009: the fixture already counted 1 attempt, so this call
        # made attempts 2 and 3, then exhausted the quota (1 + 2 = 3).
        self.assertEqual(
            orch_result.checkpoint.attempt_counts['render_candidate'], 3,
        )
        self.assertTrue(
            (job_dir / 'evidence' / 'retry_render_candidate_002.json').is_file(),
        )
        self.assertTrue(
            (job_dir / 'evidence' / 'retry_render_candidate_003.json').is_file(),
        )

    @patch('subtitle.core.renderer_adapter.subprocess.run')
    def test_execute_renderer_missing_output_fails_closed(self, mock_run: Mock) -> None:
        mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
        if self.candidate_output_path.exists():
            self.candidate_output_path.unlink()

        result = execute_renderer(
            command=('python', 'fake.py'),
            expected_output_path=self.candidate_output_path,
            timeout_seconds=10,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.output_sha256)
        self.assertEqual(result.error_message, 'missing or empty candidate output')

    def _assert_renderer_temp_cleanup(self, returncode: int) -> None:
        preexisting_dirs = []
        for prefix in ('chipstrip-', 'protostrip-'):
            path = Path(tempfile.mkdtemp(prefix=f'{prefix}adapter-existing-', dir='/tmp'))
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
            marker = path / 'keep.txt'
            marker.write_bytes(b'pre-existing')
            preexisting_dirs.append((path, marker))

        created_dirs = []

        def fake_run(*args, **kwargs):
            for prefix in ('chipstrip-', 'protostrip-'):
                path = Path(tempfile.mkdtemp(prefix=f'{prefix}adapter-created-', dir='/tmp'))
                self.addCleanup(shutil.rmtree, path, ignore_errors=True)
                (path / 'temporary.txt').write_bytes(b'temporary')
                created_dirs.append(path)
            if returncode == 0:
                self.candidate_output_path.write_bytes(b'candidate video')
            return Mock(
                returncode=returncode,
                stdout='',
                stderr='' if returncode == 0 else 'renderer failed',
            )

        with patch('subtitle.core.renderer_adapter.subprocess.run', side_effect=fake_run):
            result = execute_renderer(
                command=('python', 'fake.py'),
                expected_output_path=self.candidate_output_path,
                timeout_seconds=10,
            )

        self.assertEqual(result.success, returncode == 0)
        self.assertEqual(len(created_dirs), 2)
        for path in created_dirs:
            self.assertFalse(path.exists())
        for path, marker in preexisting_dirs:
            self.assertTrue(path.is_dir())
            self.assertEqual(marker.read_bytes(), b'pre-existing')

    def test_execute_renderer_cleans_new_temp_dirs_after_success(self) -> None:
        self._assert_renderer_temp_cleanup(returncode=0)

    def test_execute_renderer_cleans_new_temp_dirs_after_failure(self) -> None:
        self._assert_renderer_temp_cleanup(returncode=1)


if __name__ == '__main__':
    unittest.main()
