"""Offline P2-006 orchestrator tests (Part 1: fixtures, helpers, and tests 1-3)."""

import base64
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pythainlp import word_tokenize

from subtitle.core.audit import (
    ReplayLedger,
    load_checkpoint,
    load_event_log,
    save_checkpoint,
)
from subtitle.core.authorization import Decision, canonical
from subtitle.core.qc import QCResult
from subtitle.core.orchestrator import (
    AUTO_MODE_DISABLED,
    RENDER_EXECUTION_FAILED,
    OrchestratorResult,
    _resolve_existing_binding,
    _step_auto_mode_render,
    _step_intake,
    _step_mfa_g2,
    _step_pre_verify,
    _step_reverify_g3,
    _step_to_authorized,
    _step_transcript_g1,
    orchestrate_render_job,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
NOW_ISO = '2026-11-10T12:00:00Z'
GRANT_DOMAIN = b'ThaiSubburn JobGrant v1\x00'
STATUS_DOMAIN = b'ThaiSubburn Revocation v1\x00'


def ts(value: datetime) -> str:
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def sign(obj: dict, key: Ed25519PrivateKey, domain: bytes) -> bytes:
    payload = dict(obj)
    payload.pop('signature', None)
    payload['signature'] = base64.b64encode(
        key.sign(domain + canonical(payload))
    ).decode('ascii')
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


LINES = ['สวัสดีตอนเช้า', 'วันนี้อากาศดีมาก']
SCRIPT = ''.join(LINES)


def _tokenize_lines(lines: list[str]) -> list[str]:
    raw_tokens: list[str] = []
    for line in lines:
        cleaned = line.strip()
        if cleaned:
            raw_tokens.extend(
                t for t in word_tokenize(cleaned, engine='newmm', keep_whitespace=False)
                if t.strip()
            )
    tokens: list[str] = []
    for t in raw_tokens:
        if t == 'ๆ' and tokens:
            tokens[-1] += t
        else:
            tokens.append(t)
    return tokens


TOKENS = _tokenize_lines(LINES)
DEFAULT_DURATIONS = [0.18, 0.32, 0.21, 0.27, 0.24]


def make_mfa_bytes(words=None, durations=None, starts=None) -> bytes:
    if words is None:
        words = TOKENS
    if durations is None:
        durations = DEFAULT_DURATIONS
    if starts is None:
        entries = []
        curr = 0.0
        for i, (w, d) in enumerate(zip(words, durations)):
            if i > 0:
                curr += 0.05
            start = round(curr, 4)
            end = round(start + d, 4)
            entries.append([start, end, w])
            curr = end
    else:
        entries = [
            [round(s, 4), round(s + d, 4), w]
            for w, d, s in zip(words, durations, starts)
        ]
    payload = {
        'tiers': {
            'words': {
                'entries': entries,
            }
        }
    }
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


def make_mfa_meta(**overrides) -> dict:
    meta = {
        'tool': 'thai_mfa',
        'tool_version': '0.1.0',
        'method': 'synthetic_stub',
        'command': ['mfa', 'align'],
        'exit_code': 0,
        'captured_at': NOW_ISO,
    }
    meta.update(overrides)
    return meta


def make_transcript_meta() -> dict:
    return {
        'processor': 'groq',
        'model': 'whisper-large-v3-turbo',
        'model_version': 'v3',
        'method': 'synthetic_stub',
        'captured_at': NOW_ISO,
    }


def make_cues(script_lines: list[str]) -> list[dict]:
    cues = []
    cursor = 0
    for line in script_lines:
        start = cursor
        end = cursor + len(line)
        cues.append({
            'text': line,
            'source_char_start': start,
            'source_char_end': end,
        })
        cursor = end
    return cues


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_root = self.root / 'trusted-source'
        self.base_staging_dir = self.root / 'staging'
        self.source_root.mkdir()
        self.source_path = self.source_root / 'synthetic.mp4'
        self.source_bytes = b'fake-video-bytes-p2-006\x00' * 4096
        self.source_path.write_bytes(self.source_bytes)
        self.expected_hash = hashlib.sha256(self.source_bytes).hexdigest()
        self.source_ref = dict(
            provider='test',
            id='source-1',
            revision='r1',
            sha256=self.expected_hash,
        )
        self.review_destination = dict(
            provider='test',
            id='review-1',
            owner_id='philip',
        )
        self.grant_key = Ed25519PrivateKey.generate()
        self.status_key = Ed25519PrivateKey.generate()
        self.fonts_dir = Path(__file__).resolve().parents[2] / 'subtitle' / 'fonts'

    def make_grant(self, **overrides) -> dict:
        grant = dict(
            schema_version='1',
            grant_id='grant-1',
            job_id='job-1',
            issuer_id='owner',
            key_id='g1',
            issued_at=ts(NOW - timedelta(hours=1)),
            expires_at=ts(NOW + timedelta(hours=1)),
            source_ref=copy.deepcopy(self.source_ref),
            style_id='viralpop',
            style_overrides={},
            allowed_actions=['intake_copy', 'render_candidate'],
            allowed_processors=['groq'],
            review_destination=dict(self.review_destination),
            retry_allowance=2,
        )
        grant.update(overrides)
        return grant

    def make_status(self, **overrides) -> dict:
        status = dict(
            schema_version='1',
            grant_id='grant-1',
            job_id='job-1',
            issuer_id='status',
            key_id='s1',
            checked_at=NOW_ISO,
            status='active',
        )
        status.update(overrides)
        return status

    def run_job(
        self,
        *,
        grant=None,
        status=None,
        grant_bytes=None,
        status_bytes=None,
        status_raw=None,
        **overrides,
    ):
        job_id = overrides.get('job_id', 'job-1')
        grant_id = overrides.get('grant_id', 'grant-1')
        style_id = overrides.get('style_id', 'viralpop')
        style_overrides = overrides.get('style_overrides', {})
        source_ref = overrides.get('source_ref', copy.deepcopy(self.source_ref))
        review_destination = overrides.get(
            'review_destination', copy.deepcopy(self.review_destination)
        )

        if grant_bytes is None:
            g = grant if grant is not None else self.make_grant(
                job_id=job_id,
                grant_id=grant_id,
                style_id=style_id,
                style_overrides=style_overrides,
                source_ref=source_ref,
                review_destination=review_destination,
            )
            raw_grant = sign(g, self.grant_key, GRANT_DOMAIN)
        else:
            raw_grant = grant_bytes

        s_bytes = status_bytes if status_bytes is not None else status_raw
        if s_bytes is None:
            s = status if status is not None else self.make_status(
                job_id=job_id,
                grant_id=grant_id,
            )
            raw_status = sign(s, self.status_key, STATUS_DOMAIN)
        else:
            raw_status = s_bytes

        args = dict(
            trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
            trusted_status_keys={('status', 's1'): self.status_key.public_key()},
            now=NOW,
            job_id=job_id,
            grant_id=grant_id,
            source_ref=source_ref,
            style_id=style_id,
            style_overrides=style_overrides,
            review_destination=review_destination,
            allowed_actions=['intake_copy', 'render_candidate'],
            allowed_processors=['groq'],
            retry_allowance=2,
            source_path=self.source_path,
            trusted_source_root=self.source_root,
            base_staging_dir=self.base_staging_dir,
            transcript_data={'text': SCRIPT},
            script_text=SCRIPT,
            script_lines=LINES,
            transcript_tool_metadata=make_transcript_meta(),
            mfa_artifact_bytes=make_mfa_bytes(),
            mfa_tool_metadata=make_mfa_meta(),
            cues=make_cues(LINES),
            media_duration_seconds=60.0,
            fonts_dir=self.fonts_dir,
            expected_audio_sha256='d' * 64,
            auto_mode=False,
            existing_binding=None,
        )
        args.update(overrides)
        # P2-009: the caller supplies the script digest (P2-006 Q5);
        # run_qc needs it to create the ev_qc evidence record.
        args['script_sha256'] = hashlib.sha256(
            args['script_text'].encode('utf-8'),
        ).hexdigest()
        return orchestrate_render_job(raw_grant, raw_status, **args)

    def _mock_render_success(self, mock_run):
        def side_effect(command, **kwargs):
            out_idx = list(command).index('--out') + 1
            out_path = Path(command[out_idx])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b'fake-candidate-mp4-bytes' * 100)
            return Mock(returncode=0, stdout='', stderr='')

        mock_run.side_effect = side_effect

    def _mock_render_fail_then_succeed(self, mock_run, fail_count=1):
        """Render mock: fail the first ``fail_count`` calls (exit 1, no
        output file), then succeed like _mock_render_success."""
        calls = {'n': 0}

        def side_effect(command, **kwargs):
            calls['n'] += 1
            if calls['n'] <= fail_count:
                return Mock(returncode=1, stdout='', stderr='boom')
            out_idx = list(command).index('--out') + 1
            out_path = Path(command[out_idx])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b'fake-candidate-mp4-bytes' * 100)
            return Mock(returncode=0, stdout='', stderr='')

        mock_run.side_effect = side_effect

    def _mock_qc_pass(self, mock_qc):
        """Stand-in for a passing run_qc (P2-009 section 5.4).

        The render mock above writes fake bytes, not a real MP4, so
        real QC probes would fail on it. This canned QCResult lets
        tests 1-2 verify the wiring (entrypoint calls run_qc fresh
        after a successful render and transitions per its target_state)
        without pretending the fake bytes passed real probes. The real
        QC path is covered by test_qc.py and by test 17 (real burn).
        evidence_record is a stand-in object: the orchestrator only
        checks that it is not None before collecting the evidence id;
        the mock writes no ev_qc file (test 17 covers the real file).
        """
        mock_qc.return_value = QCResult(
            status='REVIEW_CANDIDATE',
            report=None,
            evidence_record=Mock(),
            evidence_id='ev_qc_job-1',
            target_state='READY_FOR_REVIEW',
            reason='QC passed (mocked)',
            unsatisfied_checks=(),
        )

    def _assert_zero_renders(self, result, mock_run):
        # 1. Mock subprocess was never called
        mock_run.assert_not_called()
        # 2. Result reports renderer was not invoked
        self.assertFalse(result.renderer_invoked)
        # 3. No candidate output exists on disk
        if result.spec is not None:
            self.assertFalse(
                Path(result.spec.staging.candidate_output_path).exists()
            )
        else:
            self.assertFalse((self.base_staging_dir / 'jobs').exists())
        # 4. Checkpoint is not RENDERED, and events log has no transition to RENDERED
        if result.checkpoint is not None:
            self.assertNotEqual(result.checkpoint.current_state, 'RENDERED')
            if result.spec is not None:
                job_dir = Path(result.spec.staging.job_work_dir).parent
                events_file = job_dir / 'events.jsonl'
                if events_file.is_file():
                    events = load_event_log(events_file)
                    self.assertTrue(all(e.next_state != 'RENDERED' for e in events))

    def test_orchestrate_happy_path_viralpop_auto_mode_true(self):
        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
        ):
            self._mock_render_success(mock_run)
            # P2-009 section 5.4: the entrypoint now calls run_qc fresh
            # after a successful render; the happy path ends at
            # READY_FOR_REVIEW, not RENDERED.
            self._mock_qc_pass(mock_qc)
            result = self.run_job(auto_mode=True)

            self.assertEqual(mock_run.call_count, 1)
            self.assertEqual(mock_qc.call_count, 1)
            qc_kwargs = mock_qc.call_args.kwargs
            self.assertTrue(qc_kwargs['authorization_decision'].allowed)
            self.assertEqual(
                qc_kwargs['script_sha256'],
                hashlib.sha256(SCRIPT.encode('utf-8')).hexdigest(),
            )
            self.assertEqual(qc_kwargs['now'], NOW_ISO)
            command = mock_run.call_args.args[0]

            video_idx = list(command).index('--video') + 1
            self.assertEqual(command[video_idx], str(result.spec.staging.staged_input_path))

            script_idx = list(command).index('--script') + 1
            self.assertTrue(command[script_idx].endswith('staged_script.txt'))

            self.assertIn('--fonts', command)
            fonts_idx = list(command).index('--fonts') + 1
            self.assertEqual(command[fonts_idx], str(self.fonts_dir))

            out_idx = list(command).index('--out') + 1
            self.assertEqual(command[out_idx], str(result.spec.staging.candidate_output_path))

            words_idx = list(command).index('--words-json') + 1
            self.assertTrue(command[words_idx].endswith('mfa_words.json'))

            profile_idx = list(command).index('--profile') + 1
            self.assertEqual(command[profile_idx], 'vo')

            self.assertNotIn('--stt', command)

            kwargs = mock_run.call_args.kwargs
            self.assertIs(kwargs.get('shell'), False)
            self.assertIsNone(kwargs.get('env'))

            self.assertTrue(result.success)
            self.assertTrue(result.renderer_invoked)
            self.assertIsNone(result.deny_code)
            self.assertIsNotNone(result.checkpoint)
            self.assertEqual(result.checkpoint.current_state, 'READY_FOR_REVIEW')
            self.assertEqual(result.checkpoint.state_revision, 9)

            job_dir = Path(result.spec.staging.job_work_dir).parent
            events = load_event_log(job_dir / 'events.jsonl')
            self.assertEqual(len(events), 9)
            self.assertEqual([e.state_revision for e in events], list(range(1, 10)))
            self.assertEqual(events[6].next_state, 'RENDERED')
            self.assertEqual(
                events[6].evidence_refs,
                (
                    f"render:{result.render_execution.output_sha256}",
                    'retry:render_candidate:attempts=1',
                ),
            )
            self.assertEqual(events[-1].next_state, 'READY_FOR_REVIEW')

            candidate_bytes = Path(result.spec.staging.candidate_output_path).read_bytes()
            candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
            self.assertEqual(result.render_execution.output_sha256, candidate_sha256)

            self.assertEqual(
                result.evidence_ids,
                (
                    'ev_transcript_job-1',
                    'ev_g1_job-1',
                    'ev_g2_job-1',
                    'ev_g3_job-1',
                    'ev_qc_job-1',
                ),
            )

            evidence_dir = job_dir / 'evidence'
            # Only the 4 real evidence files exist on disk; the mocked
            # QC returns an id but writes no file (test 17 covers the
            # real ev_qc file).
            real_ev_ids = (
                'ev_transcript_job-1',
                'ev_g1_job-1',
                'ev_g2_job-1',
                'ev_g3_job-1',
            )
            for ev_id in real_ev_ids:
                ev_file = evidence_dir / f"{ev_id}.json"
                self.assertTrue(ev_file.is_file())
                ev_data = json.loads(ev_file.read_text(encoding='utf-8'))
                self.assertNotEqual(ev_data.get('stage'), 'RENDERED')

            for ev_file in evidence_dir.glob('*.json'):
                ev_data = json.loads(ev_file.read_text(encoding='utf-8'))
                self.assertNotEqual(ev_data.get('stage'), 'RENDERED')

    def test_orchestrate_happy_path_paper_chips_auto_mode_true(self):
        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
        ):
            self._mock_render_success(mock_run)
            # P2-009 section 5.4: the entrypoint now calls run_qc fresh
            # after a successful render; the happy path ends at
            # READY_FOR_REVIEW, not RENDERED.
            self._mock_qc_pass(mock_qc)
            result = self.run_job(style_id='paper-chips', auto_mode=True)

            self.assertEqual(mock_run.call_count, 1)
            self.assertEqual(mock_qc.call_count, 1)
            command = mock_run.call_args.args[0]
            self.assertTrue(any('chip_burn.py' in arg for arg in command))

            video_idx = list(command).index('--video') + 1
            self.assertEqual(command[video_idx], str(result.spec.staging.staged_input_path))

            script_idx = list(command).index('--script') + 1
            self.assertTrue(command[script_idx].endswith('staged_script.txt'))

            stt_idx = list(command).index('--stt') + 1
            self.assertTrue(command[stt_idx].endswith('mfa_words.json'))

            out_idx = list(command).index('--out') + 1
            self.assertEqual(command[out_idx], str(result.spec.staging.candidate_output_path))

            fps_idx = list(command).index('--fps') + 1
            self.assertEqual(command[fps_idx], '30')

            crf_idx = list(command).index('--crf') + 1
            self.assertEqual(command[crf_idx], '18')

            self.assertNotIn('--fonts', command)
            self.assertNotIn('--profile', command)

            kwargs = mock_run.call_args.kwargs
            self.assertIs(kwargs.get('shell'), False)
            env = kwargs.get('env')
            self.assertIsInstance(env, dict)
            expected_tmp = str(Path(result.spec.staging.job_work_dir) / 'tmp')
            self.assertEqual(env.get('TMPDIR'), expected_tmp)
            # Note on Appendix C2 residual constraint:
            # Passing TMPDIR points to <job_work_dir>/tmp, but this does not solve
            # the issue in chip_burn.py line 246 where dir='/tmp' is explicitly
            # hardcoded in tempfile.mkdtemp(prefix='chipstrip-', dir='/tmp') (Python
            # tempfile module ignores TMPDIR when dir is explicitly supplied). This
            # residual risk has been formally acknowledged and accepted in writing;
            # actual peak /tmp disk usage must be measured during trial runs.

            self.assertTrue(result.success)
            self.assertTrue(result.renderer_invoked)
            self.assertIsNone(result.deny_code)
            self.assertIsNotNone(result.checkpoint)
            self.assertEqual(result.checkpoint.current_state, 'READY_FOR_REVIEW')
            self.assertEqual(result.checkpoint.state_revision, 9)
            self.assertIn('ev_qc_job-1', result.evidence_ids)

    def test_orchestrate_auto_mode_false_halts_at_render_permitted_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            result = self.run_job(auto_mode=False)

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.final_state, 'RENDER_PERMITTED')
            self.assertEqual(result.deny_code, AUTO_MODE_DISABLED)
            self.assertFalse(result.renderer_invoked)
            self.assertIsNotNone(result.checkpoint)
            self.assertEqual(result.checkpoint.current_state, 'RENDER_PERMITTED')
            self.assertEqual(result.checkpoint.state_revision, 6)

            evidence_dir = Path(result.spec.staging.job_work_dir).parent / 'evidence'
            expected_ids = (
                'ev_transcript_job-1',
                'ev_g1_job-1',
                'ev_g2_job-1',
                'ev_g3_job-1',
            )
            self.assertEqual(result.evidence_ids, expected_ids)
            for ev_id in expected_ids:
                self.assertTrue((evidence_dir / f"{ev_id}.json").is_file())

            self.assertFalse(Path(result.spec.staging.candidate_output_path).exists())

    def test_orchestrate_invalid_grant_schema_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            result = self.run_job(grant_bytes=b'{}')

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'AUTH_SCHEMA_INVALID')
            self.assertEqual(result.final_state, 'BLOCKED_AUTHORIZATION')
            self.assertIsNone(result.spec)
            self.assertIsNone(result.checkpoint)
            self.assertFalse(self.base_staging_dir.exists())

    def test_orchestrate_untrusted_grant_signature_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            valid_grant = self.make_grant()
            valid_bytes = sign(valid_grant, self.grant_key, GRANT_DOMAIN)
            tampered_bytes = valid_bytes.replace(b'viralpop', b'paper-chips')

            result = self.run_job(grant_bytes=tampered_bytes)

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'AUTH_INVALID_OR_UNTRUSTED')
            self.assertEqual(result.final_state, 'BLOCKED_AUTHORIZATION')
            self.assertIsNone(result.spec)
            self.assertIsNone(result.checkpoint)

    def test_orchestrate_expired_grant_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            grant = self.make_grant(expires_at=ts(NOW))

            result = self.run_job(grant=grant)

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'AUTH_EXPIRED_OR_NOT_YET_VALID')
            self.assertEqual(result.final_state, 'BLOCKED_AUTHORIZATION')
            self.assertIsNone(result.spec)
            self.assertIsNone(result.checkpoint)

    def test_orchestrate_replay_grant_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            self.base_staging_dir.mkdir(parents=True, exist_ok=True)
            ledger = ReplayLedger(Path(self.base_staging_dir) / 'replay_ledger.json')
            ledger.bind_grant('grant-1', 'job-other', NOW_ISO)

            result = self.run_job()

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'GRANT_REPLAY')
            self.assertEqual(result.final_state, 'BLOCKED_AUTHORIZATION')
            self.assertIsNone(result.spec)
            self.assertIsNone(result.checkpoint)

    def test_orchestrate_source_digest_mismatch_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            mismatched_ref = dict(self.source_ref, sha256='e' * 64)

            result = self.run_job(source_ref=mismatched_ref)

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'SOURCE_DIGEST_MISMATCH')
            self.assertEqual(result.final_state, 'FAILED_JOB')
            self.assertIsNone(result.spec)
            self.assertIsNone(result.checkpoint)
            self.assertFalse((self.base_staging_dir / 'jobs' / 'job-1').exists())

    def test_orchestrate_unsupported_style_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            grant = self.make_grant(style_id='karaoke')
            result = self.run_job(grant=grant, style_id='karaoke')

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'STYLE_NOT_AUTHORIZED')
            self.assertEqual(result.final_state, 'BLOCKED_AUTHORIZATION')
            self.assertIsNone(result.spec)
            self.assertIsNone(result.checkpoint)

    def test_orchestrate_style_overrides_forbidden_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            grant = self.make_grant(style_overrides={'font_size': 48})
            result = self.run_job(grant=grant, style_overrides={'font_size': 48})

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'STYLE_NOT_AUTHORIZED')
            self.assertEqual(result.final_state, 'BLOCKED_AUTHORIZATION')
            self.assertIsNone(result.spec)
            self.assertIsNone(result.checkpoint)

    def test_orchestrate_transcript_conflict_gate_g1_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            result = self.run_job(transcript_data={'text': 'สวัสดีตอนเย็น'})

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'SCRIPT_NEEDS_EVIDENCE')
            self.assertEqual(result.final_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertIsNotNone(result.checkpoint)
            self.assertEqual(result.checkpoint.current_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertEqual(result.checkpoint.blocked_from_state, 'TRANSCRIBED')
            self.assertEqual(result.checkpoint.state_revision, 4)

    def test_orchestrate_stt_forged_as_mfa_gate_g2_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            stt_artifact = {
                'segments': [{'id': 0, 'text': SCRIPT}],
                'words': [{'word': w, 'probability': 0.9} for w in TOKENS],
            }
            artifact_bytes = json.dumps(stt_artifact, ensure_ascii=False).encode('utf-8')

            result = self.run_job(mfa_artifact_bytes=artifact_bytes)

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'MFA_EVIDENCE_INVALID')
            self.assertEqual(result.final_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertIsNotNone(result.checkpoint)
            self.assertEqual(result.checkpoint.current_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertEqual(result.checkpoint.blocked_from_state, 'SCRIPT_EVIDENCE_CHECKED')
            self.assertEqual(result.checkpoint.state_revision, 5)

    def test_orchestrate_mfa_unk_word_ambiguity_gate_g2_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            mfa_bytes = make_mfa_bytes(
                words=(TOKENS[:2] + ['<unk>'] + TOKENS[3:]),
                durations=DEFAULT_DURATIONS,
            )

            result = self.run_job(mfa_artifact_bytes=mfa_bytes)

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'SCRIPT_NEEDS_EVIDENCE')
            self.assertEqual(result.final_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertIsNotNone(result.checkpoint)
            self.assertEqual(result.checkpoint.current_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertEqual(result.checkpoint.blocked_from_state, 'SCRIPT_EVIDENCE_CHECKED')
            self.assertEqual(result.checkpoint.state_revision, 5)

    def test_orchestrate_revocation_between_stages_cancels_job_zero_renders(self):
        # Entrypoint orchestrate_render_job takes a single status_bytes parameter,
        # so it cannot simulate a grant being active at start but revoked before Gate G3.
        # To test revocation occurring between pipeline stages, we drive step helpers
        # 1-5 directly with active status, then invoke _step_reverify_g3 with revoked status.
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            grant_bytes = sign(self.make_grant(), self.grant_key, GRANT_DOMAIN)
            status_active = sign(self.make_status(), self.status_key, STATUS_DOMAIN)
            existing = _resolve_existing_binding(
                base_staging_dir=self.base_staging_dir,
                grant_id='grant-1',
                caller_binding=None,
            )

            pre = _step_pre_verify(
                grant_bytes=grant_bytes,
                status_bytes=status_active,
                trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
                trusted_status_keys={('status', 's1'): self.status_key.public_key()},
                now=NOW,
                job_id='job-1',
                source_ref=copy.deepcopy(self.source_ref),
                style_id='viralpop',
                style_overrides={},
                review_destination=copy.deepcopy(self.review_destination),
                existing_binding=existing,
            )
            self.assertNotIsInstance(pre, OrchestratorResult)
            self.assertTrue(pre.allowed)

            res2 = _step_intake(
                grant_bytes=grant_bytes,
                status_bytes=status_active,
                job_id='job-1',
                grant_id='grant-1',
                source_ref=copy.deepcopy(self.source_ref),
                style_id='viralpop',
                style_overrides={},
                review_destination=copy.deepcopy(self.review_destination),
                allowed_actions=['intake_copy', 'render_candidate'],
                allowed_processors=['groq'],
                retry_allowance=2,
                source_path=self.source_path,
                trusted_source_root=self.source_root,
                base_staging_dir=self.base_staging_dir,
                trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
                trusted_status_keys={('status', 's1'): self.status_key.public_key()},
                now=NOW,
                now_iso=NOW_ISO,
                existing_binding=existing,
            )
            self.assertNotIsInstance(res2, OrchestratorResult)
            spec, checkpoint = res2

            checkpoint = _step_to_authorized(
                spec=spec,
                checkpoint=checkpoint,
                now_iso=NOW_ISO,
            )
            self.assertNotIsInstance(checkpoint, OrchestratorResult)

            res4 = _step_transcript_g1(
                spec=spec,
                checkpoint=checkpoint,
                transcript_data={'text': SCRIPT},
                script_text=SCRIPT,
                script_lines=LINES,
                transcript_tool_metadata=make_transcript_meta(),
                now_iso=NOW_ISO,
            )
            self.assertNotIsInstance(res4, OrchestratorResult)
            checkpoint, ids4 = res4

            res5 = _step_mfa_g2(
                spec=spec,
                checkpoint=checkpoint,
                mfa_artifact_bytes=make_mfa_bytes(),
                expected_script_sha256=hashlib.sha256(SCRIPT.encode('utf-8')).hexdigest(),
                expected_audio_sha256='d' * 64,
                media_duration_seconds=60.0,
                script_text=SCRIPT,
                mfa_tool_metadata=make_mfa_meta(),
                now_iso=NOW_ISO,
            )
            self.assertNotIsInstance(res5, OrchestratorResult)
            checkpoint, g2_record, ids5 = res5
            self.assertEqual(checkpoint.current_state, 'ALIGNED')

            status_revoked = sign(
                self.make_status(status='revoked'), self.status_key, STATUS_DOMAIN
            )
            res6 = _step_reverify_g3(
                spec=spec,
                checkpoint=checkpoint,
                grant_bytes=grant_bytes,
                status_bytes=status_revoked,
                trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
                trusted_status_keys={('status', 's1'): self.status_key.public_key()},
                now=NOW,
                script_text=SCRIPT,
                cues=make_cues(LINES),
                mfa_evidence=g2_record,
                now_iso=NOW_ISO,
            )

            self.assertIsInstance(res6, OrchestratorResult)
            self.assertFalse(res6.success)
            self.assertEqual(res6.final_state, 'CANCELLED')
            self.assertEqual(res6.deny_code, 'AUTH_REVOKED')
            self.assertFalse(res6.renderer_invoked)
            mock_run.assert_not_called()

            job_dir = Path(spec.staging.job_work_dir).parent
            disk_checkpoint = load_checkpoint(job_dir / 'checkpoint.json', expected_job_id='job-1')
            self.assertIsNotNone(disk_checkpoint)
            self.assertEqual(disk_checkpoint.current_state, 'CANCELLED')
            self.assertEqual(disk_checkpoint.state_revision, 6)
            self._assert_zero_renders(res6, mock_run)

    def test_orchestrate_stale_revocation_between_stages_zero_renders(self):
        # Entrypoint orchestrate_render_job takes a single status_bytes parameter,
        # so it cannot simulate a grant status becoming stale before Gate G3.
        # We drive step helpers 1-5 directly with active status, then invoke _step_reverify_g3
        # with a status checked_at older than 5 minutes.
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            grant_bytes = sign(self.make_grant(), self.grant_key, GRANT_DOMAIN)
            status_active = sign(self.make_status(), self.status_key, STATUS_DOMAIN)
            existing = _resolve_existing_binding(
                base_staging_dir=self.base_staging_dir,
                grant_id='grant-1',
                caller_binding=None,
            )

            pre = _step_pre_verify(
                grant_bytes=grant_bytes,
                status_bytes=status_active,
                trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
                trusted_status_keys={('status', 's1'): self.status_key.public_key()},
                now=NOW,
                job_id='job-1',
                source_ref=copy.deepcopy(self.source_ref),
                style_id='viralpop',
                style_overrides={},
                review_destination=copy.deepcopy(self.review_destination),
                existing_binding=existing,
            )
            self.assertNotIsInstance(pre, OrchestratorResult)
            self.assertTrue(pre.allowed)

            res2 = _step_intake(
                grant_bytes=grant_bytes,
                status_bytes=status_active,
                job_id='job-1',
                grant_id='grant-1',
                source_ref=copy.deepcopy(self.source_ref),
                style_id='viralpop',
                style_overrides={},
                review_destination=copy.deepcopy(self.review_destination),
                allowed_actions=['intake_copy', 'render_candidate'],
                allowed_processors=['groq'],
                retry_allowance=2,
                source_path=self.source_path,
                trusted_source_root=self.source_root,
                base_staging_dir=self.base_staging_dir,
                trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
                trusted_status_keys={('status', 's1'): self.status_key.public_key()},
                now=NOW,
                now_iso=NOW_ISO,
                existing_binding=existing,
            )
            self.assertNotIsInstance(res2, OrchestratorResult)
            spec, checkpoint = res2

            checkpoint = _step_to_authorized(
                spec=spec,
                checkpoint=checkpoint,
                now_iso=NOW_ISO,
            )
            self.assertNotIsInstance(checkpoint, OrchestratorResult)

            res4 = _step_transcript_g1(
                spec=spec,
                checkpoint=checkpoint,
                transcript_data={'text': SCRIPT},
                script_text=SCRIPT,
                script_lines=LINES,
                transcript_tool_metadata=make_transcript_meta(),
                now_iso=NOW_ISO,
            )
            self.assertNotIsInstance(res4, OrchestratorResult)
            checkpoint, ids4 = res4

            res5 = _step_mfa_g2(
                spec=spec,
                checkpoint=checkpoint,
                mfa_artifact_bytes=make_mfa_bytes(),
                expected_script_sha256=hashlib.sha256(SCRIPT.encode('utf-8')).hexdigest(),
                expected_audio_sha256='d' * 64,
                media_duration_seconds=60.0,
                script_text=SCRIPT,
                mfa_tool_metadata=make_mfa_meta(),
                now_iso=NOW_ISO,
            )
            self.assertNotIsInstance(res5, OrchestratorResult)
            checkpoint, g2_record, ids5 = res5
            self.assertEqual(checkpoint.current_state, 'ALIGNED')

            status_stale = sign(
                self.make_status(checked_at=ts(NOW - timedelta(minutes=5, seconds=1))),
                self.status_key,
                STATUS_DOMAIN,
            )
            res6 = _step_reverify_g3(
                spec=spec,
                checkpoint=checkpoint,
                grant_bytes=grant_bytes,
                status_bytes=status_stale,
                trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
                trusted_status_keys={('status', 's1'): self.status_key.public_key()},
                now=NOW,
                script_text=SCRIPT,
                cues=make_cues(LINES),
                mfa_evidence=g2_record,
                now_iso=NOW_ISO,
            )

            self.assertIsInstance(res6, OrchestratorResult)
            self.assertFalse(res6.success)
            self.assertEqual(res6.final_state, 'BLOCKED_AUTHORIZATION')
            self.assertEqual(res6.deny_code, 'REVOCATION_STALE_OR_MISMATCH')
            self.assertFalse(res6.renderer_invoked)
            mock_run.assert_not_called()

            job_dir = Path(spec.staging.job_work_dir).parent
            disk_checkpoint = load_checkpoint(job_dir / 'checkpoint.json', expected_job_id='job-1')
            self.assertIsNotNone(disk_checkpoint)
            self.assertEqual(disk_checkpoint.current_state, 'BLOCKED_AUTHORIZATION')
            self.assertEqual(disk_checkpoint.state_revision, 6)
            self._assert_zero_renders(res6, mock_run)

    def test_orchestrate_cue_coverage_gap_gate_g3_zero_renders(self):
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            result = self.run_job(cues=make_cues(LINES[:1]))

            self._assert_zero_renders(result, mock_run)
            self.assertFalse(result.success)
            self.assertEqual(result.deny_code, 'MFA_EVIDENCE_INVALID')
            self.assertEqual(result.final_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertFalse(result.renderer_invoked)
            self.assertIsNotNone(result.checkpoint)
            self.assertEqual(result.checkpoint.current_state, 'BLOCKED_NEEDS_REVIEW')
            self.assertEqual(result.checkpoint.blocked_from_state, 'ALIGNED')
            self.assertEqual(result.checkpoint.state_revision, 6)

    @unittest.skipUnless(
        shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None,
        'FFmpeg and ffprobe must be installed on host to run synthetic render test',
    )
    def test_synthetic_offline_one_clip_render_when_ffmpeg_available(self):
        # Note: This test uses the standard 2-line fixture (SCRIPT/LINES/TOKENS/MFA)
        # rather than a 2-word script to ensure Gates G1/G2/G3 pass with proven fixtures.
        # The purpose of this test is to verify the live integration seam with the real
        # renderer adapter and burner subprocess, not novel script alignment.
        # Subprocess execution of the burner is NOT mocked here.
        synth_path = self.source_root / 'synth-source.mp4'
        subprocess.run(
            [
                'ffmpeg',
                '-y',
                '-f',
                'lavfi',
                '-i',
                'testsrc2=size=320x240:rate=30',
                '-f',
                'lavfi',
                '-i',
                'sine=frequency=1000:sample_rate=44100',
                '-t',
                '3.0',
                '-c:v',
                'libx264',
                '-pix_fmt',
                'yuv420p',
                '-c:a',
                'aac',
                str(synth_path),
            ],
            check=True,
            capture_output=True,
        )

        synth_sha = hashlib.sha256(synth_path.read_bytes()).hexdigest()
        synth_ref = dict(provider='test', id='source-1', revision='r1', sha256=synth_sha)

        result = self.run_job(
            grant_bytes=sign(self.make_grant(source_ref=synth_ref), self.grant_key, GRANT_DOMAIN),
            source_ref=synth_ref,
            source_path=synth_path,
            media_duration_seconds=3.0,
            auto_mode=True,
        )

        self.assertTrue(result.success)
        self.assertTrue(result.renderer_invoked)
        self.assertIsNone(result.deny_code)
        self.assertIsNotNone(result.checkpoint)
        # P2-009 section 5.4: after a successful real render the
        # entrypoint runs real run_qc fresh, so the job ends at
        # READY_FOR_REVIEW (revision 9: 7 render transitions + 2 QC).
        self.assertEqual(result.checkpoint.current_state, 'READY_FOR_REVIEW')
        self.assertEqual(result.checkpoint.state_revision, 9)
        self.assertIn('ev_qc_job-1', result.evidence_ids)
        job_dir = Path(result.spec.staging.job_work_dir).parent
        self.assertTrue((job_dir / 'evidence' / 'ev_qc_job-1.json').is_file())
        events = load_event_log(job_dir / 'events.jsonl')
        self.assertEqual(len(events), 9)
        self.assertEqual(events[-1].next_state, 'READY_FOR_REVIEW')

        candidate = Path(result.spec.staging.candidate_output_path)
        self.assertTrue(candidate.is_file())
        self.assertGreater(candidate.stat().st_size, 0)
        self.assertEqual(
            result.render_execution.output_sha256,
            hashlib.sha256(candidate.read_bytes()).hexdigest(),
        )

        probe = subprocess.run(
            [
                'ffprobe',
                '-v',
                'error',
                '-show_entries',
                'format=duration',
                '-of',
                'csv=p=0',
                str(candidate),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        duration = float(probe.stdout.strip())
        self.assertGreater(duration, 0)

    # --- P2-009 section 8.2: retry loop (7 new tests) ---

    # 1. A retryable render failure retries once and then succeeds.
    def test_retryable_render_failure_retries_and_succeeds(self):
        sleeps = []
        source_before = hashlib.sha256(
            self.source_path.read_bytes(),
        ).hexdigest()
        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
        ):
            self._mock_render_fail_then_succeed(mock_run, fail_count=1)
            self._mock_qc_pass(mock_qc)
            result = self.run_job(auto_mode=True, sleep_fn=sleeps.append)

            self.assertTrue(result.success)
            self.assertEqual(result.final_state, 'READY_FOR_REVIEW')
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(mock_qc.call_count, 1)
            self.assertEqual(
                result.checkpoint.attempt_counts['render_candidate'], 2,
            )
            # Backoff after attempt 1 is 2-3 seconds (counts=1).
            self.assertEqual(len(sleeps), 1)
            self.assertGreaterEqual(sleeps[0], 2.0)
            self.assertLessEqual(sleeps[0], 3.0)

        job_dir = Path(result.spec.staging.job_work_dir).parent
        evidence_dir = job_dir / 'evidence'
        for n, expected_result, expected_code in (
            (1, 'FAILED', 'RENDER_EXECUTION_FAILED'),
            (2, 'SUCCESS', 'NO_FAILURE'),
        ):
            ev_file = evidence_dir / f'retry_render_candidate_{n:03d}.json'
            self.assertTrue(ev_file.is_file())
            ev = json.loads(ev_file.read_text(encoding='utf-8'))
            self.assertEqual(
                ev['event_id'], f'evt_retry_render_candidate_{n:03d}',
            )
            self.assertEqual(ev['attempt_count'], n)
            self.assertEqual(ev['result'], expected_result)
            self.assertEqual(ev['failure_code'], expected_code)
        # The retry loop never modifies the staged source.
        staged = Path(result.spec.staging.staged_input_path)
        self.assertEqual(
            hashlib.sha256(staged.read_bytes()).hexdigest(),
            result.spec.source.sha256,
        )
        self.assertEqual(
            hashlib.sha256(self.source_path.read_bytes()).hexdigest(),
            source_before,
        )

    # 2. Three failures in a row exhaust the quota: FAILED_JOB, no 4th render.
    def test_exhausted_retries_go_to_failed_job(self):
        sleeps = []
        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
        ):
            mock_run.return_value = Mock(returncode=1, stdout='', stderr='boom')
            result = self.run_job(auto_mode=True, sleep_fn=sleeps.append)

            self.assertFalse(result.success)
            self.assertEqual(result.final_state, 'FAILED_JOB')
            self.assertEqual(result.deny_code, 'RENDER_EXECUTION_FAILED')
            self.assertEqual(mock_run.call_count, 3)
            self.assertEqual(mock_qc.call_count, 0)
            self.assertEqual(
                result.checkpoint.attempt_counts['render_candidate'], 3,
            )
            # Two backoffs: 2-3 s after attempt 1, 4-5 s after attempt 2.
            self.assertEqual(len(sleeps), 2)
            self.assertGreaterEqual(sleeps[0], 2.0)
            self.assertLessEqual(sleeps[0], 3.0)
            self.assertGreaterEqual(sleeps[1], 4.0)
            self.assertLessEqual(sleeps[1], 5.0)

        job_dir = Path(result.spec.staging.job_work_dir).parent
        events = load_event_log(job_dir / 'events.jsonl')
        failed = [e for e in events if e.next_state == 'FAILED_JOB']
        self.assertEqual(len(failed), 1)
        self.assertIn('retry:render_candidate:attempts=3', failed[0].evidence_refs)
        self.assertIn('retry:render_candidate:exhausted', failed[0].evidence_refs)
        evidence_dir = job_dir / 'evidence'
        for n in (1, 2, 3):
            self.assertTrue(
                (evidence_dir / f'retry_render_candidate_{n:03d}.json').is_file(),
            )

    # 3. A revocation between retries cancels the job immediately.
    def test_revoked_between_retries_cancels_job(self):
        render_candidate_calls = {'n': 0}

        def fake_verify(*args, **kwargs):
            if kwargs.get('action') == 'render_candidate':
                render_candidate_calls['n'] += 1
                if render_candidate_calls['n'] == 2:
                    return Decision(False, 'AUTH_REVOKED')
            return Decision(True, 'OK')

        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.verify', side_effect=fake_verify),
        ):
            mock_run.return_value = Mock(returncode=1, stdout='', stderr='boom')
            result = self.run_job(auto_mode=True, sleep_fn=lambda _s: None)

            self.assertFalse(result.success)
            self.assertEqual(result.final_state, 'CANCELLED')
            self.assertEqual(result.deny_code, 'AUTH_REVOKED')
            # Only attempt 1 rendered; the revocation cancelled the job
            # before attempt 2's render.
            self.assertEqual(mock_run.call_count, 1)
            self.assertEqual(
                result.checkpoint.attempt_counts['render_candidate'], 1,
            )

        job_dir = Path(result.spec.staging.job_work_dir).parent
        events = load_event_log(job_dir / 'events.jsonl')
        self.assertEqual(events[-1].next_state, 'CANCELLED')

    # 4. verify() is called fresh once per attempt.
    def test_reverify_before_every_retry(self):
        render_candidate_calls = {'n': 0}

        def fake_verify(*args, **kwargs):
            if kwargs.get('action') == 'render_candidate':
                render_candidate_calls['n'] += 1
            return Decision(True, 'OK')

        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
            patch('subtitle.core.orchestrator.verify', side_effect=fake_verify),
        ):
            self._mock_render_fail_then_succeed(mock_run, fail_count=2)
            self._mock_qc_pass(mock_qc)
            result = self.run_job(auto_mode=True, sleep_fn=lambda _s: None)

            self.assertTrue(result.success)
            self.assertEqual(result.final_state, 'READY_FOR_REVIEW')
            self.assertEqual(mock_run.call_count, 3)
            # One fresh render_candidate verification per attempt: G3,
            # the attempt-2 re-verify, the attempt-3 re-verify, and the
            # QC step's fresh verification (4 in total, at least 3).
            self.assertGreaterEqual(render_candidate_calls['n'], 3)
            self.assertEqual(
                result.checkpoint.attempt_counts['render_candidate'], 3,
            )

    # 5. run_qc is re-run fresh after a retried render (never reused).
    def test_qc_rerun_fresh_after_retry_render(self):
        qc_first = QCResult(
            status='BLOCKED',
            report=None,
            evidence_record=Mock(),
            evidence_id='ev_qc_job-1-a',
            target_state='FAILED_JOB',
            reason='QC system fault (mocked): probe failed',
            unsatisfied_checks=('probe_failed',),
        )
        qc_second = QCResult(
            status='REVIEW_CANDIDATE',
            report=None,
            evidence_record=Mock(),
            evidence_id='ev_qc_job-1-b',
            target_state='READY_FOR_REVIEW',
            reason='QC passed (mocked)',
            unsatisfied_checks=(),
        )
        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
        ):
            self._mock_render_success(mock_run)
            mock_qc.side_effect = [qc_first, qc_second]
            result = self.run_job(auto_mode=True, sleep_fn=lambda _s: None)

            self.assertTrue(result.success)
            self.assertEqual(result.final_state, 'READY_FOR_REVIEW')
            self.assertEqual(mock_qc.call_count, 2)
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(
                result.checkpoint.attempt_counts['render_candidate'], 2,
            )
            # The second QC result is a fresh one, never a reuse of the first.
            self.assertNotEqual(qc_first.evidence_id, qc_second.evidence_id)
            self.assertIn('ev_qc_job-1-a', result.evidence_ids)
            self.assertIn('ev_qc_job-1-b', result.evidence_ids)

    # 6. Attempt counts survive a restart (loaded from disk, not reset).
    def test_retry_counts_survive_restart(self):
        # Run the job to RENDER_PERMITTED (no render yet), then pretend
        # a previous run already made 2 attempts: replace the checkpoint
        # on disk with counts=2 and reload it (a real restart).
        first = self.run_job(auto_mode=False)
        self.assertEqual(first.checkpoint.current_state, 'RENDER_PERMITTED')
        spec = first.spec
        job_dir = Path(spec.staging.job_work_dir).parent
        resumed = replace(
            first.checkpoint,
            attempt_counts=MappingProxyType({'render_candidate': 2}),
        )
        save_checkpoint(job_dir / 'checkpoint.json', resumed)
        loaded = load_checkpoint(job_dir / 'checkpoint.json')
        self.assertEqual(loaded.attempt_counts['render_candidate'], 2)

        grant_bytes = sign(self.make_grant(), self.grant_key, GRANT_DOMAIN)
        status_bytes = sign(self.make_status(), self.status_key, STATUS_DOMAIN)
        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout='', stderr='boom')
            result = _step_auto_mode_render(
                spec=spec,
                checkpoint=loaded,
                script_text=SCRIPT,
                mfa_artifact_bytes=make_mfa_bytes(),
                fonts_dir=self.fonts_dir,
                auto_mode=True,
                evidence_ids=(
                    'ev_transcript_job-1', 'ev_g1_job-1',
                    'ev_g2_job-1', 'ev_g3_job-1',
                ),
                now_iso=NOW_ISO,
                now=NOW,
                grant_bytes=grant_bytes,
                status_bytes=status_bytes,
                trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
                trusted_status_keys={('status', 's1'): self.status_key.public_key()},
                sleep_fn=lambda _s: None,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.final_state, 'FAILED_JOB')
        self.assertEqual(result.deny_code, 'RENDER_EXECUTION_FAILED')
        # Only attempt 3 ran (not a fresh attempt 1): the quota was
        # already at 2, so one more failure exhausts it.
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(
            result.checkpoint.attempt_counts['render_candidate'], 3,
        )
        reloaded = load_checkpoint(job_dir / 'checkpoint.json')
        self.assertEqual(reloaded.attempt_counts['render_candidate'], 3)
        self.assertTrue(
            (job_dir / 'evidence' / 'retry_render_candidate_003.json').is_file(),
        )

    # 7. Backoff is recorded by the injected sleep function, never slept for real.
    def test_backoff_recorded_not_slept_in_tests(self):
        sleeps = []
        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
        ):
            self._mock_render_fail_then_succeed(mock_run, fail_count=2)
            self._mock_qc_pass(mock_qc)
            result = self.run_job(auto_mode=True, sleep_fn=sleeps.append)

            self.assertTrue(result.success)
            self.assertEqual(result.final_state, 'READY_FOR_REVIEW')
            self.assertEqual(mock_run.call_count, 3)
            self.assertEqual(len(sleeps), 2)
            self.assertGreaterEqual(sleeps[0], 2.0)
            self.assertLessEqual(sleeps[0], 3.0)
            self.assertGreaterEqual(sleeps[1], 4.0)
            self.assertLessEqual(sleeps[1], 5.0)


if __name__ == '__main__':
    unittest.main()

