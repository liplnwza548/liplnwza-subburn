"""P2-010 adversarial tests (plan sections 4 / 5.3, 7 new tests).

Offline only, synthetic data only, test keys labelled ``test-key-...``.
Each test names the threat-matrix row it proves in its docstring.

The seven tests cover the rows that had no test yet:

- A-20 clock boundary (grant expiry edge)
- B-13 forged human-listening claim (PASSED must be rejected)
- C-16 retry quota across restart, driven through the real entrypoint
- C-18 ``now`` must not change mid retry loop
- D-01 fake Drive parent that is a symlink
- D-02 review destination with owner_id off by one character
- E-02 direct CLI bypass proof (the CLIs work without the orchestrator)

No test here touches production code: these tests only drive the
public seams (``verify`` / ``create_evidence_record`` / the
orchestrator entrypoint / the adapters / the burner CLIs) and read
the real files they produce.
"""

import base64
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pythainlp import word_tokenize

from subtitle.adapters import AdapterError, ColabAdapter
from subtitle.core.audit import load_checkpoint, save_checkpoint
from subtitle.core.authorization import Decision, canonical, verify
from subtitle.core.evidence import (
    CheckRecord,
    EvidenceError,
    create_evidence_record,
)
from subtitle.core.orchestrator import (
    _step_auto_mode_render,  # noqa: F401  (re-exported seam, used by the C-18 source check)
    orchestrate_render_job,
)
from subtitle.core.qc import QCResult
from subtitle.core.renderer_adapter import prepare_mfa_words_timeline

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
NOW_ISO = '2026-11-10T12:00:00Z'
GRANT_DOMAIN = b'ThaiSubburn JobGrant v1\x00'
STATUS_DOMAIN = b'ThaiSubburn Revocation v1\x00'
TEST_KEY = 'test-key-adversarial-010'


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
    payload = {'tiers': {'words': {'entries': entries}}}
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


class AdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_root = self.root / 'trusted-source'
        self.base_staging_dir = self.root / 'staging'
        self.source_root.mkdir()
        self.source_path = self.source_root / 'synthetic.mp4'
        self.source_bytes = b'fake-video-bytes-p2-010\x00' * 4096
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
        self.fake_drive = self.root / 'fakedrive'
        self.fake_drive.mkdir()
        (self.fake_drive / 'parent-1').mkdir()

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

    def _verify_args(self, *, now=NOW, grant=None, status=None, **overrides):
        args = dict(
            trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
            trusted_status_keys={('status', 's1'): self.status_key.public_key()},
            now=now,
            job_id='job-1',
            source_ref=copy.deepcopy(self.source_ref),
            style_id='viralpop',
            style_overrides={},
            review_destination=copy.deepcopy(self.review_destination),
            action='render_candidate',
            processor=None,
            existing_binding=None,
        )
        args.update(overrides)
        grant_bytes = sign(
            grant if grant is not None else self.make_grant(),
            self.grant_key, GRANT_DOMAIN,
        )
        status_bytes = sign(
            status if status is not None else self.make_status(),
            self.status_key, STATUS_DOMAIN,
        )
        return grant_bytes, status_bytes, args

    def run_job(self, *, grant=None, status=None, **overrides):
        job_id = overrides.get('job_id', 'job-1')
        grant_id = overrides.get('grant_id', 'grant-1')
        style_id = overrides.get('style_id', 'viralpop')
        style_overrides = overrides.get('style_overrides', {})
        source_ref = overrides.get('source_ref', copy.deepcopy(self.source_ref))
        review_destination = overrides.get(
            'review_destination', copy.deepcopy(self.review_destination)
        )
        raw_grant = sign(
            grant if grant is not None else self.make_grant(
                job_id=job_id,
                grant_id=grant_id,
                style_id=style_id,
                style_overrides=style_overrides,
                source_ref=source_ref,
                review_destination=review_destination,
            ),
            self.grant_key, GRANT_DOMAIN,
        )
        raw_status = sign(
            status if status is not None else self.make_status(
                job_id=job_id,
                grant_id=grant_id,
            ),
            self.status_key, STATUS_DOMAIN,
        )
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
        args['script_sha256'] = hashlib.sha256(
            args['script_text'].encode('utf-8'),
        ).hexdigest()
        return orchestrate_render_job(raw_grant, raw_status, **args)

    def _mock_render_fail_then_succeed(self, mock_run, fail_count=1):
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
        mock_qc.return_value = QCResult(
            status='REVIEW_CANDIDATE',
            report=None,
            evidence_record=Mock(),
            evidence_id='ev_qc_job-1',
            target_state='READY_FOR_REVIEW',
            reason='QC passed (mocked)',
            unsatisfied_checks=(),
        )

    def _valid_evidence_kwargs(self, **overrides):
        kwargs = dict(
            evidence_id='ev-test-001',
            job_id='job-1',
            stage='TRANSCRIBED',
            tool='test-tool',
            tool_version='1',
            method='synthetic_stub',
            command=['test-tool', 'run'],
            input_digest_refs={
                'source_media_sha256': 'a' * 64,
                'script_sha256': 'b' * 64,
            },
            artifact_sha256='c' * 64,
            artifact_relpath='evidence/ev-test-001.json',
            captured_at=NOW_ISO,
            exit_code=0,
            checks=(
                CheckRecord(
                    'human_audio_verification', 'NOT_CHECKED',
                    None, None, None, '',
                ),
            ),
            media_duration_seconds=None,
            notes='',
        )
        kwargs.update(overrides)
        return kwargs

    # A-20: the grant must be judged by the real clock edge, not by
    # rounding: now exactly at expires_at is already expired, one
    # second before it is still valid, one second before issued_at
    # is not yet valid.
    def test_clock_boundary_expiry(self):
        # now == expires_at -> denied (issued <= now < expires).
        grant, status, args = self._verify_args(
            now=NOW,
            grant=self.make_grant(expires_at=ts(NOW)),
            status=self.make_status(checked_at=ts(NOW)),
        )
        decision = verify(grant, status, **args)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, 'AUTH_EXPIRED_OR_NOT_YET_VALID')

        # now == expires_at - 1s -> still allowed (status is fresh
        # relative to that same now).
        probe = NOW - timedelta(seconds=1)
        grant, status, args = self._verify_args(
            now=probe,
            grant=self.make_grant(expires_at=ts(NOW)),
            status=self.make_status(checked_at=ts(probe)),
        )
        self.assertTrue(verify(grant, status, **args).allowed)

        # now == issued_at -> allowed (issued <= now).
        grant, status, args = self._verify_args(
            now=NOW,
            grant=self.make_grant(
                issued_at=ts(NOW), expires_at=ts(NOW + timedelta(hours=1)),
            ),
            status=self.make_status(checked_at=ts(NOW)),
        )
        self.assertTrue(verify(grant, status, **args).allowed)

        # now == issued_at - 1s -> not yet valid.
        probe = NOW - timedelta(seconds=1)
        grant, status, args = self._verify_args(
            now=probe,
            grant=self.make_grant(
                issued_at=ts(NOW), expires_at=ts(NOW + timedelta(hours=1)),
            ),
            status=self.make_status(checked_at=ts(probe)),
        )
        decision = verify(grant, status, **args)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, 'AUTH_EXPIRED_OR_NOT_YET_VALID')

    # B-13: a forged "human listened" claim must be rejected at
    # record creation time. The enforcement lives in evidence.py
    # (exactly one human_audio_verification check, result NOT_CHECKED)
    # but no negative test proved it until this one (plan C3).
    def test_forged_passed_rejected(self):
        # 'PASSED' must raise.
        with self.assertRaises(EvidenceError):
            create_evidence_record(**self._valid_evidence_kwargs(
                checks=(
                    CheckRecord(
                        'human_audio_verification', 'PASSED',
                        None, None, None, '',
                    ),
                ),
            ))
        # A missing human check must raise.
        with self.assertRaises(EvidenceError):
            create_evidence_record(**self._valid_evidence_kwargs(checks=()))
        # A duplicated human check must raise.
        with self.assertRaises(EvidenceError):
            create_evidence_record(**self._valid_evidence_kwargs(
                checks=(
                    CheckRecord(
                        'human_audio_verification', 'NOT_CHECKED',
                        None, None, None, '',
                    ),
                    CheckRecord(
                        'human_audio_verification', 'NOT_CHECKED',
                        None, None, None, '',
                    ),
                ),
            ))
        # The honest record (exactly one NOT_CHECKED) still works.
        record = create_evidence_record(**self._valid_evidence_kwargs())
        self.assertEqual(record.checks[0].result, 'NOT_CHECKED')

    # C-16: retry quota across restart, driven through the real
    # entrypoint (not just the pure function). The honest result is
    # NOT what the plan's first draft expected, and this test pins
    # the real behaviour down so nobody can claim otherwise:
    # the entrypoint refuses to resume a job that is already past
    # INTAKE (_step_intake requires state INTAKE + revision 1), so
    # the restarted run is denied AUDIT_CORRUPT with ZERO renders
    # and the attempt count on disk is NEVER reset (it stays 2).
    # The security property (quota cannot be reset across a restart)
    # holds; the functional gap (a crashed mid-pipeline job cannot
    # be continued through the public entrypoint at all) is recorded
    # as a P1 observation in docs/phase2/review.md.
    def test_quota_preserved_but_entrypoint_cannot_resume_mid_pipeline(self):
        first = self.run_job(auto_mode=False)
        self.assertEqual(first.checkpoint.current_state, 'RENDER_PERMITTED')
        job_dir = Path(first.spec.staging.job_work_dir).parent
        resumed = replace(
            first.checkpoint,
            attempt_counts=MappingProxyType({'render_candidate': 2}),
        )
        save_checkpoint(job_dir / 'checkpoint.json', resumed)
        seeded = load_checkpoint(job_dir / 'checkpoint.json')
        self.assertEqual(seeded.attempt_counts['render_candidate'], 2)

        with patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout='', stderr='boom')
            result = self.run_job(auto_mode=True, sleep_fn=lambda _s: None)

        self.assertFalse(result.success)
        self.assertEqual(result.final_state, 'FAILED_JOB')
        self.assertEqual(result.deny_code, 'AUDIT_CORRUPT')
        # No render at all in the restarted run (fail-closed).
        mock_run.assert_not_called()
        # The count on disk was never reset to 1 (and never advanced
        # to 3 either, because no render happened).
        reloaded = load_checkpoint(job_dir / 'checkpoint.json')
        self.assertEqual(reloaded.attempt_counts['render_candidate'], 2)
        self.assertEqual(reloaded.current_state, 'RENDER_PERMITTED')

    # C-18: the retry loop must use one single ``now`` for every
    # fresh verify() call (plan D8 / P2-006 C12). If the loop read
    # a fresh clock mid-run, an expiry-boundary result could flip
    # between attempts. Proved two ways: (a) behaviour — record the
    # ``now`` argument of every verify() call during a 3-attempt
    # retry loop and require them all to equal the entrypoint's
    # ``now``; (b) source — the render step's own source contains
    # no real-clock call.
    def test_now_does_not_change_mid_loop(self):
        seen_nows: list = []
        real_verify = verify

        def recording_verify(*args, **kwargs):
            seen_nows.append(kwargs.get('now', args[5] if len(args) > 5 else None))
            return real_verify(*args, **kwargs)

        with (
            patch('subtitle.core.renderer_adapter.subprocess.run') as mock_run,
            patch('subtitle.core.orchestrator.run_qc') as mock_qc,
            patch('subtitle.core.orchestrator.verify', side_effect=recording_verify),
        ):
            self._mock_render_fail_then_succeed(mock_run, fail_count=2)
            self._mock_qc_pass(mock_qc)
            result = self.run_job(auto_mode=True, sleep_fn=lambda _s: None)

        self.assertTrue(result.success)
        self.assertEqual(result.final_state, 'READY_FOR_REVIEW')
        # Three render attempts happened, so verify() was called
        # fresh for every retry (at least 3 render_candidate calls).
        self.assertEqual(mock_run.call_count, 3)
        self.assertGreaterEqual(len(seen_nows), 3)
        # Every verify() in the run used the same single ``now``.
        self.assertTrue(all(seen == NOW for seen in seen_nows))

        # (b) The render step itself never reads the real clock.
        source = inspect.getsource(_step_auto_mode_render)
        self.assertNotIn('datetime.now(', source)
        self.assertNotIn('time.time(', source)

    # D-01: a fake Drive parent that is a symlink must be rejected.
    # safe_resolve_path rejects symlink components, so resolving the
    # source through the linked parent must fail on both roots and
    # the adapter must raise AdapterError (never follow the link).
    def test_drive_parent_symlink_rejected(self):
        real_drive = self.root / 'real-drive'
        real_drive.mkdir()
        (real_drive / 'parent-1').mkdir()
        clip = real_drive / 'parent-1' / 'clip.mp4'
        clip.write_bytes(b'synthetic-clip-bytes' * 10)

        link = self.root / 'linked-drive'
        link.symlink_to(real_drive, target_is_directory=True)
        self.assertTrue(link.is_symlink())

        adapter = ColabAdapter(
            fake_drive_root=link,
            fake_drive_parent_id='parent-1',
            fake_drive_credential=TEST_KEY,
        )
        source_ref = {
            'provider': 'drive',
            'id': 'clip-1',
            'revision': 'r1',
            'sha256': hashlib.sha256(clip.read_bytes()).hexdigest(),
        }
        with self.assertRaises(AdapterError):
            adapter.resolve_source(
                source_ref=source_ref,
                source_path=link / 'parent-1' / 'clip.mp4',
                trusted_source_root=self.source_root,
            )

        # Positive control: the same file through the real (unlinked)
        # root resolves fine, so the denial above is caused by the
        # symlink and nothing else.
        real_adapter = ColabAdapter(
            fake_drive_root=real_drive,
            fake_drive_parent_id='parent-1',
            fake_drive_credential=TEST_KEY,
        )
        resolved = real_adapter.resolve_source(
            source_ref=source_ref,
            source_path=real_drive / 'parent-1' / 'clip.mp4',
            trusted_source_root=self.source_root,
        )
        self.assertEqual(resolved, real_drive / 'parent-1' / 'clip.mp4')

    # D-02: a destination that is almost right but has owner_id off
    # by one character must be denied BEFORE a single byte is
    # written (rule A4: provider + id + owner_id must all match).
    def test_owner_id_off_by_one_rejected(self):
        artifact = self.root / 'candidate.mp4'
        artifact.write_bytes(b'fake-candidate-mp4-bytes' * 100)
        parent_dir = self.fake_drive / 'parent-1'
        before = sorted(p.name for p in parent_dir.iterdir())

        adapter = ColabAdapter(
            fake_drive_root=self.fake_drive,
            fake_drive_parent_id='parent-1',
            fake_drive_credential=TEST_KEY,
        )
        expected = {
            'provider': 'drive',
            'id': 'parent-1',
            'owner_id': 'philip',
        }
        almost = dict(expected, owner_id='phillip')  # one extra 'l'
        with self.assertRaises(AdapterError) as ctx:
            adapter.deliver_review(
                job_id='job-1',
                artifact_path=artifact,
                destination=almost,
                expected_destination=expected,
                target_state='READY_FOR_REVIEW',
                evidence_id='ev_qc_job-1',
                now=NOW_ISO,
            )
        self.assertEqual(ctx.exception.code, 'ADAPTER_DESTINATION_MISMATCH')
        # Nothing was written into the Drive parent.
        self.assertEqual(sorted(p.name for p in parent_dir.iterdir()), before)

        # Positive control: the exact destination delivers.
        receipt = adapter.deliver_review(
            job_id='job-1',
            artifact_path=artifact,
            destination=expected,
            expected_destination=expected,
            target_state='READY_FOR_REVIEW',
            evidence_id='ev_qc_job-1',
            now=NOW_ISO,
        )
        self.assertEqual(receipt.destination_owner_id, 'philip')
        # artifact_relpath is relative to the fake Drive root.
        self.assertTrue((self.fake_drive / receipt.artifact_relpath).is_file())

    # E-02: direct CLI bypass proof. The burner CLIs work when
    # called straight from the shell, with no grant, no verify(),
    # no orchestrator and no gates involved at all. This test MUST
    # pass to prove the limitation is real: the orchestrator is
    # the guarded door, not the only door. Recorded as known
    # limitation L2 in docs/phase2/review.md (Spec section 10:
    # offline tests prove policy behaviour at the software
    # interface, not that an agent with arbitrary shell access is
    # unable to bypass it).
    @unittest.skipUnless(
        shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None,
        'FFmpeg and ffprobe must be installed on host to run the direct-CLI test',
    )
    def test_direct_cli_bypasses_orchestrator(self):
        import importlib.util

        from subtitle.viralpop.render.script_pipeline import _cli as viralpop_cli
        # paper-chips is a hyphenated directory, not an importable
        # package: load chip_burn.py straight from its file path,
        # exactly the way the shell would run it (python chip_burn.py).
        chip_spec = importlib.util.spec_from_file_location(
            'chip_burn_direct',
            Path(__file__).resolve().parents[2]
            / 'subtitle' / 'paper-chips' / 'chip_burn.py',
        )
        chip_mod = importlib.util.module_from_spec(chip_spec)
        chip_spec.loader.exec_module(chip_mod)
        chip_main = chip_mod.main

        clip = self.source_root / 'synth-source.mp4'
        subprocess.run(
            [
                'ffmpeg', '-y',
                '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=44100',
                '-t', '3.0',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                str(clip),
            ],
            check=True,
            capture_output=True,
        )
        script_path = self.root / 'script.txt'
        script_path.write_text('\n'.join(LINES) + '\n', encoding='utf-8')
        words = prepare_mfa_words_timeline(make_mfa_bytes(), SCRIPT)
        words_path = self.root / 'words.json'
        words_path.write_text(
            json.dumps(words, ensure_ascii=False), encoding='utf-8',
        )

        # (1) viralpop CLI straight from argv, no orchestrator.
        viralpop_out = self.root / 'direct-viralpop.mp4'
        with patch.object(sys, 'argv', [
            'burn_script_aligned.py',
            '--video', str(clip),
            '--script', str(script_path),
            '--fonts', str(self.fonts_dir),
            '--out', str(viralpop_out),
            '--words-json', str(words_path),
            '--profile', 'vo',
        ]):
            exit_code = viralpop_cli()
        self.assertEqual(exit_code, 0)
        self.assertTrue(viralpop_out.is_file())
        self.assertGreater(viralpop_out.stat().st_size, 0)

        # (2) paper-chips CLI straight from argv, no orchestrator.
        chips_out = self.root / 'direct-chips.mp4'
        with patch.object(sys, 'argv', [
            'chip_burn.py',
            '--video', str(clip),
            '--script', str(script_path),
            '--stt', str(words_path),
            '--out', str(chips_out),
            '--fps', '30',
            '--crf', '18',
        ]):
            chip_main()
        self.assertTrue(chips_out.is_file())
        self.assertGreater(chips_out.stat().st_size, 0)


if __name__ == '__main__':
    unittest.main()
