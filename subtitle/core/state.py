"""P2-004 single-job state machine (pure reducer core); no Production Auto Mode.

State and revision live in a frozen JobCheckpoint; every accepted transition is
recorded as a frozen JobEvent. The reducer is pure: no filesystem I/O, no clock
reads, no subprocess calls. Spec section 6 names the canonical form
transition(current, event, evidence, policy); this module's concrete form is
reduce_transition(checkpoint, target_state, ...), which keeps the same purity.
Locked decisions: state_revision starts at 1 at INTAKE; BLOCKED_NEEDS_REVIEW is
a temporary pause resumable only to the state it was blocked from, with a
correction event and new evidence; CANCELLED and BLOCKED_AUTHORIZATION never
resume; a duplicate event is defined by event_id only; READY_FOR_REVIEW counts
as terminal only within the Phase 2A one-clip scope; attempt_counts is a counter
mechanism only and retry policy belongs to P2-008; single writer per job is
assumed and there is no fcntl.flock in this ticket. JobEvent fields job_id,
timestamp and reason are P2-004 extensions, not from Spec section 7. The audit
log is verifiable, never tamper-proof.
"""

import json
import re
from dataclasses import dataclass, fields
from datetime import datetime
from types import MappingProxyType

from .authorization import canonical
from .job import JobSpec

MAINLINE_STATES = (
    'INTAKE',
    'AUTHORIZED',
    'TRANSCRIBED',
    'SCRIPT_EVIDENCE_CHECKED',
    'ALIGNED',
    'RENDER_PERMITTED',
    'RENDERED',
    'QC_EVALUATED',
    'READY_FOR_REVIEW',
)
BRANCH_STATES = (
    'BLOCKED_NEEDS_REVIEW',
    'BLOCKED_AUTHORIZATION',
    'CANCELLED',
    'FAILED_JOB',
    'FAILED_SYSTEM',
)
KNOWN_STATES = MAINLINE_STATES + BRANCH_STATES
TERMINAL_STATES = (
    'CANCELLED',
    'FAILED_JOB',
    'FAILED_SYSTEM',
    'READY_FOR_REVIEW',
)

TRANSITION_ACCEPTED = 'TRANSITION_ACCEPTED'
STATE_SKIP_FORBIDDEN = 'STATE_SKIP_FORBIDDEN'
SCRIPT_EVIDENCE_UNCHECKED = 'SCRIPT_EVIDENCE_UNCHECKED'
RENDER_NOT_PERMITTED = 'RENDER_NOT_PERMITTED'
TERMINAL_STATE_LOCKED = 'TERMINAL_STATE_LOCKED'
REVOKED_OR_CANCELLED_JOB = 'REVOKED_OR_CANCELLED_JOB'
IDEMPOTENT_NOOP = 'IDEMPOTENT_NOOP'
INVALID_STATE_REVISION = 'INVALID_STATE_REVISION'
CONTENT_EVIDENCE_AMBIGUOUS = 'CONTENT_EVIDENCE_AMBIGUOUS'
CORRECTION_EVIDENCE_REQUIRED = 'CORRECTION_EVIDENCE_REQUIRED'

_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


@dataclass(frozen=True)
class JobEvent:
    event_id: str
    job_id: str
    prior_state: str
    next_state: str
    state_revision: int
    evidence_refs: tuple[str, ...]
    actor: str
    decision: str
    timestamp: str
    reason: str


@dataclass(frozen=True)
class JobCheckpoint:
    schema_version: str
    job_id: str
    grant_id: str
    current_state: str
    state_revision: int
    last_event_id: str
    attempt_counts: MappingProxyType
    updated_at: str
    blocked_from_state: str | None = None


@dataclass(frozen=True)
class TransitionDecision:
    accepted: bool
    next_state: str
    new_revision: int
    reason: str
    event: JobEvent | None


class CheckpointError(ValueError):
    """Serialized checkpoint or event violates the frozen version 1 contract."""


def _validate_timestamp(value) -> bool:
    if type(value) is not str or not _TIMESTAMP.match(value):
        return False
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return True
    except ValueError:
        return False


def _validate_evidence_refs(value) -> bool:
    if type(value) not in (tuple, list) or len(value) == 0:
        return False
    if not all(type(item) is str and bool(item) for item in value):
        return False
    if len(set(value)) != len(value):
        return False
    return True


def is_terminal_state(state: str) -> bool:
    return state in TERMINAL_STATES


def validate_transition(current_state: str, target_state: str) -> bool:
    """Validate whether current_state can transition to target_state.

    A BLOCKED_NEEDS_REVIEW resume to its blocked_from_state cannot be checked
    here because it needs the checkpoint; that check lives in reduce_transition.
    """
    if current_state not in KNOWN_STATES or target_state not in KNOWN_STATES:
        return False
    if current_state in MAINLINE_STATES:
        idx = MAINLINE_STATES.index(current_state)
        if idx + 1 < len(MAINLINE_STATES) and target_state == MAINLINE_STATES[idx + 1]:
            return True
    if (target_state in BRANCH_STATES
            and current_state not in TERMINAL_STATES
            and current_state not in ('BLOCKED_AUTHORIZATION', 'CANCELLED')):
        return True
    if current_state == 'BLOCKED_NEEDS_REVIEW' and target_state in BRANCH_STATES:
        return True
    return False


def initial_checkpoint(spec, *, event_id: str, actor: str, now: str, evidence_refs) -> JobCheckpoint:
    if not isinstance(spec, JobSpec):
        raise ValueError('INVALID_SPEC')
    if type(event_id) is not str or not event_id:
        raise ValueError('INVALID_EVENT_ID')
    if type(actor) is not str or not actor:
        raise ValueError('INVALID_ACTOR')
    if not _validate_timestamp(now):
        raise ValueError('INVALID_TIMESTAMP')
    if not _validate_evidence_refs(evidence_refs):
        raise ValueError('INVALID_EVIDENCE_REFS')
    return JobCheckpoint(
        schema_version='1',
        job_id=spec.job_id,
        grant_id=spec.grant_id,
        current_state='INTAKE',
        state_revision=1,
        last_event_id=event_id,
        attempt_counts=MappingProxyType({}),
        updated_at=now,
        blocked_from_state=None,
    )


def increment_attempt(checkpoint, key: str) -> JobCheckpoint:
    """Increment the attempt counter for key.

    attempt_counts is a counter mechanism only and retry policy belongs to P2-008.
    """
    if not isinstance(checkpoint, JobCheckpoint):
        raise ValueError('INVALID_CHECKPOINT')
    if type(key) is not str or not key:
        raise ValueError('INVALID_KEY')
    counts = dict(checkpoint.attempt_counts)
    counts[key] = counts.get(key, 0) + 1
    return JobCheckpoint(
        schema_version=checkpoint.schema_version,
        job_id=checkpoint.job_id,
        grant_id=checkpoint.grant_id,
        current_state=checkpoint.current_state,
        state_revision=checkpoint.state_revision,
        last_event_id=checkpoint.last_event_id,
        attempt_counts=MappingProxyType(counts),
        updated_at=checkpoint.updated_at,
        blocked_from_state=checkpoint.blocked_from_state,
    )


def reduce_transition(
    checkpoint,
    target_state,
    *,
    event_id: str,
    evidence_refs,
    actor: str,
    decision: str,
    now: str,
    reason: str = '',
) -> TransitionDecision:
    """Pure transition reducer. Rules, in order: (1) validate inputs, (2) duplicate event_id is an idempotent no-op, (3) CANCELLED/BLOCKED_AUTHORIZATION reject with REVOKED_OR_CANCELLED_JOB, FAILED_JOB/FAILED_SYSTEM/READY_FOR_REVIEW reject with TERMINAL_STATE_LOCKED, (4) BLOCKED_NEEDS_REVIEW resumes only to checkpoint.blocked_from_state with new evidence, (5) mainline must step to the immediate successor, TRANSCRIBED->ALIGNED is SCRIPT_EVIDENCE_UNCHECKED, ALIGNED->RENDERED is RENDER_NOT_PERMITTED, anything else disallowed is STATE_SKIP_FORBIDDEN, (6) every accepted transition requires non-empty evidence, else CONTENT_EVIDENCE_AMBIGUOUS, (7) accept with revision + 1 and a new JobEvent. The reducer does not mutate or return a checkpoint. The caller derives blocked_from_state from the accepted event: entering BLOCKED_NEEDS_REVIEW sets it to prior_state; leaving BLOCKED_NEEDS_REVIEW clears it to None."""
    if not isinstance(checkpoint, JobCheckpoint):
        raise ValueError('INVALID_CHECKPOINT')
    if target_state not in KNOWN_STATES:
        raise ValueError('INVALID_TARGET_STATE')
    if checkpoint.current_state not in KNOWN_STATES:
        raise ValueError('INVALID_CURRENT_STATE')
    if type(event_id) is not str or not event_id:
        raise ValueError('INVALID_EVENT_ID')
    if type(actor) is not str or not actor:
        raise ValueError('INVALID_ACTOR')
    if type(decision) is not str or not decision:
        raise ValueError('INVALID_DECISION')
    if not _validate_timestamp(now):
        raise ValueError('INVALID_TIMESTAMP')
    if type(evidence_refs) not in (tuple, list) or not all(type(item) is str for item in evidence_refs):
        raise ValueError('INVALID_EVIDENCE_REFS')
    if type(reason) is not str:
        raise ValueError('INVALID_REASON')

    if event_id == checkpoint.last_event_id:
        return TransitionDecision(
            True,
            checkpoint.current_state,
            checkpoint.state_revision,
            IDEMPOTENT_NOOP,
            None,
        )

    current = checkpoint.current_state
    current_revision = checkpoint.state_revision

    if current in ('CANCELLED', 'BLOCKED_AUTHORIZATION'):
        return TransitionDecision(False, current, current_revision, REVOKED_OR_CANCELLED_JOB, None)
    if current in ('FAILED_JOB', 'FAILED_SYSTEM', 'READY_FOR_REVIEW'):
        return TransitionDecision(False, current, current_revision, TERMINAL_STATE_LOCKED, None)

    if current == 'BLOCKED_NEEDS_REVIEW':
        if target_state == checkpoint.blocked_from_state:
            if not _validate_evidence_refs(evidence_refs):
                return TransitionDecision(False, current, current_revision, CORRECTION_EVIDENCE_REQUIRED, None)
            new_revision = current_revision + 1
            event = JobEvent(
                event_id=event_id,
                job_id=checkpoint.job_id,
                prior_state=current,
                next_state=target_state,
                state_revision=new_revision,
                evidence_refs=tuple(evidence_refs),
                actor=actor,
                decision=decision,
                timestamp=now,
                reason=reason,
            )
            return TransitionDecision(True, target_state, new_revision, TRANSITION_ACCEPTED, event)
        if target_state not in BRANCH_STATES:
            return TransitionDecision(False, current, current_revision, STATE_SKIP_FORBIDDEN, None)

    if target_state == current:
        return TransitionDecision(False, current, current_revision, STATE_SKIP_FORBIDDEN, None)

    if target_state not in BRANCH_STATES:
        if current == 'TRANSCRIBED' and target_state == 'ALIGNED':
            return TransitionDecision(False, current, current_revision, SCRIPT_EVIDENCE_UNCHECKED, None)
        if current == 'ALIGNED' and target_state == 'RENDERED':
            return TransitionDecision(False, current, current_revision, RENDER_NOT_PERMITTED, None)
        if current in MAINLINE_STATES:
            idx = MAINLINE_STATES.index(current)
            if idx + 1 >= len(MAINLINE_STATES) or target_state != MAINLINE_STATES[idx + 1]:
                return TransitionDecision(False, current, current_revision, STATE_SKIP_FORBIDDEN, None)
        else:
            return TransitionDecision(False, current, current_revision, STATE_SKIP_FORBIDDEN, None)

    if not _validate_evidence_refs(evidence_refs):
        return TransitionDecision(False, current, current_revision, CONTENT_EVIDENCE_AMBIGUOUS, None)

    new_revision = current_revision + 1
    event = JobEvent(
        event_id=event_id,
        job_id=checkpoint.job_id,
        prior_state=current,
        next_state=target_state,
        state_revision=new_revision,
        evidence_refs=tuple(evidence_refs),
        actor=actor,
        decision=decision,
        timestamp=now,
        reason=reason,
    )
    return TransitionDecision(True, target_state, new_revision, TRANSITION_ACCEPTED, event)


def serialize_checkpoint(checkpoint) -> bytes:
    if not isinstance(checkpoint, JobCheckpoint):
        raise ValueError('INVALID_CHECKPOINT')
    payload = {
        'schema_version': checkpoint.schema_version,
        'job_id': checkpoint.job_id,
        'grant_id': checkpoint.grant_id,
        'current_state': checkpoint.current_state,
        'state_revision': checkpoint.state_revision,
        'last_event_id': checkpoint.last_event_id,
        'attempt_counts': dict(checkpoint.attempt_counts),
        'updated_at': checkpoint.updated_at,
        'blocked_from_state': checkpoint.blocked_from_state,
    }
    return canonical(payload)


def deserialize_checkpoint(data, *, expected_job_id=None) -> JobCheckpoint:
    if not isinstance(data, bytes) or len(data) > 65536:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    def _pairs_hook(pairs):
        res = {}
        for k, v in pairs:
            if k in res:
                raise CheckpointError('AUTH_SCHEMA_INVALID')
            res[k] = v
        return res

    def _reject(_):
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    try:
        payload = json.loads(
            data.decode('utf-8'),
            object_pairs_hook=_pairs_hook,
            parse_float=_reject,
            parse_constant=_reject,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CheckpointError('AUTH_SCHEMA_INVALID') from exc

    if not isinstance(payload, dict):
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if set(payload.keys()) != {field.name for field in fields(JobCheckpoint)}:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if payload['schema_version'] != '1':
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if type(payload['job_id']) is not str or not payload['job_id']:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if type(payload['grant_id']) is not str or not payload['grant_id']:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if payload['current_state'] not in KNOWN_STATES:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if type(payload['state_revision']) is not int or payload['state_revision'] < 1:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if type(payload['last_event_id']) is not str or not payload['last_event_id']:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    counts = payload['attempt_counts']
    if not isinstance(counts, dict):
        raise CheckpointError('AUTH_SCHEMA_INVALID')
    for k, v in counts.items():
        if type(k) is not str or not k or type(v) is not int or v < 0:
            raise CheckpointError('AUTH_SCHEMA_INVALID')

    if not _validate_timestamp(payload['updated_at']):
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if payload['blocked_from_state'] is not None and payload['blocked_from_state'] not in KNOWN_STATES:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if expected_job_id is not None and payload['job_id'] != expected_job_id:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    return JobCheckpoint(
        schema_version='1',
        job_id=payload['job_id'],
        grant_id=payload['grant_id'],
        current_state=payload['current_state'],
        state_revision=payload['state_revision'],
        last_event_id=payload['last_event_id'],
        attempt_counts=MappingProxyType(dict(payload['attempt_counts'])),
        updated_at=payload['updated_at'],
        blocked_from_state=payload['blocked_from_state'],
    )


def serialize_event(event) -> bytes:
    if not isinstance(event, JobEvent):
        raise ValueError('INVALID_EVENT')
    payload = {
        'event_id': event.event_id,
        'job_id': event.job_id,
        'prior_state': event.prior_state,
        'next_state': event.next_state,
        'state_revision': event.state_revision,
        'evidence_refs': list(event.evidence_refs),
        'actor': event.actor,
        'decision': event.decision,
        'timestamp': event.timestamp,
        'reason': event.reason,
    }
    return canonical(payload)


def deserialize_event(data, *, expected_job_id=None) -> JobEvent:
    if not isinstance(data, bytes) or len(data) > 65536:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    def _pairs_hook(pairs):
        res = {}
        for k, v in pairs:
            if k in res:
                raise CheckpointError('AUTH_SCHEMA_INVALID')
            res[k] = v
        return res

    def _reject(_):
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    try:
        payload = json.loads(
            data.decode('utf-8'),
            object_pairs_hook=_pairs_hook,
            parse_float=_reject,
            parse_constant=_reject,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CheckpointError('AUTH_SCHEMA_INVALID') from exc

    if not isinstance(payload, dict):
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if set(payload.keys()) != {field.name for field in fields(JobEvent)}:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    for key in ('event_id', 'job_id', 'actor', 'decision'):
        val = payload[key]
        if type(val) is not str or not val:
            raise CheckpointError('AUTH_SCHEMA_INVALID')

    if payload['prior_state'] not in KNOWN_STATES or payload['next_state'] not in KNOWN_STATES:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if type(payload['state_revision']) is not int or payload['state_revision'] < 1:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    refs = payload['evidence_refs']
    if type(refs) is not list or len(refs) == 0:
        raise CheckpointError('AUTH_SCHEMA_INVALID')
    if not all(type(item) is str and bool(item) for item in refs):
        raise CheckpointError('AUTH_SCHEMA_INVALID')
    if len(set(refs)) != len(refs):
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if not _validate_timestamp(payload['timestamp']):
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if type(payload['reason']) is not str:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    if expected_job_id is not None and payload['job_id'] != expected_job_id:
        raise CheckpointError('AUTH_SCHEMA_INVALID')

    return JobEvent(
        event_id=payload['event_id'],
        job_id=payload['job_id'],
        prior_state=payload['prior_state'],
        next_state=payload['next_state'],
        state_revision=payload['state_revision'],
        evidence_refs=tuple(payload['evidence_refs']),
        actor=payload['actor'],
        decision=payload['decision'],
        timestamp=payload['timestamp'],
        reason=payload['reason'],
    )

