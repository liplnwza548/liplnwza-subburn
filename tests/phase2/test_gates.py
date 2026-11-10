"""Offline P2-005 tests: synthetic MFA artifacts and forged STT-as-MFA; no real audio, no renderer calls."""

import hashlib
import json
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest
import unittest.mock

from pythainlp import word_tokenize

from subtitle.core.authorization import Decision
from subtitle.core.evidence import (
    CheckRecord,
    EvidenceError,
    EvidenceRecord,
    create_evidence_record,
    load_evidence_record,
    save_evidence_record,
)
from subtitle.core.gates import (
    GateDecision,
    MFA_EVIDENCE_INVALID,
    SCRIPT_NEEDS_EVIDENCE,
    check_mfa_provenance,
    check_pre_render_gate,
    check_transcript_evidence,
    load_gate_evidence,
)
from subtitle.core.job import (
    JobSpec,
    SourceSnapshot,
    StagingPaths,
    create_job_spec,
)
from subtitle.core.state import (
    JobCheckpoint,
    RENDER_NOT_PERMITTED,
    reduce_transition,
)

NOW = '2026-11-10T12:00:00Z'
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


def make_spec(*, job_id='job-1', grant_id='grant-1') -> JobSpec:
    return create_job_spec(
        job_id=job_id,
        grant_id=grant_id,
        source=SourceSnapshot(
            'test', 'source-1', 'r1', '0' * 64, 10, '/synthetic/source.mp4'
        ),
        style_id='viralpop',
        style_overrides={},
        allowed_actions=['intake_copy', 'mfa_align', 'render_candidate'],
        allowed_processors=['groq'],
        review_destination={'provider': 'test', 'id': 'review-1', 'owner_id': 'philip'},
        retry_allowance=2,
        staging=StagingPaths(
            '/synthetic/work',
            '/synthetic/work/source.mp4',
            '/synthetic/output',
            '/synthetic/output/candidate.mp4',
        ),
        created_at=NOW,
    )


def make_mfa_meta(**overrides) -> dict:
    meta = {
        'tool': 'thai_mfa',
        'tool_version': '0.1.0',
        'method': 'synthetic_stub',
        'command': ['mfa', 'align'],
        'exit_code': 0,
        'captured_at': NOW,
    }
    meta.update(overrides)
    return meta


def make_checkpoint(state='ALIGNED', revision=4, event_id='evt-aligned-1', **kwargs) -> JobCheckpoint:
    return JobCheckpoint(
        '1',
        kwargs.get('job_id', 'job-1'),
        kwargs.get('grant_id', 'grant-1'),
        state,
        revision,
        event_id,
        MappingProxyType({}),
        NOW,
        kwargs.get('blocked_from_state', None),
    )


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


def valid_g2(**overrides) -> GateDecision:
    words = overrides.get('words', TOKENS)
    durations = overrides.get('durations', DEFAULT_DURATIONS)
    starts = overrides.get('starts', None)
    if 'mfa_artifact_bytes' in overrides:
        artifact_bytes = overrides['mfa_artifact_bytes']
    else:
        artifact_bytes = make_mfa_bytes(words, durations, starts=starts)

    spec = overrides.get('spec', make_spec())
    script_text = overrides.get('script_text', SCRIPT)
    expected_script_sha256 = overrides.get(
        'expected_script_sha256',
        hashlib.sha256(script_text.encode('utf-8')).hexdigest(),
    )
    expected_audio_sha256 = overrides.get('expected_audio_sha256', 'd' * 64)
    media_duration_seconds = overrides.get('media_duration_seconds', 60.0)
    tool_metadata = overrides.get('tool_metadata', make_mfa_meta())
    artifact_relpath = overrides.get('artifact_relpath', 'work/mfa_out.json')
    evidence_id = overrides.get('evidence_id', 'ev-mfa-001')
    now = overrides.get('now', NOW)

    return check_mfa_provenance(
        spec=spec,
        mfa_artifact_bytes=artifact_bytes,
        expected_script_sha256=expected_script_sha256,
        expected_audio_sha256=expected_audio_sha256,
        media_duration_seconds=media_duration_seconds,
        script_text=script_text,
        tool_metadata=tool_metadata,
        artifact_relpath=artifact_relpath,
        evidence_id=evidence_id,
        now=now,
    )


class GateTests(unittest.TestCase):
    def test_valid_mfa_provenance_passes_gate_g2(self):
        decision = valid_g2()
        self.assertTrue(decision.passed)
        self.assertIsNone(decision.deny_code)
        self.assertIsNotNone(decision.evidence)
        self.assertIsInstance(decision.evidence, EvidenceRecord)
        self.assertEqual(decision.evidence.stage, 'ALIGNED')
        self.assertEqual(decision.evidence.media_duration_seconds, 60.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir) / 'job-1'
            saved_path = save_evidence_record(job_dir, decision.evidence)
            self.assertTrue(saved_path.is_file())
            loaded = load_gate_evidence(job_dir, decision.evidence.evidence_id, expected_job_id='job-1')
            self.assertEqual(loaded, decision.evidence)

    def test_forged_stt_as_mfa_rejected(self):
        stt_artifact = {
            'segments': [{'id': 0, 'text': SCRIPT}],
            'words': [{'word': w, 'probability': 0.9} for w in TOKENS],
        }
        artifact_bytes = json.dumps(stt_artifact, ensure_ascii=False).encode('utf-8')
        decision = valid_g2(mfa_artifact_bytes=artifact_bytes)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, MFA_EVIDENCE_INVALID)

    def test_interpolated_timing_rejected(self):
        durations = [0.250] * len(TOKENS)
        decision = valid_g2(durations=durations)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, MFA_EVIDENCE_INVALID)
        self.assertTrue(decision.reason.startswith('averaged-timing heuristic (best-effort):'))

    def test_timing_nan_or_inf_rejected(self):
        valid_raw = make_mfa_bytes(TOKENS, DEFAULT_DURATIONS).decode('utf-8')
        nan_bytes = valid_raw.replace('0.18', 'NaN', 1).encode('utf-8')
        inf_bytes = valid_raw.replace('0.18', 'Infinity', 1).encode('utf-8')

        res_nan = valid_g2(mfa_artifact_bytes=nan_bytes)
        self.assertFalse(res_nan.passed)
        self.assertEqual(res_nan.deny_code, MFA_EVIDENCE_INVALID)

        res_inf = valid_g2(mfa_artifact_bytes=inf_bytes)
        self.assertFalse(res_inf.passed)
        self.assertEqual(res_inf.deny_code, MFA_EVIDENCE_INVALID)

    def test_timing_negative_rejected(self):
        starts = [-0.5, 0.23, 0.60, 0.86, 1.18]
        decision = valid_g2(starts=starts)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, MFA_EVIDENCE_INVALID)

    def test_timing_overlapping_words_rejected(self):
        # Entry 0 ends at 0.18, so starting entry 1 at 0.10 causes overlap
        starts = [0.0, 0.10, 0.60, 0.86, 1.18]
        decision = valid_g2(starts=starts)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, MFA_EVIDENCE_INVALID)

    def test_timing_exceeding_media_duration_rejected(self):
        decision = valid_g2(media_duration_seconds=1.0)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, MFA_EVIDENCE_INVALID)

    def test_mfa_word_count_mismatch_rejected(self):
        decision = valid_g2(words=TOKENS[:4], durations=[0.18, 0.32, 0.21, 0.27])
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, MFA_EVIDENCE_INVALID)

    def test_mfa_unk_ambiguity_triggers_blocked_needs_review(self):
        unk_words = ['สวัสดี', 'ตอนเช้า', '<unk>', 'อากาศ', 'ดีมาก']
        decision = valid_g2(words=unk_words)
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, SCRIPT_NEEDS_EVIDENCE)
        self.assertIsNotNone(decision.evidence)
        self.assertIn('วันนี้', decision.reason)

    def test_tampered_script_hash_invalidates_gate(self):
        # (a) G2 with expected_script_sha256='e'*64
        g2_res = valid_g2(expected_script_sha256='e' * 64)
        self.assertFalse(g2_res.passed)
        self.assertEqual(g2_res.deny_code, MFA_EVIDENCE_INVALID)

        # (b) G3 with an EvidenceRecord whose input_digest_refs['script_sha256'] is 'e'*64
        spec = make_spec()
        tampered_rec = create_evidence_record(
            evidence_id='ev-mfa-tampered-script',
            job_id=spec.job_id,
            stage='ALIGNED',
            tool='thai_mfa',
            tool_version='0.1.0',
            method='synthetic_stub',
            command=['mfa', 'align'],
            input_digest_refs={
                'source_media_sha256': spec.source.sha256,
                'extracted_audio_sha256': 'd' * 64,
                'script_sha256': 'e' * 64,
            },
            artifact_sha256='a' * 64,
            artifact_relpath='work/mfa_out.json',
            captured_at=NOW,
            exit_code=0,
            media_duration_seconds=60.0,
            checks=(
                CheckRecord('test', 'PASSED', 0, 0, None, ''),
                CheckRecord('human_audio_verification', 'NOT_CHECKED', None, None, None, ''),
            ),
            notes='',
        )
        g3_res = check_pre_render_gate(
            spec=spec,
            checkpoint=make_checkpoint(),
            mfa_evidence=tampered_rec,
            script_text=SCRIPT,
            cues=make_cues(LINES),
            authorization_decision=Decision(True, 'AUTHORIZED'),
            evidence_id='ev-prerender-001',
            now=NOW,
        )
        self.assertFalse(g3_res.passed)
        self.assertEqual(g3_res.deny_code, MFA_EVIDENCE_INVALID)

    def test_tampered_media_hash_invalidates_gate(self):
        spec = make_spec()
        tampered_rec = create_evidence_record(
            evidence_id='ev-mfa-tampered-media',
            job_id=spec.job_id,
            stage='ALIGNED',
            tool='thai_mfa',
            tool_version='0.1.0',
            method='synthetic_stub',
            command=['mfa', 'align'],
            input_digest_refs={
                'source_media_sha256': 'f' * 64,
                'extracted_audio_sha256': 'd' * 64,
                'script_sha256': hashlib.sha256(SCRIPT.encode('utf-8')).hexdigest(),
            },
            artifact_sha256='a' * 64,
            artifact_relpath='work/mfa_out.json',
            captured_at=NOW,
            exit_code=0,
            media_duration_seconds=60.0,
            checks=(
                CheckRecord('test', 'PASSED', 0, 0, None, ''),
                CheckRecord('human_audio_verification', 'NOT_CHECKED', None, None, None, ''),
            ),
            notes='',
        )
        g3_res = check_pre_render_gate(
            spec=spec,
            checkpoint=make_checkpoint(),
            mfa_evidence=tampered_rec,
            script_text=SCRIPT,
            cues=make_cues(LINES),
            authorization_decision=Decision(True, 'AUTHORIZED'),
            evidence_id='ev-prerender-001',
            now=NOW,
        )
        self.assertFalse(g3_res.passed)
        self.assertEqual(g3_res.deny_code, MFA_EVIDENCE_INVALID)

    def test_cue_coverage_gap_or_overlap_rejected(self):
        g2 = valid_g2()
        self.assertTrue(g2.passed)

        # Gap: missing second line
        cues_gap = make_cues(LINES[:1])
        res_gap = check_pre_render_gate(
            spec=make_spec(),
            checkpoint=make_checkpoint(),
            mfa_evidence=g2.evidence,
            script_text=SCRIPT,
            cues=cues_gap,
            authorization_decision=Decision(True, 'AUTHORIZED'),
            evidence_id='ev-prerender-001',
            now=NOW,
        )
        self.assertFalse(res_gap.passed)
        self.assertEqual(res_gap.deny_code, MFA_EVIDENCE_INVALID)

        # Overlap: second cue starts inside the first
        cues_overlap = make_cues(LINES)
        cues_overlap[1]['source_char_start'] = cues_overlap[0]['source_char_end'] - 2
        res_overlap = check_pre_render_gate(
            spec=make_spec(),
            checkpoint=make_checkpoint(),
            mfa_evidence=g2.evidence,
            script_text=SCRIPT,
            cues=cues_overlap,
            authorization_decision=Decision(True, 'AUTHORIZED'),
            evidence_id='ev-prerender-001',
            now=NOW,
        )
        self.assertFalse(res_overlap.passed)
        self.assertEqual(res_overlap.deny_code, MFA_EVIDENCE_INVALID)

    def test_no_human_listening_reports_not_checked(self):
        spec = make_spec()
        g1_tool_meta = {
            'processor': 'groq',
            'model': 'whisper-large-v3-turbo',
            'model_version': 'v3',
            'method': 'synthetic_stub',
            'captured_at': NOW,
        }

        # Valid G1
        g1_valid = check_transcript_evidence(
            spec=spec,
            transcript_data={'text': SCRIPT},
            script_text=SCRIPT,
            script_lines=LINES,
            tool_metadata=g1_tool_meta,
            artifact_relpath='work/transcript.json',
            evidence_id='ev-transcript-001',
            now=NOW,
        )
        self.assertTrue(g1_valid.passed)

        # G1 mismatch case
        g1_mismatch = check_transcript_evidence(
            spec=spec,
            transcript_data={'text': 'สวัสดีตอนเย็น'},
            script_text=SCRIPT,
            script_lines=LINES,
            tool_metadata=g1_tool_meta,
            artifact_relpath='work/transcript.json',
            evidence_id='ev-transcript-002',
            now=NOW,
        )
        self.assertFalse(g1_mismatch.passed)
        self.assertEqual(g1_mismatch.deny_code, SCRIPT_NEEDS_EVIDENCE)

        # Valid G2
        g2_valid = valid_g2()
        self.assertTrue(g2_valid.passed)

        # Valid G3
        g3_valid = check_pre_render_gate(
            spec=spec,
            checkpoint=make_checkpoint(),
            mfa_evidence=g2_valid.evidence,
            script_text=SCRIPT,
            cues=make_cues(LINES),
            authorization_decision=Decision(True, 'AUTHORIZED'),
            evidence_id='ev-prerender-001',
            now=NOW,
        )
        self.assertTrue(g3_valid.passed)

        records = [g1_valid.evidence, g2_valid.evidence, g3_valid.evidence]
        for rec in records:
            self.assertIsNotNone(rec)
            self.assertIsInstance(rec, EvidenceRecord)
            human_checks = [c for c in rec.checks if c.name == 'human_audio_verification']
            self.assertEqual(len(human_checks), 1)
            self.assertEqual(human_checks[0].result, 'NOT_CHECKED')
            for c in rec.checks:
                if c.name == 'human_audio_verification':
                    self.assertNotEqual(c.result, 'PASSED')

    def test_gate_rejection_prevents_render_transition(self):
        """Pre-render gate rejection prevents calling state transition.

        Note: counting renderer invocations belongs to P2-006, not this test.
        """
        def caller_stub(gate_decision, checkpoint):
            if not gate_decision.passed:
                return checkpoint
            return reduce_transition(
                checkpoint,
                'RENDER_PERMITTED',
                event_id='evt-render-1',
                evidence_refs=(gate_decision.evidence.evidence_id,),
                actor='runner',
                decision='AUTHORIZED',
                now=NOW,
            )

        spec = make_spec()
        checkpoint = make_checkpoint()
        g2 = valid_g2()
        tampered_rec = create_evidence_record(
            evidence_id='ev-mfa-tampered-14',
            job_id=spec.job_id,
            stage='ALIGNED',
            tool='thai_mfa',
            tool_version='0.1.0',
            method='synthetic_stub',
            command=['mfa', 'align'],
            input_digest_refs={
                'source_media_sha256': spec.source.sha256,
                'extracted_audio_sha256': 'd' * 64,
                'script_sha256': 'e' * 64,
            },
            artifact_sha256=g2.evidence.artifact_sha256,
            artifact_relpath='work/mfa_out.json',
            captured_at=NOW,
            exit_code=0,
            media_duration_seconds=60.0,
            checks=(
                CheckRecord('test', 'PASSED', 0, 0, None, ''),
                CheckRecord('human_audio_verification', 'NOT_CHECKED', None, None, None, ''),
            ),
            notes='',
        )

        decision = check_pre_render_gate(
            spec=spec,
            checkpoint=checkpoint,
            mfa_evidence=tampered_rec,
            script_text=SCRIPT,
            cues=make_cues(LINES),
            authorization_decision=Decision(True, 'AUTHORIZED'),
            evidence_id='ev-prerender-001',
            now=NOW,
        )
        self.assertFalse(decision.passed)
        self.assertEqual(decision.deny_code, MFA_EVIDENCE_INVALID)

        mock_reduce = unittest.mock.Mock(side_effect=reduce_transition)
        with unittest.mock.patch('tests.phase2.test_gates.reduce_transition', mock_reduce):
            result_checkpoint = caller_stub(decision, checkpoint)

        self.assertEqual(mock_reduce.call_count, 0)
        self.assertEqual(result_checkpoint, checkpoint)

    def test_synthetic_stub_flag_validation(self):
        spec = make_spec()
        rec = create_evidence_record(
            evidence_id='ev-stub-valid',
            job_id=spec.job_id,
            stage='ALIGNED',
            tool='thai_mfa',
            tool_version='0.1.0',
            method='synthetic_stub',
            command=['mfa', 'align'],
            input_digest_refs={
                'source_media_sha256': spec.source.sha256,
                'script_sha256': 'a' * 64,
            },
            artifact_sha256='b' * 64,
            artifact_relpath='work/stub.json',
            captured_at=NOW,
            exit_code=0,
            media_duration_seconds=60.0,
            checks=(
                CheckRecord('test', 'PASSED', 0, 0, None, ''),
                CheckRecord('human_audio_verification', 'NOT_CHECKED', None, None, None, ''),
            ),
            notes='',
        )
        self.assertEqual(rec.method, 'synthetic_stub')

        with self.assertRaises(EvidenceError):
            create_evidence_record(
                evidence_id='ev-stub-invalid',
                job_id=spec.job_id,
                stage='ALIGNED',
                tool='thai_mfa',
                tool_version='0.1.0',
                method='inferred',
                command=['mfa', 'align'],
                input_digest_refs={
                    'source_media_sha256': spec.source.sha256,
                    'script_sha256': 'a' * 64,
                },
                artifact_sha256='b' * 64,
                artifact_relpath='work/stub.json',
                captured_at=NOW,
                exit_code=0,
                media_duration_seconds=60.0,
                checks=(
                    CheckRecord('test', 'PASSED', 0, 0, None, ''),
                    CheckRecord('human_audio_verification', 'NOT_CHECKED', None, None, None, ''),
                ),
                notes='',
            )


if __name__ == '__main__':
    unittest.main()
