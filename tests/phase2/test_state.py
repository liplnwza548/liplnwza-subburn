"""Offline P2-004 tests: synthetic bytes and ephemeral, in-memory signing keys.

Covers the pure state reducer (state.py) and the atomic JSON audit store
(audit.py). INVALID_STATE_REVISION is reserved for the audit layer: the
reducer takes no expected revision, so out-of-order revisions are caught by
load_event_log raising AuditError (see test_event_log_revision_gap_rejected).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from subtitle.core.audit import (
    EVENT_LOG_FILENAME, JOB_SPEC_FILENAME, LEDGER_FILENAME,
    AuditError, ReplayLedger, append_event, atomic_write_file,
    atomic_write_json, load_checkpoint, load_event_log, load_job_record,
    load_job_spec, save_checkpoint, save_job_spec,
)
from subtitle.core.job import (
    JobSpecError, SourceSnapshot, StagingPaths, create_job_spec,
)
from subtitle.core.state import (
    CONTENT_EVIDENCE_AMBIGUOUS, CORRECTION_EVIDENCE_REQUIRED,
    IDEMPOTENT_NOOP, INVALID_STATE_REVISION, REVOKED_OR_CANCELLED_JOB,
    RENDER_NOT_PERMITTED, SCRIPT_EVIDENCE_UNCHECKED, STATE_SKIP_FORBIDDEN,
    TERMINAL_STATE_LOCKED, TRANSITION_ACCEPTED, CheckpointError,
    JobCheckpoint, JobEvent, increment_attempt, initial_checkpoint,
    is_terminal_state, reduce_transition, serialize_checkpoint,
    deserialize_checkpoint, serialize_event, deserialize_event,
    validate_transition,
)

from subtitle.core.audit import CHECKPOINT_FILENAME
import base64
import copy
import hashlib
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from subtitle.core import intake
from subtitle.core.authorization import canonical
from subtitle.core.intake import (
    JOB_STATE_CONFLICT, allocate_staging_paths, compute_file_sha256,
    copy_source_to_staging, process_job_intake,
)

NOW = '2026-11-10T12:00:00Z'

def make_spec(*, job_id='job-1', grant_id='grant-1'):
    return create_job_spec(
        job_id=job_id, grant_id=grant_id,
        source=SourceSnapshot('test', 'source-1', 'r1', '0' * 64, 10, '/synthetic/source.mp4'),
        style_id='viralpop', style_overrides={}, allowed_actions=['intake_copy'],
        allowed_processors=[],
        review_destination={'provider': 'test', 'id': 'review-1', 'owner_id': 'philip'},
        retry_allowance=2,
        staging=StagingPaths('/synthetic/work', '/synthetic/work/source.mp4',
                             '/synthetic/output', '/synthetic/output/candidate.mp4'),
        created_at=NOW)

def make_checkpoint(spec, *, event_id='evt-intake-1', state='INTAKE', revision=1, blocked_from_state=None):
    return JobCheckpoint('1', spec.job_id, spec.grant_id, state, revision,
                         event_id, MappingProxyType({}), NOW, blocked_from_state)

def apply(checkpoint, target, *, event_id, evidence=('ev-1',), actor='runner', decision='AUTHORIZED', now=NOW, reason=''):
    """Call reduce_transition and return (decision, new_checkpoint_or_same)."""
    result = reduce_transition(checkpoint, target, event_id=event_id,
                               evidence_refs=evidence, actor=actor,
                               decision=decision, now=now, reason=reason)
    if not result.accepted or result.event is None:
        return result, checkpoint
    if result.next_state == 'BLOCKED_NEEDS_REVIEW':
        blocked_from = result.event.prior_state
    elif checkpoint.current_state == 'BLOCKED_NEEDS_REVIEW':
        blocked_from = None
    else:
        blocked_from = checkpoint.blocked_from_state
    new_checkpoint = JobCheckpoint(
        '1', checkpoint.job_id, checkpoint.grant_id, result.next_state,
        result.new_revision, result.event.event_id,
        checkpoint.attempt_counts, now, blocked_from)
    return result, new_checkpoint


class StateTransitionTests(unittest.TestCase):
    def test_valid_path_reaches_ready_for_review(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        self.assertEqual(ckpt.current_state, 'INTAKE')
        self.assertEqual(ckpt.state_revision, 1)

        mainline = [
            'AUTHORIZED',
            'TRANSCRIBED',
            'SCRIPT_EVIDENCE_CHECKED',
            'ALIGNED',
            'RENDER_PERMITTED',
            'RENDERED',
            'QC_EVALUATED',
            'READY_FOR_REVIEW',
        ]

        for idx, target in enumerate(mainline, start=2):
            evidence = (f'ev-{target}',)
            event_id = f'evt-{target}'
            res, ckpt = apply(ckpt, target, event_id=event_id, evidence=evidence)
            self.assertTrue(res.accepted)
            self.assertEqual(res.reason, TRANSITION_ACCEPTED)
            self.assertEqual(res.next_state, target)
            self.assertEqual(res.new_revision, idx)
            self.assertIsNotNone(res.event)
            self.assertEqual(res.event.state_revision, idx)
            self.assertEqual(res.event.evidence_refs, evidence)
            self.assertEqual(ckpt.current_state, target)
            self.assertEqual(ckpt.state_revision, idx)

        self.assertEqual(ckpt.current_state, 'READY_FOR_REVIEW')
        self.assertEqual(ckpt.state_revision, 9)

    def test_forbidden_skips_rejected(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)

        # From INTAKE: target RENDERED -> rejected STATE_SKIP_FORBIDDEN
        res, _ = apply(ckpt, 'RENDERED', event_id='evt-f1')
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, STATE_SKIP_FORBIDDEN)
        self.assertEqual(res.next_state, 'INTAKE')
        self.assertEqual(res.new_revision, 1)
        self.assertIsNone(res.event)

        # Move to AUTHORIZED
        _, ckpt_auth = apply(ckpt, 'AUTHORIZED', event_id='evt-a', evidence=('ev-a',))
        self.assertEqual(ckpt_auth.current_state, 'AUTHORIZED')

        # From AUTHORIZED: target READY_FOR_REVIEW -> rejected STATE_SKIP_FORBIDDEN
        res, _ = apply(ckpt_auth, 'READY_FOR_REVIEW', event_id='evt-f2')
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, STATE_SKIP_FORBIDDEN)
        self.assertEqual(res.next_state, 'AUTHORIZED')
        self.assertEqual(res.new_revision, ckpt_auth.state_revision)
        self.assertIsNone(res.event)

        # Move to TRANSCRIBED
        _, ckpt_trans = apply(ckpt_auth, 'TRANSCRIBED', event_id='evt-t', evidence=('ev-t',))
        self.assertEqual(ckpt_trans.current_state, 'TRANSCRIBED')

        # From TRANSCRIBED: target ALIGNED -> rejected SCRIPT_EVIDENCE_UNCHECKED
        res, _ = apply(ckpt_trans, 'ALIGNED', event_id='evt-f3')
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, SCRIPT_EVIDENCE_UNCHECKED)
        self.assertEqual(res.next_state, 'TRANSCRIBED')
        self.assertEqual(res.new_revision, ckpt_trans.state_revision)
        self.assertIsNone(res.event)

        # Move to SCRIPT_EVIDENCE_CHECKED then ALIGNED
        _, ckpt_script = apply(ckpt_trans, 'SCRIPT_EVIDENCE_CHECKED', event_id='evt-s', evidence=('ev-s',))
        _, ckpt_aligned = apply(ckpt_script, 'ALIGNED', event_id='evt-al', evidence=('ev-al',))
        self.assertEqual(ckpt_aligned.current_state, 'ALIGNED')

        # From ALIGNED: target RENDERED -> rejected RENDER_NOT_PERMITTED
        res, _ = apply(ckpt_aligned, 'RENDERED', event_id='evt-f4')
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, RENDER_NOT_PERMITTED)
        self.assertEqual(res.next_state, 'ALIGNED')
        self.assertEqual(res.new_revision, ckpt_aligned.state_revision)
        self.assertIsNone(res.event)

    def test_duplicate_event_is_idempotent(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        res1, ckpt_auth = apply(ckpt, 'AUTHORIZED', event_id='evt-2', evidence=('ev-auth',))
        self.assertTrue(res1.accepted)
        self.assertEqual(ckpt_auth.current_state, 'AUTHORIZED')
        self.assertEqual(ckpt_auth.state_revision, 2)

        res2, _ = apply(ckpt_auth, 'AUTHORIZED', event_id='evt-2', evidence=('ev-auth',))
        self.assertTrue(res2.accepted)
        self.assertEqual(res2.reason, IDEMPOTENT_NOOP)
        self.assertEqual(res2.next_state, 'AUTHORIZED')
        self.assertEqual(res2.new_revision, 2)
        self.assertIsNone(res2.event)

        res3, _ = apply(ckpt_auth, 'AUTHORIZED', event_id='evt-2', evidence=('ev-different',))
        self.assertTrue(res3.accepted)
        self.assertEqual(res3.reason, IDEMPOTENT_NOOP)
        self.assertEqual(res3.next_state, 'AUTHORIZED')
        self.assertEqual(res3.new_revision, 2)
        self.assertIsNone(res3.event)

    def test_fresh_event_same_state_is_not_duplicate(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        res, _ = apply(ckpt, 'INTAKE', event_id='evt-fresh-same', evidence=('ev-new',))
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, STATE_SKIP_FORBIDDEN)
        self.assertEqual(res.next_state, 'INTAKE')
        self.assertEqual(res.new_revision, 1)
        self.assertIsNone(res.event)

    def test_terminal_states_locked(self):
        spec = make_spec()
        for state in ('FAILED_JOB', 'FAILED_SYSTEM', 'READY_FOR_REVIEW'):
            ckpt = make_checkpoint(spec, state=state, revision=5)
            res, _ = apply(ckpt, 'AUTHORIZED', event_id='evt-term', evidence=('ev-term',))
            self.assertFalse(res.accepted)
            self.assertEqual(res.reason, TERMINAL_STATE_LOCKED)
            self.assertEqual(res.next_state, state)
            self.assertEqual(res.new_revision, 5)
            self.assertIsNone(res.event)

        for state in ('CANCELLED', 'BLOCKED_AUTHORIZATION'):
            ckpt = make_checkpoint(spec, state=state, revision=5)
            res, _ = apply(ckpt, 'AUTHORIZED', event_id='evt-revoked', evidence=('ev-revoked',))
            self.assertFalse(res.accepted)
            self.assertEqual(res.reason, REVOKED_OR_CANCELLED_JOB)
            self.assertEqual(res.next_state, state)
            self.assertEqual(res.new_revision, 5)
            self.assertIsNone(res.event)

    def test_blocked_needs_review_resume(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        res_blocked, ckpt_blocked = apply(
            ckpt, 'BLOCKED_NEEDS_REVIEW',
            event_id='evt-block', evidence=('ev-ambiguous',),
            decision='BLOCKED', reason='ambiguous'
        )
        self.assertTrue(res_blocked.accepted)
        self.assertEqual(res_blocked.new_revision, 2)
        self.assertEqual(ckpt_blocked.current_state, 'BLOCKED_NEEDS_REVIEW')
        self.assertEqual(ckpt_blocked.blocked_from_state, 'INTAKE')

        # (a) resume to AUTHORIZED -> rejected STATE_SKIP_FORBIDDEN
        res_a, _ = apply(ckpt_blocked, 'AUTHORIZED', event_id='evt-resume-auth', evidence=('ev-auth',))
        self.assertFalse(res_a.accepted)
        self.assertEqual(res_a.reason, STATE_SKIP_FORBIDDEN)
        self.assertEqual(res_a.next_state, 'BLOCKED_NEEDS_REVIEW')
        self.assertEqual(res_a.new_revision, 2)
        self.assertIsNone(res_a.event)

        # (b) resume to INTAKE with EMPTY evidence () -> rejected CORRECTION_EVIDENCE_REQUIRED
        res_b, _ = apply(ckpt_blocked, 'INTAKE', event_id='evt-resume-empty', evidence=())
        self.assertFalse(res_b.accepted)
        self.assertEqual(res_b.reason, CORRECTION_EVIDENCE_REQUIRED)
        self.assertEqual(res_b.next_state, 'BLOCKED_NEEDS_REVIEW')
        self.assertEqual(res_b.new_revision, 2)
        self.assertIsNone(res_b.event)

        # (c) resume to INTAKE with evidence ('ev-corrected',) and decision 'CORRECTED'
        res_c, ckpt_resumed = apply(
            ckpt_blocked, 'INTAKE',
            event_id='evt-resume-corr', evidence=('ev-corrected',),
            decision='CORRECTED'
        )
        self.assertTrue(res_c.accepted)
        self.assertEqual(res_c.reason, TRANSITION_ACCEPTED)
        self.assertEqual(res_c.next_state, 'INTAKE')
        self.assertEqual(res_c.new_revision, 3)
        self.assertEqual(ckpt_resumed.current_state, 'INTAKE')
        self.assertIsNone(ckpt_resumed.blocked_from_state)

        # Also assert move from BLOCKED_NEEDS_REVIEW to CANCELLED with evidence is accepted
        res_can, ckpt_can = apply(
            ckpt_blocked, 'CANCELLED',
            event_id='evt-cancel-block', evidence=('ev-cancel',)
        )
        self.assertTrue(res_can.accepted)
        self.assertEqual(res_can.reason, TRANSITION_ACCEPTED)
        self.assertEqual(res_can.next_state, 'CANCELLED')
        self.assertEqual(res_can.new_revision, 3)
        self.assertEqual(ckpt_can.current_state, 'CANCELLED')

    def test_empty_evidence_rejected(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        res, _ = apply(ckpt, 'AUTHORIZED', event_id='evt-empty', evidence=())
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason, CONTENT_EVIDENCE_AMBIGUOUS)
        self.assertEqual(res.next_state, 'INTAKE')
        self.assertEqual(res.new_revision, 1)
        self.assertIsNone(res.event)

    def test_checkpoint_round_trip_and_tamper(self):
        spec = make_spec()
        base = make_checkpoint(spec)
        ckpt = increment_attempt(base, 'render_candidate')
        raw = serialize_checkpoint(ckpt)
        restored = deserialize_checkpoint(raw)
        self.assertEqual(restored, ckpt)

        # Tamper cases:
        # payload with schema_version '2'
        bad_schema = raw.replace(b'"schema_version":"1"', b'"schema_version":"2"')
        with self.assertRaises(CheckpointError):
            deserialize_checkpoint(bad_schema)

        # payload with state_revision 0
        bad_rev = raw.replace(b'"state_revision":1', b'"state_revision":0')
        with self.assertRaises(CheckpointError):
            deserialize_checkpoint(bad_rev)

        # payload with an extra unknown key
        bad_extra = raw.replace(b'"job_id":"job-1"', b'"job_id":"job-1","unknown_key":"val"')
        with self.assertRaises(CheckpointError):
            deserialize_checkpoint(bad_extra)

        # payload with state_revision True (bool)
        bad_bool = raw.replace(b'"state_revision":1', b'"state_revision":true')
        with self.assertRaises(CheckpointError):
            deserialize_checkpoint(bad_bool)

        # deserializing with expected_job_id='job-other'
        with self.assertRaises(CheckpointError):
            deserialize_checkpoint(raw, expected_job_id='job-other')

        # data longer than 65536 bytes
        with self.assertRaises(CheckpointError):
            deserialize_checkpoint(b' ' * 65537)

        # duplicate JSON keys
        bad_dup = raw.replace(b'"job_id":"job-1"', b'"job_id":"job-1","job_id":"job-1"')
        with self.assertRaises(CheckpointError):
            deserialize_checkpoint(bad_dup)

        # Serializing a non-checkpoint (e.g. a plain string) must raise ValueError
        with self.assertRaises(ValueError):
            serialize_checkpoint('plain-string')

    def test_event_round_trip_and_tamper(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        res, _ = apply(ckpt, 'AUTHORIZED', event_id='evt-rt', evidence=('ev-1', 'ev-2'))
        self.assertTrue(res.accepted)
        event = res.event
        raw = serialize_event(event)
        restored = deserialize_event(raw)
        self.assertEqual(restored, event)

        # Tamper:
        # evidence_refs with duplicate entry
        bad_dup = raw.replace(b'["ev-1","ev-2"]', b'["ev-1","ev-1"]')
        with self.assertRaises(CheckpointError):
            deserialize_event(bad_dup)

        # evidence_refs empty list
        bad_empty = raw.replace(b'["ev-1","ev-2"]', b'[]')
        with self.assertRaises(CheckpointError):
            deserialize_event(bad_empty)

        # payload with unknown key
        bad_extra = raw.replace(b'"job_id":"job-1"', b'"job_id":"job-1","unknown_key":"val"')
        with self.assertRaises(CheckpointError):
            deserialize_event(bad_extra)

        # deserializing with expected_job_id='job-other'
        with self.assertRaises(CheckpointError):
            deserialize_event(raw, expected_job_id='job-other')

    def test_increment_attempt_counter(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        c1 = increment_attempt(ckpt, 'render_candidate')
        c2 = increment_attempt(c1, 'render_candidate')
        c3 = increment_attempt(c2, 'qc')

        self.assertEqual(c3.attempt_counts, {'render_candidate': 2, 'qc': 1})
        self.assertEqual(ckpt.attempt_counts, {})
        self.assertEqual(c3.current_state, ckpt.current_state)
        self.assertEqual(c3.state_revision, ckpt.state_revision)

    def test_validate_transition_matrix(self):
        true_pairs = [
            ('INTAKE', 'AUTHORIZED'),
            ('AUTHORIZED', 'TRANSCRIBED'),
            ('QC_EVALUATED', 'READY_FOR_REVIEW'),
            ('INTAKE', 'CANCELLED'),
            ('ALIGNED', 'BLOCKED_NEEDS_REVIEW'),
            ('BLOCKED_NEEDS_REVIEW', 'CANCELLED'),
        ]
        for cur, tgt in true_pairs:
            self.assertTrue(validate_transition(cur, tgt), f"Expected True for {cur} -> {tgt}")

        false_pairs = [
            ('INTAKE', 'RENDERED'),
            ('TRANSCRIBED', 'ALIGNED'),
            ('RENDERED', 'ALIGNED'),
            ('READY_FOR_REVIEW', 'AUTHORIZED'),
            ('CANCELLED', 'AUTHORIZED'),
            ('INTAKE', 'NOPE'),
        ]
        for cur, tgt in false_pairs:
            self.assertFalse(validate_transition(cur, tgt), f"Expected False for {cur} -> {tgt}")

    def test_initial_checkpoint_validation(self):
        spec = make_spec()
        with self.assertRaises(ValueError):
            initial_checkpoint('plain-string', event_id='evt-1', actor='runner', now=NOW, evidence_refs=('ev-1',))

        with self.assertRaises(ValueError):
            initial_checkpoint(spec, event_id='evt-1', actor='runner', now='2026-02-30T12:00:00Z', evidence_refs=('ev-1',))

        with self.assertRaises(ValueError):
            initial_checkpoint(spec, event_id='evt-1', actor='runner', now=NOW, evidence_refs=())

    def test_is_terminal_state_scope(self):
        for state in ('CANCELLED', 'FAILED_JOB', 'FAILED_SYSTEM', 'READY_FOR_REVIEW'):
            self.assertTrue(is_terminal_state(state), f"Expected True for {state}")

        for state in ('BLOCKED_AUTHORIZATION', 'BLOCKED_NEEDS_REVIEW', 'INTAKE', 'AUTHORIZED'):
            self.assertFalse(is_terminal_state(state), f"Expected False for {state}")


class AuditPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_restart_resume_from_checkpoint(self):
        from subtitle.core.audit import CHECKPOINT_FILENAME

        spec = make_spec()
        checkpoint = initial_checkpoint(
            spec, event_id='evt-intake-1', actor='runner', now=NOW, evidence_refs=('ev_grant_verified',)
        )
        job_dir = self.root / 'jobs' / 'job-1'
        job_dir.mkdir(parents=True, exist_ok=True)
        save_job_spec(job_dir / JOB_SPEC_FILENAME, spec)

        intake_event = JobEvent(
            event_id='evt-intake-1',
            job_id=spec.job_id,
            prior_state='INTAKE',
            next_state='INTAKE',
            state_revision=1,
            evidence_refs=('ev_grant_verified',),
            actor='runner',
            decision='AUTHORIZED',
            timestamp=NOW,
            reason='intake accepted',
        )
        events_path = job_dir / EVENT_LOG_FILENAME
        append_event(events_path, intake_event)
        save_checkpoint(job_dir / CHECKPOINT_FILENAME, checkpoint)

        steps = [
            ('AUTHORIZED', 'evt-auth', ('ev-auth',)),
            ('TRANSCRIBED', 'evt-trans', ('ev-trans',)),
            ('SCRIPT_EVIDENCE_CHECKED', 'evt-script', ('ev-script',)),
            ('ALIGNED', 'evt-aligned', ('ev-aligned',)),
        ]
        for target, evt_id, ev_refs in steps:
            res, checkpoint = apply(checkpoint, target, event_id=evt_id, evidence=ev_refs)
            self.assertTrue(res.accepted)
            append_event(events_path, res.event)
            save_checkpoint(job_dir / CHECKPOINT_FILENAME, checkpoint)

        # Simulate restart by loading fresh objects from disk:
        record = load_job_record(job_dir, job_id='job-1')
        self.assertIsNotNone(record)
        rec_spec, rec_ckpt, rec_events = record
        self.assertEqual(rec_spec, spec)
        self.assertEqual(rec_ckpt.current_state, 'ALIGNED')
        self.assertEqual(rec_ckpt.state_revision, 5)
        self.assertEqual(len(rec_events), 5)
        self.assertEqual(rec_events[-1].event_id, rec_ckpt.last_event_id)

        # Continue from loaded checkpoint to RENDER_PERMITTED
        res_next, next_ckpt = apply(rec_ckpt, 'RENDER_PERMITTED', event_id='evt-render-perm', evidence=('ev-perm',))
        self.assertTrue(res_next.accepted)
        self.assertEqual(res_next.new_revision, 6)
        self.assertEqual(next_ckpt.current_state, 'RENDER_PERMITTED')
        self.assertEqual(next_ckpt.state_revision, 6)

    def test_atomic_write_failure_leaves_target_byte_identical(self):
        target = self.root / 'atomic_test.json'
        atomic_write_json(target, {'k': 'v', 'num': 1})
        recorded_bytes = target.read_bytes()

        with self.assertRaises(ValueError):
            atomic_write_file(target, 'not-bytes')
        self.assertEqual(target.read_bytes(), recorded_bytes)

        with self.assertRaises(ValueError):
            atomic_write_json(target, ['not', 'a', 'dict'])
        self.assertEqual(target.read_bytes(), recorded_bytes)

    def test_corrupt_event_log_middle_line_rejected(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        events_path = self.root / EVENT_LOG_FILENAME

        e1 = JobEvent('evt-1', spec.job_id, 'INTAKE', 'INTAKE', 1, ('ev-1',), 'runner', 'AUTHORIZED', NOW, 'intake')
        append_event(events_path, e1)

        res2, ckpt2 = apply(ckpt, 'AUTHORIZED', event_id='evt-2', evidence=('ev-2',))
        append_event(events_path, res2.event)

        res3, _ = apply(ckpt2, 'TRANSCRIBED', event_id='evt-3', evidence=('ev-3',))
        append_event(events_path, res3.event)

        valid_bytes = events_path.read_bytes()
        lines = valid_bytes.splitlines()
        self.assertEqual(len(lines), 3)

        # Replace middle line with corrupt json
        corrupt_middle = lines[0] + b'\n' + b'{corrupt json' + b'\n' + lines[2] + b'\n'
        events_path.write_bytes(corrupt_middle)
        with self.assertRaises(AuditError):
            load_event_log(events_path)

        # Insert empty line in middle
        blank_middle = lines[0] + b'\n\n' + lines[1] + b'\n' + lines[2] + b'\n'
        events_path.write_bytes(blank_middle)
        with self.assertRaises(AuditError):
            load_event_log(events_path)

    def test_event_log_revision_gap_rejected(self):
        spec = make_spec()
        ckpt = make_checkpoint(spec)
        events_path = self.root / EVENT_LOG_FILENAME

        e1 = JobEvent('evt-1', spec.job_id, 'INTAKE', 'INTAKE', 1, ('ev-1',), 'runner', 'AUTHORIZED', NOW, 'intake')
        append_event(events_path, e1)

        res2, _ = apply(ckpt, 'AUTHORIZED', event_id='evt-2', evidence=('ev-2',))
        append_event(events_path, res2.event)

        # Event with revision 4 (skipping 3) but matching prior_state
        e_gap = JobEvent('evt-3', spec.job_id, 'AUTHORIZED', 'TRANSCRIBED', 4, ('ev-3',), 'runner', 'AUTHORIZED', NOW, 'gap')
        append_event(events_path, e_gap)

        with self.assertRaises(AuditError):
            load_event_log(events_path)

    def test_checkpoint_wrong_schema_or_job_rejected(self):
        from subtitle.core.audit import CHECKPOINT_FILENAME

        spec = make_spec()
        ckpt = make_checkpoint(spec)
        ckpt_path = self.root / CHECKPOINT_FILENAME
        save_checkpoint(ckpt_path, ckpt)

        with self.assertRaises(CheckpointError):
            load_checkpoint(ckpt_path, expected_job_id='job-other')

        raw = ckpt_path.read_bytes()
        ckpt_path.write_bytes(raw.replace(b'"schema_version":"1"', b'"schema_version":"2"'))
        with self.assertRaises(CheckpointError):
            load_checkpoint(ckpt_path)

    def test_replay_ledger_bind_resume_replay(self):
        ledger = ReplayLedger(self.root / LEDGER_FILENAME)
        self.assertIsNone(ledger.get_binding('grant-1'))
        self.assertTrue(ledger.bind_grant('grant-1', 'job-1', NOW))
        self.assertTrue(ledger.bind_grant('grant-1', 'job-1', NOW))
        self.assertFalse(ledger.bind_grant('grant-1', 'job-2', NOW))
        self.assertEqual(ledger.get_binding('grant-1'), 'job-1')

        ledger2 = ReplayLedger(self.root / LEDGER_FILENAME)
        self.assertEqual(ledger2.get_binding('grant-1'), 'job-1')

        (self.root / LEDGER_FILENAME).write_bytes(b'{corrupt')
        with self.assertRaises(AuditError):
            ledger2.get_binding('grant-1')

    def test_load_job_record_missing_checkpoint(self):
        job_dir = self.root / 'jobs' / 'job-1'
        job_dir.mkdir(parents=True, exist_ok=True)
        spec = make_spec()
        save_job_spec(job_dir / JOB_SPEC_FILENAME, spec)

        with self.assertRaises(AuditError):
            load_job_record(job_dir, job_id='job-1')

        self.assertIsNone(load_job_record(self.root / 'jobs' / 'missing', job_id='missing'))
        self.assertIsNone(load_checkpoint(self.root / 'missing_checkpoint.json'))
        self.assertEqual(load_event_log(self.root / 'missing_events.jsonl'), [])

    def test_atomic_ledger_file_location(self):
        from subtitle.core.audit import CHECKPOINT_FILENAME

        self.assertEqual(LEDGER_FILENAME, 'replay_ledger.json')
        self.assertEqual(JOB_SPEC_FILENAME, 'job_spec.json')
        self.assertEqual(CHECKPOINT_FILENAME, 'checkpoint.json')
        self.assertEqual(EVENT_LOG_FILENAME, 'events.jsonl')

        ledger_path = self.root / LEDGER_FILENAME
        ledger = ReplayLedger(ledger_path)
        ledger.bind_grant('grant-loc-1', 'job-loc-1', NOW)
        self.assertTrue(ledger_path.exists())
        self.assertTrue(ledger_path.is_file())


NOW_DT = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
GRANT_DOMAIN = b'ThaiSubburn JobGrant v1\x00'
STATUS_DOMAIN = b'ThaiSubburn Revocation v1\x00'


def ts(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def sign(obj, key, domain):
    payload = dict(obj)
    payload.pop('signature', None)
    payload['signature'] = base64.b64encode(
        key.sign(domain + canonical(payload))).decode('ascii')
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


class IntakeResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_root = self.root / 'trusted-source'
        self.base_staging_dir = self.root / 'staging'
        self.source_root.mkdir()
        self.source_path = self.source_root / 'synthetic.mp4'
        self.source_bytes = b'fake-video-bytes-p2-004\x00' * 4096
        self.source_path.write_bytes(self.source_bytes)
        self.expected_hash = hashlib.sha256(self.source_bytes).hexdigest()
        self.source_ref = dict(provider='test', id='source-1', revision='r1',
                               sha256=self.expected_hash)
        self.review_destination = dict(provider='test', id='review-1', owner_id='philip')
        self.grant_key = Ed25519PrivateKey.generate()
        self.status_key = Ed25519PrivateKey.generate()
        # The harness is the trusted host. Expectations are never read from a grant.
        self.host = dict(
            job_id='job-1', grant_id='grant-1', source_ref=copy.deepcopy(self.source_ref),
            style_id='viralpop', style_overrides={},
            review_destination=dict(self.review_destination),
            allowed_actions=['intake_copy'], allowed_processors=[], retry_allowance=2,
            source_path=self.source_path, trusted_source_root=self.source_root,
            base_staging_dir=self.base_staging_dir,
            trusted_grant_keys={('owner', 'g1'): self.grant_key.public_key()},
            trusted_status_keys={('status', 's1'): self.status_key.public_key()}, now=NOW_DT,
        )

    def make_grant(self, **overrides):
        grant = dict(
            schema_version='1', grant_id='grant-1', job_id='job-1',
            issuer_id='owner', key_id='g1', issued_at=ts(NOW_DT - timedelta(hours=1)),
            expires_at=ts(NOW_DT + timedelta(hours=1)),
            source_ref=copy.deepcopy(self.source_ref), style_id='viralpop',
            style_overrides={}, allowed_actions=['intake_copy'], allowed_processors=[],
            review_destination=dict(self.review_destination), retry_allowance=2,
        )
        grant.update(overrides)
        return grant

    def make_status(self, **overrides):
        status = dict(schema_version='1', grant_id='grant-1', job_id='job-1',
                      issuer_id='status', key_id='s1', checked_at=ts(NOW_DT), status='active')
        status.update(overrides)
        return status

    def run_intake(self, *, grant=None, status=None, grant_bytes=None, **overrides):
        args = dict(self.host)
        args.update(overrides)
        raw = (grant_bytes if grant_bytes is not None else
               sign(self.make_grant() if grant is None else grant,
                    self.grant_key, GRANT_DOMAIN))
        status_raw = sign(self.make_status() if status is None else status,
                          self.status_key, STATUS_DOMAIN)
        return process_job_intake(raw, status_raw, **args)

    def job_dir(self):
        return self.base_staging_dir / 'jobs' / 'job-1'

    def test_intake_persists_spec_checkpoint_event_log(self):
        result = self.run_intake()
        self.assertTrue(result.success)
        job_dir = self.job_dir()
        self.assertTrue((job_dir / JOB_SPEC_FILENAME).is_file())
        self.assertTrue((job_dir / CHECKPOINT_FILENAME).is_file())
        self.assertTrue((job_dir / EVENT_LOG_FILENAME).is_file())
        spec, checkpoint, events = load_job_record(job_dir, job_id='job-1')
        self.assertEqual(spec, result.spec)
        self.assertEqual(checkpoint.current_state, 'INTAKE')
        self.assertEqual(checkpoint.state_revision, 1)
        self.assertEqual(checkpoint.last_event_id, 'evt-intake-1')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, 'evt-intake-1')
        self.assertEqual(events[0].state_revision, 1)
        # Intake never touches the replay ledger; the host owns it.
        self.assertFalse((self.base_staging_dir / LEDGER_FILENAME).exists())

    def test_intake_reentry_returns_frozen_spec(self):
        first = self.run_intake()
        self.assertTrue(first.success)
        staged = Path(first.spec.staging.staged_input_path)
        staged_bytes_before = staged.read_bytes()
        second = self.run_intake()
        self.assertTrue(second.success)
        self.assertIsNone(second.deny_reason)
        self.assertEqual(second.spec, first.spec)
        self.assertEqual(staged.read_bytes(), staged_bytes_before)
        self.assertEqual(compute_file_sha256(staged), self.expected_hash)
        _, checkpoint, events = load_job_record(self.job_dir(), job_id='job-1')
        self.assertEqual(checkpoint.current_state, 'INTAKE')
        self.assertEqual(checkpoint.state_revision, 1)
        self.assertEqual(len(events), 1)

    def test_intake_reentry_different_grant_denied_conflict(self):
        first = self.run_intake()
        self.assertTrue(first.success)
        spec_bytes_before = (self.job_dir() / JOB_SPEC_FILENAME).read_bytes()
        staged_digest_before = compute_file_sha256(Path(first.spec.staging.staged_input_path))
        second = self.run_intake(
            grant=self.make_grant(grant_id='grant-2'),
            status=self.make_status(grant_id='grant-2'),
            grant_id='grant-2')
        self.assertFalse(second.success)
        self.assertIsNone(second.spec)
        self.assertEqual(second.deny_reason, JOB_STATE_CONFLICT)
        # The existing job folder is never deleted by a polite deny.
        self.assertTrue(self.job_dir().exists())
        self.assertEqual((self.job_dir() / JOB_SPEC_FILENAME).read_bytes(), spec_bytes_before)
        self.assertEqual(compute_file_sha256(Path(first.spec.staging.staged_input_path)),
                         staged_digest_before)

    def test_intake_reentry_without_record_denied_conflict(self):
        job_dir = self.job_dir()
        job_dir.mkdir(parents=True)
        result = self.run_intake()
        self.assertFalse(result.success)
        self.assertIsNone(result.spec)
        self.assertEqual(result.deny_reason, JOB_STATE_CONFLICT)
        # The pre-existing folder is preserved exactly as it was: still there, still empty.
        self.assertTrue(job_dir.exists())
        self.assertEqual(list(job_dir.iterdir()), [])

    def test_intake_reentry_tampered_staged_file(self):
        first = self.run_intake()
        self.assertTrue(first.success)
        staged = Path(first.spec.staging.staged_input_path)
        staged.chmod(0o600)
        staged.write_bytes(b'tampered-bytes')
        second = self.run_intake()
        self.assertFalse(second.success)
        self.assertIsNone(second.spec)
        self.assertEqual(second.deny_reason, 'SOURCE_MUTATED_ERROR')
        self.assertTrue((self.job_dir() / JOB_SPEC_FILENAME).exists())

    def test_allocate_existing_dir_denied_politely(self):
        allocate_staging_paths(self.base_staging_dir, 'job-9', 'video.mp4')
        with self.assertRaises(intake._Deny) as ctx:
            allocate_staging_paths(self.base_staging_dir, 'job-9', 'video.mp4')
        self.assertEqual(ctx.exception.args[0], JOB_STATE_CONFLICT)

    def test_copy_existing_dest_denied_politely(self):
        dest_dir = self.root / 'dest'
        dest_dir.mkdir()
        dest = dest_dir / 'source.mp4'
        digest, count = copy_source_to_staging(self.source_path, dest, self.expected_hash)
        self.assertEqual(digest, self.expected_hash)
        self.assertEqual(count, len(self.source_bytes))
        with self.assertRaises(intake._Deny) as ctx:
            copy_source_to_staging(self.source_path, dest, self.expected_hash)
        self.assertEqual(ctx.exception.args[0], JOB_STATE_CONFLICT)


if __name__ == '__main__':
    unittest.main()
