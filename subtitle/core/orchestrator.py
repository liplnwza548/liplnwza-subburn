"""Single Supported Automatic Entrypoint for P2-006 (Guarded One-Clip Render).

This module defines the orchestration layer for executing a guarded one-clip subtitle
render pipeline. The main entrypoint function orchestrate_render_job will be introduced
in a subsequent part.

Approved Architectural Decisions:
- (Q1) auto_mode=False must return success=False and deny_code='AUTO_MODE_DISABLED';
  it must never return success.
- (Q5) expected_audio_sha256 must be an explicit parameter provided by the caller;
  it must never be computed automatically from the staged file.
- (Q6) If caller does not pass an existing_binding, the orchestrator must inspect
  the ReplayLedger itself; if the ledger cannot be read, it must fail-closed.
- (C2) New code is strictly forbidden from writing or storing files in /tmp. The sole
  written accepted exception is the legacy burner chip_burn.py at line 246, which
  hardcodes dir='/tmp'.
- (C3) The render evidence record must never be named as if it were a numbered gate; call it "render evidence record" only.
- (C4) 'RENDER_EXECUTION_FAILED' is defined and accepted as a new deny code in this module.
- (C5) expected_script_sha256 is computed inline via hashlib.sha256(text.encode('utf-8')).hexdigest();
  never import the private helper _sha256_text from gates.py.
- (C6) The orchestrator creates ev_transcript_{job_id} evidence independently, distinct
  from ev_g1_{job_id}.
- (C12) ใช้ค่า now เดียวกันกับ verify() ทุกครั้ง ห้ามอ่านนาฬิกาจริงในโมดูลนี้เด็ดขาด (ห้าม datetime.now() / time.time()).

Deviation from Plan §4.7.2:
The approved plan section §4.7.2 originally called for creating an EvidenceRecord with
stage='RENDERED'. However, STAGES in evidence.py does not include 'RENDERED', and
create_evidence_record will reject it with EvidenceError. Modifying evidence.py is forbidden
under ticket P2-006. Therefore, no EvidenceRecord with stage='RENDERED' will be created, and
it must not be mislabeled as stage='RENDER_PERMITTED'. The render execution outcome is
recorded strictly in the state transition event to RENDERED in events.jsonl and in
OrchestratorResult.render_execution, deferring to ticket P2-007 to decide whether STAGES
should be expanded.

P2-009 (VM/Colab adapters + retry/QC wiring) — deviations from the approved plan,
all documented in docs/adapters.md:
- (D-I) resolve_source takes source_path: a source_ref has no filename, so the adapter
  cannot find the file from it alone.
- (D-II) deliver_review takes expected_destination (the authorized review destination
  from the grant) plus now; without the former, rule A4 (same-origin delivery) could
  not be checked at all.
- (D-III) The entrypoint now always runs QC after a successful render (P2-007 D5,
  plan section 5.4), so the happy-path end state is READY_FOR_REVIEW, not RENDERED.
  Existing tests 1, 2 and 17 were updated accordingly (mocked QC in 1-2, real QC
  in 17); no test count changed.
- (D-IV) Re-verification inside the retry loop is a direct verify() call with
  action='render_candidate', not a re-run of _step_reverify_g3 (G3): G3 asserts the
  checkpoint is ALIGNED and transitions to RENDER_PERMITTED, both of which the
  reducer rejects from RENDER_PERMITTED. The re-verify uses the same inputs and
  the same single now as G3.
- (D-V) mfa_tool_available is informational only and never blocks a job: the
  pipeline receives the MFA artifact from the caller and never executes MFA.
- (D-VI) _step_auto_mode_render also takes `now` (the datetime the caller
  passed in); verify() needs it, and now_iso alone is not enough.
- (D-VII) When a QC system fault is retried, the loop re-enters from RENDERED:
  the next successful render skips the RENDERED transition (the reducer
  rejects a same-state transition), the candidate file is overwritten, and
  the fresh QC evidence carries the new digest.

Evidence Hash Notes:
- Gate G1 calculates artifact_sha256 from sha256 of transcript_data['text'] (gates.py:177),
  not from the raw JSON file hash.
- Gate G2 calculates artifact_sha from sha256 of mfa_artifact_bytes (gates.py:412).

Staging layout (from intake.allocate_staging_paths, verified against real code):
- Job folder <base>/jobs/<job_id>/ holds job_spec.json, checkpoint.json, events.jsonl and evidence/.
- <base>/jobs/<job_id>/work/ (spec.staging.job_work_dir) holds the staged source plus adapter
  scratch files (staged_script.txt, mfa_words.json, tmp/); it never holds the checkpoint or log.
- <base>/jobs/<job_id>/output/ holds candidate.mp4.
  Use _job_dir(spec) to derive the job folder; never pass job_work_dir as the job folder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time

from subtitle.adapters.base import CapabilityReport, PlatformAdapter
from subtitle.adapters.colab import ColabAdapter
from subtitle.adapters.local import LocalAdapter

from .audit import (
    AuditError,
    ReplayLedger,
    append_event,
    atomic_write_file,
    atomic_write_json,
    load_checkpoint,
    save_checkpoint,
)
from .authorization import Decision, verify
from .evidence import (
    CheckRecord,
    EvidenceRecord,
    create_evidence_record,
    save_evidence_record,
)
from .gates import (
    check_mfa_provenance,
    check_pre_render_gate,
    check_transcript_evidence,
)
from .intake import IntakeResult, process_job_intake
from .job import JobSpec
from .qc import QCResult, run_qc
from .renderer_adapter import RenderExecutionResult, render_one_clip
from .retry import (
    RetryDecision,
    RetryEvent,
    classify_failure,
    create_retry_event,
    decide_retry,
)
from .state import JobCheckpoint, TransitionDecision, increment_attempt, reduce_transition


class OrchestratorError(Exception):
    """ข้อผิดพลาดระดับโครงสร้างการประสานงานของ Orchestrator"""


AUTO_MODE_DISABLED = 'AUTO_MODE_DISABLED'
RENDER_EXECUTION_FAILED = 'RENDER_EXECUTION_FAILED'
SYSTEM_DEPENDENCY_MISSING = 'SYSTEM_DEPENDENCY_MISSING'
# P2-009: the single failure scope for the render+QC wiring. Quota is
# counted per this key only, in checkpoint.attempt_counts.
FAILURE_KEY = 'render_candidate'
_DIGEST = re.compile(r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class _QCOutcome:
    """Outcome of _step_qc_and_review.

    Either ``result`` holds a final OrchestratorResult (READY_FOR_REVIEW /
    BLOCKED_NEEDS_REVIEW / FAILED_JOB / CANCELLED / BLOCKED_AUTHORIZATION),
    or ``retry_decision`` holds a RetryDecision when QC reported a system
    fault and the retry quota remains. In the retry case NO transition
    has been made yet (FAILED_JOB is terminal; plan section 4.2.4 / C9),
    and the caller must re-enter the render loop.
    """

    result: OrchestratorResult | None
    retry_decision: RetryDecision | None
    qc_result: QCResult | None
    evidence_ids: tuple[str, ...] = ()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _preflight_capabilities(
    *,
    adapter: PlatformAdapter,
    now_iso: str,
    review_destination: dict,
) -> OrchestratorResult | None:
    """Pre-flight capability check, before intake (plan section 4.3
    point 1, decisions D4 / D6).

    Returns None when the run may proceed, else a deny
    OrchestratorResult. No job folder exists yet here, so no checkpoint
    is attached and no fake folder is ever created to record the failure
    (like _step_pre_verify).

    A missing required binary (ffmpeg / ffprobe) stops the whole run as
    FAILED_SYSTEM: a missing binary would hit every clip, not one clip.
    A missing simulated Drive credential only stops the run as
    BLOCKED_NEEDS_REVIEW when the review destination is actually a
    Drive destination (the owner can fix it); rendering does not need
    the credential, so other destinations proceed.
    """
    report: CapabilityReport = adapter.check_capabilities(now=now_iso)
    if report.blocked:
        return _deny_result(
            final_state='FAILED_SYSTEM',
            deny_code=report.blocking_reason or SYSTEM_DEPENDENCY_MISSING,
            reason=f"Pre-flight capability check failed: {report.notes}",
            spec=None,
            checkpoint=None,
            evidence_ids=(),
        )
    if (
        isinstance(adapter, ColabAdapter)
        and report.capabilities.drive_access == 'unavailable'
        and review_destination.get('provider') == 'drive'
    ):
        return _deny_result(
            final_state='BLOCKED_NEEDS_REVIEW',
            deny_code='ADAPTER_NOT_READY',
            reason='Simulated Drive credential is missing; the owner can fix it and resubmit the job',
            spec=None,
            checkpoint=None,
            evidence_ids=(),
        )
    return None


def _record_attempt(
    *,
    spec: JobSpec,
    checkpoint: JobCheckpoint,
    failure_key: str,
    render_result: RenderExecutionResult,
    now_iso: str,
    job_dir: Path | str,
    before_digests: dict[str, str],
) -> tuple[JobCheckpoint, RetryEvent]:
    """Record one render attempt (plan section 5.3, correction C1).

    Layer 1, the durable ledger: ``increment_attempt`` + atomic
    ``save_checkpoint``. This count is the single source of truth for
    the retry quota and survives a restart (it lives in
    checkpoint.json). Layer 2, evidence (not a ledger): one RetryEvent
    JSON per attempt at
    ``<job_dir>/evidence/retry_render_candidate_{n:03d}.json``,
    written with ``atomic_write_file``. No code reads that file to
    decide quota; quota reads only ``checkpoint.attempt_counts``.

    RetryEvent is NEVER written into events.jsonl: ``append_event``
    accepts JobEvent only, and ``audit.py`` / ``state.py`` were not
    changed for this ticket.

    ``before_digests`` is captured by the caller BEFORE the render
    (the old candidate digest, if any); the after digests are computed
    here from disk. The staged source file is hashed from disk: if its
    digest differs from ``spec.source.sha256``, the render mutated the
    input and the run fails closed with SOURCE_MUTATED_ERROR.
    """
    job_dir = Path(job_dir)
    evidence_dir = job_dir / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)

    staged_path = Path(spec.staging.staged_input_path)
    if staged_path.is_file():
        # The input must never be modified by a render attempt. (In a
        # real entrypoint run intake always creates this file; a direct
        # _step_auto_mode_render call that bypasses intake has no file
        # to check, so the check is skipped rather than invented.)
        on_disk_source = _sha256_file(staged_path)
        if on_disk_source != spec.source.sha256:
            raise OrchestratorError(
                'SOURCE_MUTATED_ERROR: staged source digest changed during render; '
                'the input must never be modified by a render attempt',
            )
    on_disk_source = spec.source.sha256

    attempt_count = checkpoint.attempt_counts.get(failure_key, 0) + 1
    new_checkpoint = increment_attempt(checkpoint, failure_key)
    save_checkpoint(job_dir / 'checkpoint.json', new_checkpoint)

    after_digests: dict[str, str] = {'source': on_disk_source}
    if render_result.success and render_result.output_sha256 is not None:
        after_digests['candidate'] = render_result.output_sha256

    render_ok = (
        render_result.success
        and render_result.output_path is not None
        and Path(render_result.output_path).is_file()
        and Path(render_result.output_path).stat().st_size > 0
        and render_result.output_sha256 is not None
    )
    if render_ok:
        classification, failure_code = 'NON_RETRYABLE', 'NO_FAILURE'
        result_value = 'SUCCESS'
        reason = 'Render attempt completed successfully'
    else:
        classification, _target, failure_code = classify_failure(
            deny_code=RENDER_EXECUTION_FAILED,
        )
        result_value = 'FAILED'
        reason = (
            render_result.error_message or 'Renderer execution failed'
        )

    event = create_retry_event(
        job_id=spec.job_id,
        failure_key=failure_key,
        failure_code=failure_code,
        classification=classification,
        attempt_count=attempt_count,
        max_allowance=spec.retry_allowance,
        exact_action='render:render_candidate',
        before_artifact_digests=dict(before_digests),
        after_artifact_digests=after_digests,
        result=result_value,
        now_iso=now_iso,
        reason=reason,
    )
    payload = {
        'schema_version': event.schema_version,
        'event_id': event.event_id,
        'job_id': event.job_id,
        'failure_code': event.failure_code,
        'failure_scope': event.failure_scope,
        'classification': event.classification,
        'attempt_count': event.attempt_count,
        'max_allowance': event.max_allowance,
        'exact_action': event.exact_action,
        'before_artifact_digests': dict(event.before_artifact_digests),
        'after_artifact_digests': dict(event.after_artifact_digests),
        'result': event.result,
        'timestamp': event.timestamp,
        'reason': event.reason,
    }
    atomic_write_file(
        evidence_dir / f"retry_{failure_key}_{attempt_count:03d}.json",
        json.dumps(
            payload, sort_keys=True, ensure_ascii=True, indent=2,
        ).encode('utf-8'),
    )
    return (new_checkpoint, event)


@dataclass(frozen=True)
class OrchestratorResult:
    """ผลลัพธ์การรันงานจาก Orchestrator"""

    success: bool
    final_state: str
    deny_code: str | None
    reason: str
    spec: JobSpec | None
    checkpoint: JobCheckpoint | None
    evidence_ids: tuple[str, ...]
    renderer_invoked: bool
    render_execution: RenderExecutionResult | None


def _sha256_text_value(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _now_iso(now: datetime) -> str:
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset().total_seconds() != 0
    ):
        raise OrchestratorError('now must be an aware UTC datetime')
    return now.strftime('%Y-%m-%dT%H:%M:%SZ')


def _human_check_record() -> CheckRecord:
    return CheckRecord(
        'human_audio_verification',
        'NOT_CHECKED',
        None,
        'audited_by_human',
        None,
        'Automated pipeline only; no human listening has been performed.',
    )


def _deny_result(
    *,
    final_state: str,
    deny_code: str | None,
    reason: str,
    spec: JobSpec | None = None,
    checkpoint: JobCheckpoint | None = None,
    evidence_ids: tuple[str, ...] = (),
    renderer_invoked: bool = False,
    render_execution: RenderExecutionResult | None = None,
) -> OrchestratorResult:
    return OrchestratorResult(
        success=False,
        final_state=final_state,
        deny_code=deny_code,
        reason=reason,
        spec=spec,
        checkpoint=checkpoint,
        evidence_ids=evidence_ids,
        renderer_invoked=renderer_invoked,
        render_execution=render_execution,
    )


def _advance(
    checkpoint: JobCheckpoint,
    *,
    target_state: str,
    event_id: str,
    evidence_refs: tuple[str, ...] | list[str],
    actor: str,
    decision: str,
    now: str,
    reason: str,
    job_dir: Path | str,
) -> tuple[JobCheckpoint | None, TransitionDecision]:
    transition_decision = reduce_transition(
        checkpoint,
        target_state,
        event_id=event_id,
        evidence_refs=evidence_refs,
        actor=actor,
        decision=decision,
        now=now,
        reason=reason,
    )
    if not transition_decision.accepted:
        return (None, transition_decision)

    if transition_decision.event is None:
        return (checkpoint, transition_decision)

    if transition_decision.next_state == 'BLOCKED_NEEDS_REVIEW':
        blocked_from_state = transition_decision.event.prior_state
    elif checkpoint.current_state == 'BLOCKED_NEEDS_REVIEW':
        blocked_from_state = None
    else:
        blocked_from_state = checkpoint.blocked_from_state

    new_checkpoint = replace(
        checkpoint,
        current_state=transition_decision.next_state,
        state_revision=transition_decision.new_revision,
        last_event_id=transition_decision.event.event_id,
        updated_at=now,
        blocked_from_state=blocked_from_state,
    )
    append_event(Path(job_dir) / 'events.jsonl', transition_decision.event)
    save_checkpoint(Path(job_dir) / 'checkpoint.json', new_checkpoint)
    return (new_checkpoint, transition_decision)


def _save_gate_evidence(job_dir: Path | str, record: EvidenceRecord) -> Path:
    return save_evidence_record(job_dir, record)


def _job_dir(spec: JobSpec) -> Path:
    """Return the job folder holding job_spec.json, checkpoint.json, events.jsonl and evidence/.

    Layout (from intake.allocate_staging_paths): job_work_dir is
    <base>/jobs/<job_id>/work/ (staged source + adapter scratch); the job
    folder itself is its parent, <base>/jobs/<job_id>/, which holds the
    frozen spec, the checkpoint, the event log and the evidence directory.
    """
    return Path(spec.staging.job_work_dir).parent


def _resolve_existing_binding(
    *,
    base_staging_dir: Path | str,
    grant_id: str,
    caller_binding: str | None,
) -> str | None:
    if caller_binding is not None:
        return caller_binding
    try:
        ledger = ReplayLedger(Path(base_staging_dir) / 'replay_ledger.json')
        return ledger.get_binding(grant_id)
    except AuditError as exc:
        raise OrchestratorError(f'AUDIT_CORRUPT: {exc}') from exc


def _write_transcript_artifact(
    *,
    job_dir: Path | str,
    transcript_data: dict,
) -> Path:
    evidence_dir = Path(job_dir) / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target_path = evidence_dir / 'transcript_raw.json'
    atomic_write_json(target_path, transcript_data)
    return target_path


def _write_mfa_artifact(
    *,
    job_dir: Path | str,
    mfa_artifact_bytes: bytes,
) -> Path:
    evidence_dir = Path(job_dir) / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target_path = evidence_dir / 'mfa_artifact.json'
    atomic_write_file(target_path, mfa_artifact_bytes)
    return target_path


def _create_transcript_record(
    *,
    spec: JobSpec,
    script_text: str,
    transcript_data: dict,
    tool_metadata: dict,
    now_iso: str,
) -> EvidenceRecord:
    if (
        not isinstance(transcript_data, dict)
        or 'text' not in transcript_data
        or not isinstance(transcript_data['text'], str)
    ):
        raise OrchestratorError("transcript_data must contain a str 'text' field")

    raw_exit_code = tool_metadata.get('exit_code', 0)
    observed_exit_code = raw_exit_code if type(raw_exit_code) is int else 0

    return create_evidence_record(
        evidence_id=f"ev_transcript_{spec.job_id}",
        job_id=spec.job_id,
        stage='TRANSCRIBED',
        tool=tool_metadata['processor'],
        tool_version=tool_metadata['model_version'],
        method=tool_metadata['method'],
        command=tuple(tool_metadata.get('command', ())),
        input_digest_refs={
            'source_media_sha256': spec.source.sha256,
            'script_sha256': _sha256_text_value(script_text),
        },
        artifact_sha256=_sha256_text_value(transcript_data['text']),
        artifact_relpath='evidence/transcript_raw.json',
        captured_at=tool_metadata.get('captured_at', now_iso),
        exit_code=tool_metadata.get('exit_code'),
        media_duration_seconds=None,
        checks=(
            CheckRecord(
                'transcript_captured',
                'PASSED',
                observed_exit_code,
                0,
                None,
                '',
            ),
            _human_check_record(),
        ),
        notes='',
    )


def _step_pre_verify(
    *,
    grant_bytes: bytes,
    status_bytes: bytes,
    trusted_grant_keys: dict,
    trusted_status_keys: dict,
    now,
    job_id: str,
    source_ref: dict,
    style_id: str,
    style_overrides: dict,
    review_destination: dict,
    existing_binding,
) -> Decision | OrchestratorResult:
    decision = verify(
        grant_bytes,
        status_bytes,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        now=now,
        job_id=job_id,
        source_ref=source_ref,
        style_id=style_id,
        style_overrides=style_overrides,
        review_destination=review_destination,
        action='intake_copy',
        processor=None,
        existing_binding=existing_binding,
    )
    if decision.allowed:
        return decision
    final_state = (
        'CANCELLED' if decision.reason == 'AUTH_REVOKED' else 'BLOCKED_AUTHORIZATION'
    )
    return _deny_result(
        final_state=final_state,
        deny_code=decision.reason,
        reason=f"Pre-intake verification failed: {decision.reason}",
    )


def _step_intake(
    *,
    grant_bytes: bytes,
    status_bytes: bytes,
    job_id: str,
    grant_id: str,
    source_ref: dict,
    style_id: str,
    style_overrides: dict,
    review_destination: dict,
    allowed_actions,
    allowed_processors,
    retry_allowance: int,
    source_path,
    trusted_source_root,
    base_staging_dir,
    trusted_grant_keys: dict,
    trusted_status_keys: dict,
    now,
    now_iso: str,
    existing_binding,
) -> tuple[JobSpec, JobCheckpoint] | OrchestratorResult:
    intake_result = process_job_intake(
        grant_bytes,
        status_bytes,
        job_id=job_id,
        grant_id=grant_id,
        source_ref=source_ref,
        style_id=style_id,
        style_overrides=style_overrides,
        review_destination=review_destination,
        allowed_actions=allowed_actions,
        allowed_processors=allowed_processors,
        retry_allowance=retry_allowance,
        source_path=source_path,
        trusted_source_root=trusted_source_root,
        base_staging_dir=base_staging_dir,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        now=now,
        existing_binding=existing_binding,
    )
    if not intake_result.success:
        return _deny_result(
            final_state='FAILED_JOB',
            deny_code=intake_result.deny_reason,
            reason=f"Intake failed: {intake_result.deny_reason}",
        )

    spec = intake_result.spec
    job_dir = _job_dir(spec)
    checkpoint = load_checkpoint(job_dir / 'checkpoint.json', expected_job_id=job_id)

    if (
        checkpoint is None
        or checkpoint.current_state != 'INTAKE'
        or checkpoint.state_revision != 1
    ):
        return _deny_result(
            final_state='FAILED_JOB',
            deny_code='AUDIT_CORRUPT',
            reason='Intake checkpoint missing or inconsistent',
            spec=spec,
        )

    try:
        ledger = ReplayLedger(Path(base_staging_dir) / 'replay_ledger.json')
        bound = ledger.bind_grant(grant_id, job_id, now_iso)
    except AuditError as exc:
        raise OrchestratorError(f'AUDIT_CORRUPT: {exc}') from exc

    if not bound:
        new_checkpoint, _ = _advance(
            checkpoint,
            target_state='BLOCKED_AUTHORIZATION',
            event_id=f"evt_block_auth_{job_id}",
            evidence_refs=('deny:GRANT_REPLAY',),
            actor='orchestrator',
            decision='DENIED',
            now=now_iso,
            reason='Grant already bound to a different job',
            job_dir=job_dir,
        )
        return _deny_result(
            final_state='BLOCKED_AUTHORIZATION',
            deny_code='GRANT_REPLAY',
            reason='Grant already bound to a different job',
            spec=spec,
            checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
        )

    return (spec, checkpoint)


def _step_to_authorized(
    *,
    spec: JobSpec,
    checkpoint: JobCheckpoint,
    now_iso: str,
) -> JobCheckpoint | OrchestratorResult:
    job_dir = _job_dir(spec)
    new_checkpoint, trans = _advance(
        checkpoint,
        target_state='AUTHORIZED',
        event_id=f"evt_auth_{checkpoint.job_id}",
        evidence_refs=('ev_grant_verified',),
        actor='orchestrator',
        decision='AUTHORIZED',
        now=now_iso,
        reason='Grant verified for job execution',
        job_dir=job_dir,
    )
    if new_checkpoint is None:
        return _deny_result(
            final_state=checkpoint.current_state,
            deny_code=trans.reason,
            reason=f"Transition to AUTHORIZED rejected: {trans.reason}",
            spec=spec,
            checkpoint=checkpoint,
        )
    return new_checkpoint


def _step_transcript_g1(
    *,
    spec: JobSpec,
    checkpoint: JobCheckpoint,
    transcript_data: dict,
    script_text: str,
    script_lines: list,
    transcript_tool_metadata: dict,
    now_iso: str,
) -> tuple[JobCheckpoint, tuple[str, ...]] | OrchestratorResult:
    job_dir = _job_dir(spec)

    _write_transcript_artifact(job_dir=job_dir, transcript_data=transcript_data)
    transcript_record = _create_transcript_record(
        spec=spec,
        script_text=script_text,
        transcript_data=transcript_data,
        tool_metadata=transcript_tool_metadata,
        now_iso=now_iso,
    )
    _save_gate_evidence(job_dir, transcript_record)
    collected = [transcript_record.evidence_id]

    new_checkpoint, trans = _advance(
        checkpoint,
        target_state='TRANSCRIBED',
        event_id=f"evt_transcribed_{spec.job_id}",
        evidence_refs=(transcript_record.evidence_id,),
        actor='orchestrator',
        decision='TRANSCRIBED',
        now=now_iso,
        reason='Transcription evidence attached',
        job_dir=job_dir,
    )
    if new_checkpoint is None:
        return _deny_result(
            final_state=checkpoint.current_state,
            deny_code=trans.reason,
            reason=f"Transition to TRANSCRIBED rejected: {trans.reason}",
            spec=spec,
            checkpoint=checkpoint,
            evidence_ids=tuple(collected),
        )

    gate = check_transcript_evidence(
        spec=spec,
        transcript_data=transcript_data,
        script_text=script_text,
        script_lines=script_lines,
        tool_metadata=transcript_tool_metadata,
        artifact_relpath='evidence/transcript_raw.json',
        evidence_id=f"ev_g1_{spec.job_id}",
        now=now_iso,
    )
    if gate.evidence is not None:
        _save_gate_evidence(job_dir, gate.evidence)
        collected.append(gate.evidence.evidence_id)

    if not gate.passed:
        blocked_checkpoint, btrans = _advance(
            new_checkpoint,
            target_state='BLOCKED_NEEDS_REVIEW',
            event_id=f"evt_block_g1_{spec.job_id}",
            evidence_refs=(f"deny:{gate.deny_code}",),
            actor='orchestrator',
            decision='DENIED',
            now=now_iso,
            reason=gate.reason,
            job_dir=job_dir,
        )
        return _deny_result(
            final_state='BLOCKED_NEEDS_REVIEW',
            deny_code=gate.deny_code,
            reason=gate.reason,
            spec=spec,
            checkpoint=blocked_checkpoint if blocked_checkpoint is not None else new_checkpoint,
            evidence_ids=tuple(collected),
        )

    if gate.evidence is None:
        raise OrchestratorError('G1 passed without evidence')

    final_checkpoint, trans2 = _advance(
        new_checkpoint,
        target_state='SCRIPT_EVIDENCE_CHECKED',
        event_id=f"evt_g1_{spec.job_id}",
        evidence_refs=(gate.evidence.evidence_id,),
        actor='orchestrator',
        decision='PASSED',
        now=now_iso,
        reason='Gate G1 transcript evidence check passed',
        job_dir=job_dir,
    )
    if final_checkpoint is None:
        return _deny_result(
            final_state=new_checkpoint.current_state,
            deny_code=trans2.reason,
            reason=f"Transition to SCRIPT_EVIDENCE_CHECKED rejected: {trans2.reason}",
            spec=spec,
            checkpoint=new_checkpoint,
            evidence_ids=tuple(collected),
        )

    return (final_checkpoint, tuple(collected))


def _step_mfa_g2(
    *,
    spec,
    checkpoint,
    mfa_artifact_bytes: bytes,
    expected_script_sha256: str,
    expected_audio_sha256: str,
    media_duration_seconds: float,
    script_text: str,
    mfa_tool_metadata: dict,
    now_iso: str,
) -> tuple[JobCheckpoint, EvidenceRecord, tuple[str, ...]] | OrchestratorResult:
    job_dir = _job_dir(spec)
    _write_mfa_artifact(job_dir=job_dir, mfa_artifact_bytes=mfa_artifact_bytes)
    gate = check_mfa_provenance(
        spec=spec,
        mfa_artifact_bytes=mfa_artifact_bytes,
        expected_script_sha256=expected_script_sha256,
        expected_audio_sha256=expected_audio_sha256,
        media_duration_seconds=media_duration_seconds,
        script_text=script_text,
        tool_metadata=mfa_tool_metadata,
        artifact_relpath='evidence/mfa_artifact.json',
        evidence_id=f"ev_g2_{spec.job_id}",
        now=now_iso,
    )
    collected = []
    if gate.evidence is not None:
        _save_gate_evidence(job_dir, gate.evidence)
        collected.append(gate.evidence.evidence_id)

    if not gate.passed:
        blocked_checkpoint, btrans = _advance(
            checkpoint,
            target_state='BLOCKED_NEEDS_REVIEW',
            event_id=f"evt_block_g2_{spec.job_id}",
            evidence_refs=(f"deny:{gate.deny_code}",),
            actor='orchestrator',
            decision='DENIED',
            now=now_iso,
            reason=gate.reason,
            job_dir=job_dir,
        )
        return _deny_result(
            final_state='BLOCKED_NEEDS_REVIEW',
            deny_code=gate.deny_code,
            reason=gate.reason,
            spec=spec,
            checkpoint=blocked_checkpoint if blocked_checkpoint is not None else checkpoint,
            evidence_ids=tuple(collected),
        )

    if gate.evidence is None:
        raise OrchestratorError('G2 passed without evidence')

    final_checkpoint, trans2 = _advance(
        checkpoint,
        target_state='ALIGNED',
        event_id=f"evt_g2_{spec.job_id}",
        evidence_refs=(gate.evidence.evidence_id,),
        actor='orchestrator',
        decision='PASSED',
        now=now_iso,
        reason='Gate G2 MFA provenance check passed',
        job_dir=job_dir,
    )
    if final_checkpoint is None:
        return _deny_result(
            final_state=checkpoint.current_state,
            deny_code=trans2.reason,
            reason=f"Transition to ALIGNED rejected: {trans2.reason}",
            spec=spec,
            checkpoint=checkpoint,
            evidence_ids=tuple(collected),
        )

    return (final_checkpoint, gate.evidence, tuple(collected))


def _step_reverify_g3(
    *,
    spec,
    checkpoint,
    grant_bytes: bytes,
    status_bytes: bytes,
    trusted_grant_keys: dict,
    trusted_status_keys: dict,
    now,
    script_text: str,
    cues: list,
    mfa_evidence,
    now_iso: str,
) -> tuple[JobCheckpoint, tuple[str, ...]] | OrchestratorResult:
    job_dir = _job_dir(spec)
    fresh = verify(
        grant_bytes,
        status_bytes,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        now=now,
        job_id=spec.job_id,
        source_ref={
            'provider': spec.source.provider,
            'id': spec.source.id,
            'revision': spec.source.revision,
            'sha256': spec.source.sha256,
        },
        style_id=spec.style_id,
        style_overrides=dict(spec.style_overrides),
        review_destination=dict(spec.review_destination),
        action='render_candidate',
        processor=None,
        existing_binding=spec.job_id,
    )
    if not fresh.allowed:
        if fresh.reason == 'AUTH_REVOKED':
            new_checkpoint, _ = _advance(
                checkpoint,
                target_state='CANCELLED',
                event_id=f"evt_revoked_{spec.job_id}",
                evidence_refs=('deny:AUTH_REVOKED',),
                actor='orchestrator',
                decision='REVOKED',
                now=now_iso,
                reason='Job grant was revoked by issuer before render',
                job_dir=job_dir,
            )
            return _deny_result(
                final_state='CANCELLED',
                deny_code='AUTH_REVOKED',
                reason='Job grant was revoked by issuer before render',
                spec=spec,
                checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
            )
        new_checkpoint, _ = _advance(
            checkpoint,
            target_state='BLOCKED_AUTHORIZATION',
            event_id=f"evt_block_reverify_{spec.job_id}",
            evidence_refs=(f"deny:{fresh.reason}",),
            actor='orchestrator',
            decision='DENIED',
            now=now_iso,
            reason=f"Fresh verification failed: {fresh.reason}",
            job_dir=job_dir,
        )
        return _deny_result(
            final_state='BLOCKED_AUTHORIZATION',
            deny_code=fresh.reason,
            reason=f"Fresh verification failed: {fresh.reason}",
            spec=spec,
            checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
        )

    gate = check_pre_render_gate(
        spec=spec,
        checkpoint=checkpoint,
        mfa_evidence=mfa_evidence,
        script_text=script_text,
        cues=cues,
        authorization_decision=fresh,
        evidence_id=f"ev_g3_{spec.job_id}",
        now=now_iso,
    )
    collected = []
    if gate.evidence is not None:
        _save_gate_evidence(job_dir, gate.evidence)
        collected.append(gate.evidence.evidence_id)

    if not gate.passed:
        if gate.deny_code == 'AUTH_REVOKED':
            new_checkpoint, _ = _advance(
                checkpoint,
                target_state='CANCELLED',
                event_id=f"evt_revoked_{spec.job_id}",
                evidence_refs=('deny:AUTH_REVOKED',),
                actor='orchestrator',
                decision='REVOKED',
                now=now_iso,
                reason='Job grant was revoked by issuer before render',
                job_dir=job_dir,
            )
            return _deny_result(
                final_state='CANCELLED',
                deny_code='AUTH_REVOKED',
                reason='Job grant was revoked by issuer before render',
                spec=spec,
                checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
            )
        blocked_checkpoint, btrans = _advance(
            checkpoint,
            target_state='BLOCKED_NEEDS_REVIEW',
            event_id=f"evt_block_g3_{spec.job_id}",
            evidence_refs=(f"deny:{gate.deny_code}",),
            actor='orchestrator',
            decision='DENIED',
            now=now_iso,
            reason=gate.reason,
            job_dir=job_dir,
        )
        return _deny_result(
            final_state='BLOCKED_NEEDS_REVIEW',
            deny_code=gate.deny_code,
            reason=gate.reason,
            spec=spec,
            checkpoint=blocked_checkpoint if blocked_checkpoint is not None else checkpoint,
            evidence_ids=tuple(collected),
        )

    if gate.evidence is None:
        raise OrchestratorError('G3 passed without evidence')

    final_checkpoint, trans2 = _advance(
        checkpoint,
        target_state='RENDER_PERMITTED',
        event_id=f"evt_g3_{spec.job_id}",
        evidence_refs=(gate.evidence.evidence_id,),
        actor='orchestrator',
        decision='PASSED',
        now=now_iso,
        reason='Gate G3 pre-render check passed',
        job_dir=job_dir,
    )
    if final_checkpoint is None:
        return _deny_result(
            final_state=checkpoint.current_state,
            deny_code=trans2.reason,
            reason=f"Transition to RENDER_PERMITTED rejected: {trans2.reason}",
            spec=spec,
            checkpoint=checkpoint,
            evidence_ids=tuple(collected),
        )

    return (final_checkpoint, tuple(collected))


def _step_auto_mode_render(
    *,
    spec,
    checkpoint,
    script_text: str,
    mfa_artifact_bytes: bytes,
    fonts_dir,
    auto_mode: bool,
    evidence_ids: tuple[str, ...],
    now_iso: str,
    now,
    grant_bytes: bytes,
    status_bytes: bytes,
    trusted_grant_keys: dict,
    trusted_status_keys: dict,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> OrchestratorResult:
    """Guarded render with the P2-009 retry loop (plan sections 4.2 /
    5.2.3). Failure scope is the single key ``'render_candidate'``.

    Per attempt: (1) re-verify fresh (except attempt 1, which reuses the
    fresh decision from _step_reverify_g3 — never verify twice in one
    attempt); (2) render via render_one_clip (timeout 600 s); (3)
    record the attempt immediately via _record_attempt (success or
    fail); (4) on success + complete output, transition to RENDERED;
    (5) on failure, decide_retry on the checkpoint AFTER the increment
    — retry means NO state change (stay RENDER_PERMITTED, or stay
    RENDERED when re-entered after a QC system fault), sleep via
    sleep_fn, then loop; exhausted or non-retryable means transition
    to the decision's target_state and return the deny result.

    The quota formula is the single correct one:
    attempt_counts.get(key, 0) < (1 + retry_allowance), checked inside
    decide_retry AFTER the increment. With allowance 2: attempt 1 fail
    (counts 1) -> retry; attempt 2 fail (counts 2) -> retry; attempt 3
    fail (counts 3) -> exhausted -> FAILED_JOB. There is no 4th render.
    """
    if type(auto_mode) is not bool:
        raise OrchestratorError('auto_mode must be a bool')

    if not auto_mode:
        return _deny_result(
            final_state='RENDER_PERMITTED',
            deny_code=AUTO_MODE_DISABLED,
            reason='Job reached RENDER_PERMITTED; auto_mode is False (execution halted safely)',
            spec=spec,
            checkpoint=checkpoint,
            evidence_ids=evidence_ids,
            renderer_invoked=False,
            render_execution=None,
        )

    job_dir = _job_dir(spec)
    max_attempts = 1 + spec.retry_allowance
    attempt = checkpoint.attempt_counts.get(FAILURE_KEY, 0) + 1
    if attempt > max_attempts:
        # Re-entered with the quota already exhausted (e.g. after a QC
        # system fault on the last attempt): fail closed, do not render.
        return _deny_result(
            final_state='FAILED_JOB',
            deny_code=RENDER_EXECUTION_FAILED,
            reason='Retry quota exhausted for render_candidate before this attempt',
            spec=spec,
            checkpoint=checkpoint,
            evidence_ids=evidence_ids,
            renderer_invoked=False,
            render_execution=None,
        )

    while attempt <= max_attempts:
        if attempt > 1:
            # Re-verify fresh before every retry with the same inputs
            # and the same single now as _step_reverify_g3 (D-IV: a
            # direct verify() call, not a re-run of G3 — G3 asserts the
            # checkpoint is ALIGNED and transitions to RENDER_PERMITTED,
            # both rejected from RENDER_PERMITTED).
            fresh = verify(
                grant_bytes,
                status_bytes,
                trusted_grant_keys=trusted_grant_keys,
                trusted_status_keys=trusted_status_keys,
                now=now,
                job_id=spec.job_id,
                source_ref={
                    'provider': spec.source.provider,
                    'id': spec.source.id,
                    'revision': spec.source.revision,
                    'sha256': spec.source.sha256,
                },
                style_id=spec.style_id,
                style_overrides=dict(spec.style_overrides),
                review_destination=dict(spec.review_destination),
                action='render_candidate',
                processor=None,
                existing_binding=spec.job_id,
            )
            if not fresh.allowed:
                attempt_count = checkpoint.attempt_counts.get(FAILURE_KEY, 0)
                refs = (
                    f"deny:{fresh.reason}",
                    f"retry:{FAILURE_KEY}:attempts={attempt_count}",
                )
                if fresh.reason == 'AUTH_REVOKED':
                    new_checkpoint, _ = _advance(
                        checkpoint,
                        target_state='CANCELLED',
                        event_id=f"evt_revoked_{spec.job_id}",
                        evidence_refs=refs,
                        actor='orchestrator',
                        decision='REVOKED',
                        now=now_iso,
                        reason='Job grant was revoked by issuer before a retry render',
                        job_dir=job_dir,
                    )
                    return _deny_result(
                        final_state='CANCELLED',
                        deny_code='AUTH_REVOKED',
                        reason='Job grant was revoked by issuer before a retry render',
                        spec=spec,
                        checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
                        evidence_ids=evidence_ids,
                        renderer_invoked=True,
                        render_execution=None,
                    )
                new_checkpoint, _ = _advance(
                    checkpoint,
                    target_state='BLOCKED_AUTHORIZATION',
                    event_id=f"evt_block_reverify_{spec.job_id}",
                    evidence_refs=refs,
                    actor='orchestrator',
                    decision='BLOCKED',
                    now=now_iso,
                    reason=f"Re-verification before a retry render failed: {fresh.reason}",
                    job_dir=job_dir,
                )
                return _deny_result(
                    final_state='BLOCKED_AUTHORIZATION',
                    deny_code=fresh.reason,
                    reason=f"Re-verification before a retry render failed: {fresh.reason}",
                    spec=spec,
                    checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
                    evidence_ids=evidence_ids,
                    renderer_invoked=True,
                    render_execution=None,
                )

        # Capture the old candidate digest BEFORE the render overwrites
        # it; _record_attempt needs the pre-render state.
        before_digests: dict[str, str] = {'source': spec.source.sha256}
        candidate_path = Path(spec.staging.candidate_output_path)
        if candidate_path.is_file():
            before_digests['candidate'] = _sha256_file(candidate_path)

        result = render_one_clip(
            spec,
            script_text=script_text,
            mfa_artifact_bytes=mfa_artifact_bytes,
            fonts_dir=fonts_dir,
            timeout_seconds=600,
        )

        # Record the attempt immediately, success or fail, before any
        # retry decision (plan section 4.2.1 step 3).
        checkpoint, _retry_event = _record_attempt(
            spec=spec,
            checkpoint=checkpoint,
            failure_key=FAILURE_KEY,
            render_result=result,
            now_iso=now_iso,
            job_dir=job_dir,
            before_digests=before_digests,
        )
        attempt_count = checkpoint.attempt_counts.get(FAILURE_KEY, 0)

        out = Path(result.output_path) if result.output_path is not None else None
        output_ok = (
            result.success
            and out is not None
            and out.is_file()
            and out.stat().st_size > 0
            and out.stat().st_size == result.output_size_bytes
            and result.output_sha256 is not None
        )
        if output_ok:
            if checkpoint.current_state == 'RENDER_PERMITTED':
                new_checkpoint, trans = _advance(
                    checkpoint,
                    target_state='RENDERED',
                    event_id=f"evt_render_{spec.job_id}",
                    evidence_refs=(
                        f"render:{result.output_sha256}",
                        f"retry:{FAILURE_KEY}:attempts={attempt_count}",
                    ),
                    actor='orchestrator',
                    decision='RENDERED',
                    now=now_iso,
                    reason='Renderer completed candidate output successfully',
                    job_dir=job_dir,
                )
                if new_checkpoint is None:
                    return _deny_result(
                        final_state=checkpoint.current_state,
                        deny_code=trans.reason,
                        reason=f"Transition to RENDERED rejected: {trans.reason}",
                        spec=spec,
                        checkpoint=checkpoint,
                        evidence_ids=evidence_ids,
                        renderer_invoked=True,
                        render_execution=result,
                    )
                checkpoint = new_checkpoint
            elif checkpoint.current_state != 'RENDERED':
                # D-VII: re-entered after a QC system fault, the state
                # is already RENDERED; the reducer rejects a same-state
                # transition, so the candidate file is simply
                # overwritten and the fresh QC evidence carries the new
                # digest. Any other state here is a structural error.
                return _deny_result(
                    final_state=checkpoint.current_state,
                    deny_code='STATE_SKIP_FORBIDDEN',
                    reason='Render completed but the job is in an unexpected state for RENDERED',
                    spec=spec,
                    checkpoint=checkpoint,
                    evidence_ids=evidence_ids,
                    renderer_invoked=True,
                    render_execution=result,
                )
            return OrchestratorResult(
                success=True,
                final_state='RENDERED',
                deny_code=None,
                reason='One-clip render completed successfully',
                spec=spec,
                checkpoint=checkpoint,
                evidence_ids=evidence_ids,
                renderer_invoked=True,
                render_execution=result,
            )

        fail_reason = (
            result.error_message
            or 'Candidate output missing or inconsistent after successful render'
        )
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=FAILURE_KEY,
            deny_code=RENDER_EXECUTION_FAILED,
            retry_allowance=spec.retry_allowance,
        )
        if decision.should_retry:
            # No state change: decide_retry returns
            # target_state = checkpoint.current_state. Sleep via the
            # injectable sleep_fn, then loop to the next attempt.
            sleep_fn(decision.backoff_seconds)
            attempt += 1
            continue

        refs = [
            f"exit_code:{result.exit_code}",
            f"retry:{FAILURE_KEY}:attempts={attempt_count}",
        ]
        if decision.exhausted:
            refs.append(f"retry:{FAILURE_KEY}:exhausted")
        new_checkpoint, _ = _advance(
            checkpoint,
            target_state=decision.target_state,
            event_id=f"evt_fail_render_{spec.job_id}",
            evidence_refs=tuple(refs),
            actor='orchestrator',
            decision='FAILED',
            now=now_iso,
            reason=fail_reason,
            job_dir=job_dir,
        )
        return _deny_result(
            final_state=decision.target_state,
            deny_code=RENDER_EXECUTION_FAILED,
            reason=fail_reason,
            spec=spec,
            checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
            evidence_ids=evidence_ids,
            renderer_invoked=True,
            render_execution=result,
        )

    # Unreachable in practice (every path in the loop returns), but fail
    # closed rather than fall through.
    return _deny_result(
        final_state='FAILED_JOB',
        deny_code=RENDER_EXECUTION_FAILED,
        reason='Retry loop ended without a render outcome',
        spec=spec,
        checkpoint=checkpoint,
        evidence_ids=evidence_ids,
        renderer_invoked=True,
        render_execution=None,
    )


def _step_qc_and_review(
    *,
    spec,
    checkpoint,
    render_execution,
    script_sha256,
    grant_bytes: bytes,
    status_bytes: bytes,
    trusted_grant_keys: dict,
    trusted_status_keys: dict,
    now,
    now_iso: str,
    evidence_ids: tuple[str, ...],
) -> _QCOutcome:
    """Run fresh QC after a successful render and transition per its
    target_state (plan section 5.4; closes the P2-007 D5 gap).

    The authorization decision is verified fresh here, with the same
    inputs and the same single ``now`` as the render attempt's
    re-verify, because the decision from _step_reverify_g3 is not
    carried through the render loop. verify() is deterministic on
    identical inputs and the same now, so this cannot disagree with the
    render attempt's re-verify; the deny branches below are
    fail-closed defense, mirroring _step_reverify_g3.

    Transitions (all via _advance):
    - READY_FOR_REVIEW: RENDERED -> QC_EVALUATED -> READY_FOR_REVIEW.
    - BLOCKED_NEEDS_REVIEW (content fault): RENDERED -> QC_EVALUATED ->
      BLOCKED_NEEDS_REVIEW. Never retried.
    - FAILED_JOB (system fault): C9 — do NOT transition yet. decide_retry
      decides first; if retry is allowed, return a retry signal with NO
      transition (the caller re-enters the render loop from RENDERED);
      only when retry is not allowed does it transition RENDERED ->
      QC_EVALUATED -> FAILED_JOB.

    Event ids carry the attempt number (evt_qc_<job>_a<n:03d>): QC can
    run more than once for one job (after a retried render), and the
    reducer treats a repeated event id as an idempotent no-op, which
    would silently swallow the second QC_EVALUATED transition.
    """
    job_dir = _job_dir(spec)
    fresh = verify(
        grant_bytes,
        status_bytes,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        now=now,
        job_id=spec.job_id,
        source_ref={
            'provider': spec.source.provider,
            'id': spec.source.id,
            'revision': spec.source.revision,
            'sha256': spec.source.sha256,
        },
        style_id=spec.style_id,
        style_overrides=dict(spec.style_overrides),
        review_destination=dict(spec.review_destination),
        action='render_candidate',
        processor=None,
        existing_binding=spec.job_id,
    )
    if not fresh.allowed:
        attempt_count = checkpoint.attempt_counts.get(FAILURE_KEY, 0)
        refs = (
            f"deny:{fresh.reason}",
            f"retry:{FAILURE_KEY}:attempts={attempt_count}",
        )
        if fresh.reason == 'AUTH_REVOKED':
            new_checkpoint, _ = _advance(
                checkpoint,
                target_state='CANCELLED',
                event_id=f"evt_revoked_{spec.job_id}",
                evidence_refs=refs,
                actor='orchestrator',
                decision='REVOKED',
                now=now_iso,
                reason='Job grant was revoked by issuer before QC',
                job_dir=job_dir,
            )
            return _QCOutcome(
                result=_deny_result(
                    final_state='CANCELLED',
                    deny_code='AUTH_REVOKED',
                    reason='Job grant was revoked by issuer before QC',
                    spec=spec,
                    checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
                    evidence_ids=evidence_ids,
                    renderer_invoked=True,
                    render_execution=render_execution,
                ),
                retry_decision=None,
                qc_result=None,
                evidence_ids=evidence_ids,
            )
        new_checkpoint, _ = _advance(
            checkpoint,
            target_state='BLOCKED_AUTHORIZATION',
            event_id=f"evt_block_reverify_{spec.job_id}",
            evidence_refs=refs,
            actor='orchestrator',
            decision='BLOCKED',
            now=now_iso,
            reason=f"Re-verification before QC failed: {fresh.reason}",
            job_dir=job_dir,
        )
        return _QCOutcome(
            result=_deny_result(
                final_state='BLOCKED_AUTHORIZATION',
                deny_code=fresh.reason,
                reason=f"Re-verification before QC failed: {fresh.reason}",
                spec=spec,
                checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
                evidence_ids=evidence_ids,
                renderer_invoked=True,
                render_execution=render_execution,
            ),
            retry_decision=None,
            qc_result=None,
            evidence_ids=evidence_ids,
        )

    qc = run_qc(
        spec,
        render_execution,
        now=now_iso,
        job_dir=job_dir,
        authorization_decision=fresh,
        script_sha256=script_sha256,
    )
    ids = evidence_ids
    if (
        qc.evidence_record is not None
        and qc.evidence_id
        and qc.evidence_id not in ids
    ):
        ids = ids + (qc.evidence_id,)

    attempt_count = checkpoint.attempt_counts.get(FAILURE_KEY, 0)
    retry_ref = f"retry:{FAILURE_KEY}:attempts={attempt_count}"
    qc_tag = f"qc_{spec.job_id}_a{attempt_count:03d}"
    qc_evidence_ref = (
        f"qc:{qc.evidence_id}" if qc.evidence_id else "qc:no_evidence"
    )

    if qc.target_state == 'FAILED_JOB':
        # C9: FAILED_JOB is terminal. Do NOT transition yet — decide
        # first. If retry is allowed, the caller re-enters the render
        # loop from RENDERED; the attempt was already counted in
        # _record_attempt (plan section 6.2 row 11: "นับ attempt นี้แล้ว").
        decision = decide_retry(
            checkpoint=checkpoint,
            failure_key=FAILURE_KEY,
            deny_code=RENDER_EXECUTION_FAILED,
            retry_allowance=spec.retry_allowance,
        )
        if decision.should_retry:
            return _QCOutcome(
                result=None,
                retry_decision=decision,
                qc_result=qc,
                evidence_ids=ids,
            )
        new_checkpoint, trans = _advance(
            checkpoint,
            target_state='QC_EVALUATED',
            event_id=f"evt_{qc_tag}",
            evidence_refs=(qc_evidence_ref, retry_ref),
            actor='orchestrator',
            decision='QC_EVALUATED',
            now=now_iso,
            reason=qc.reason,
            job_dir=job_dir,
        )
        if new_checkpoint is None:
            return _QCOutcome(
                result=_deny_result(
                    final_state=checkpoint.current_state,
                    deny_code=trans.reason,
                    reason=f"Transition to QC_EVALUATED rejected: {trans.reason}",
                    spec=spec,
                    checkpoint=checkpoint,
                    evidence_ids=ids,
                    renderer_invoked=True,
                    render_execution=render_execution,
                ),
                retry_decision=None,
                qc_result=qc,
                evidence_ids=ids,
            )
        refs = [retry_ref]
        if decision.exhausted:
            refs.append(f"retry:{FAILURE_KEY}:exhausted")
        new_checkpoint2, trans2 = _advance(
            new_checkpoint,
            target_state='FAILED_JOB',
            event_id=f"evt_{qc_tag}_failed",
            evidence_refs=tuple(refs),
            actor='orchestrator',
            decision='FAILED',
            now=now_iso,
            reason=qc.reason,
            job_dir=job_dir,
        )
        if new_checkpoint2 is None:
            return _QCOutcome(
                result=_deny_result(
                    final_state=new_checkpoint.current_state,
                    deny_code=trans2.reason,
                    reason=f"Transition to FAILED_JOB rejected: {trans2.reason}",
                    spec=spec,
                    checkpoint=new_checkpoint,
                    evidence_ids=ids,
                    renderer_invoked=True,
                    render_execution=render_execution,
                ),
                retry_decision=None,
                qc_result=qc,
                evidence_ids=ids,
            )
        return _QCOutcome(
            result=_deny_result(
                final_state='FAILED_JOB',
                deny_code=RENDER_EXECUTION_FAILED,
                reason=qc.reason,
                spec=spec,
                checkpoint=new_checkpoint2,
                evidence_ids=ids,
                renderer_invoked=True,
                render_execution=render_execution,
            ),
            retry_decision=None,
            qc_result=qc,
            evidence_ids=ids,
        )

    if qc.target_state == 'READY_FOR_REVIEW':
        new_checkpoint, trans = _advance(
            checkpoint,
            target_state='QC_EVALUATED',
            event_id=f"evt_{qc_tag}",
            evidence_refs=(qc_evidence_ref, retry_ref),
            actor='orchestrator',
            decision='QC_EVALUATED',
            now=now_iso,
            reason=qc.reason,
            job_dir=job_dir,
        )
        if new_checkpoint is None:
            return _QCOutcome(
                result=_deny_result(
                    final_state=checkpoint.current_state,
                    deny_code=trans.reason,
                    reason=f"Transition to QC_EVALUATED rejected: {trans.reason}",
                    spec=spec,
                    checkpoint=checkpoint,
                    evidence_ids=ids,
                    renderer_invoked=True,
                    render_execution=render_execution,
                ),
                retry_decision=None,
                qc_result=qc,
                evidence_ids=ids,
            )
        new_checkpoint2, trans2 = _advance(
            new_checkpoint,
            target_state='READY_FOR_REVIEW',
            event_id=f"evt_{qc_tag}_ready",
            evidence_refs=(qc_evidence_ref, retry_ref),
            actor='orchestrator',
            decision='READY_FOR_REVIEW',
            now=now_iso,
            reason=qc.reason,
            job_dir=job_dir,
        )
        if new_checkpoint2 is None:
            return _QCOutcome(
                result=_deny_result(
                    final_state=new_checkpoint.current_state,
                    deny_code=trans2.reason,
                    reason=f"Transition to READY_FOR_REVIEW rejected: {trans2.reason}",
                    spec=spec,
                    checkpoint=new_checkpoint,
                    evidence_ids=ids,
                    renderer_invoked=True,
                    render_execution=render_execution,
                ),
                retry_decision=None,
                qc_result=qc,
                evidence_ids=ids,
            )
        return _QCOutcome(
            result=OrchestratorResult(
                success=True,
                final_state='READY_FOR_REVIEW',
                deny_code=None,
                reason=qc.reason,
                spec=spec,
                checkpoint=new_checkpoint2,
                evidence_ids=ids,
                renderer_invoked=True,
                render_execution=render_execution,
            ),
            retry_decision=None,
            qc_result=qc,
            evidence_ids=ids,
        )

    # BLOCKED_NEEDS_REVIEW: a content fault. Never retried.
    new_checkpoint, trans = _advance(
        checkpoint,
        target_state='QC_EVALUATED',
        event_id=f"evt_{qc_tag}",
        evidence_refs=(qc_evidence_ref, retry_ref),
        actor='orchestrator',
        decision='QC_EVALUATED',
        now=now_iso,
        reason=qc.reason,
        job_dir=job_dir,
    )
    if new_checkpoint is None:
        return _QCOutcome(
            result=_deny_result(
                final_state=checkpoint.current_state,
                deny_code=trans.reason,
                reason=f"Transition to QC_EVALUATED rejected: {trans.reason}",
                spec=spec,
                checkpoint=checkpoint,
                evidence_ids=ids,
                renderer_invoked=True,
                render_execution=render_execution,
            ),
            retry_decision=None,
            qc_result=qc,
            evidence_ids=ids,
        )
    new_checkpoint2, trans2 = _advance(
        new_checkpoint,
        target_state='BLOCKED_NEEDS_REVIEW',
        event_id=f"evt_{qc_tag}_blocked",
        evidence_refs=(qc_evidence_ref, retry_ref),
        actor='orchestrator',
        decision='BLOCKED',
        now=now_iso,
        reason=qc.reason,
        job_dir=job_dir,
    )
    if new_checkpoint2 is None:
        return _QCOutcome(
            result=_deny_result(
                final_state=new_checkpoint.current_state,
                deny_code=trans2.reason,
                reason=f"Transition to BLOCKED_NEEDS_REVIEW rejected: {trans2.reason}",
                spec=spec,
                checkpoint=new_checkpoint,
                evidence_ids=ids,
                renderer_invoked=True,
                render_execution=render_execution,
            ),
            retry_decision=None,
            qc_result=qc,
            evidence_ids=ids,
        )
    deny_code = (
        qc.unsatisfied_checks[0] if qc.unsatisfied_checks else 'QC_BLOCKED'
    )
    return _QCOutcome(
        result=_deny_result(
            final_state='BLOCKED_NEEDS_REVIEW',
            deny_code=deny_code,
            reason=qc.reason,
            spec=spec,
            checkpoint=new_checkpoint2,
            evidence_ids=ids,
            renderer_invoked=True,
            render_execution=render_execution,
        ),
        retry_decision=None,
        qc_result=qc,
        evidence_ids=ids,
    )


def orchestrate_render_job(
    grant_bytes: bytes,
    status_bytes: bytes,
    *,
    trusted_grant_keys: dict,
    trusted_status_keys: dict,
    now,
    job_id: str,
    grant_id: str,
    source_ref: dict,
    style_id: str,
    style_overrides: dict,
    review_destination: dict,
    allowed_actions,
    allowed_processors,
    retry_allowance: int,
    source_path,
    trusted_source_root,
    base_staging_dir,
    transcript_data: dict,
    script_text: str,
    script_lines: list,
    transcript_tool_metadata: dict,
    mfa_artifact_bytes: bytes,
    mfa_tool_metadata: dict,
    cues: list,
    media_duration_seconds: float,
    fonts_dir,
    expected_audio_sha256: str,
    auto_mode: bool = False,
    existing_binding: str | None = None,
    adapter: PlatformAdapter | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    script_sha256: str | None = None,
) -> OrchestratorResult:
    if type(grant_bytes) is not bytes or type(status_bytes) is not bytes:
        raise OrchestratorError('grant_bytes and status_bytes must be bytes')
    if type(job_id) is not str or not job_id:
        raise OrchestratorError('job_id must be a non-empty str')
    if type(grant_id) is not str or not grant_id:
        raise OrchestratorError('grant_id must be a non-empty str')
    if type(auto_mode) is not bool:
        raise OrchestratorError('auto_mode must be a bool')
    if adapter is not None and not isinstance(adapter, PlatformAdapter):
        raise OrchestratorError('adapter must satisfy the PlatformAdapter contract')
    if type(expected_audio_sha256) is not str or _DIGEST.fullmatch(expected_audio_sha256) is None:
        raise OrchestratorError('expected_audio_sha256 must be a 64-char lowercase hex sha256')
    if (
        type(media_duration_seconds) is bool
        or type(media_duration_seconds) not in (int, float)
        or not math.isfinite(media_duration_seconds)
        or media_duration_seconds < 0
    ):
        raise OrchestratorError('media_duration_seconds must be a non-negative finite number')
    if type(script_text) is not str:
        raise OrchestratorError('script_text must be a str')

    now_iso = _now_iso(now)
    existing = _resolve_existing_binding(
        base_staging_dir=base_staging_dir,
        grant_id=grant_id,
        caller_binding=existing_binding,
    )

    # P2-009 pre-flight (before intake): if a required binary is
    # missing, the whole run stops as FAILED_SYSTEM; if the review
    # destination is a Drive destination and the simulated Drive
    # credential is missing, it stops as BLOCKED_NEEDS_REVIEW (D6).
    # adapter=None behaves exactly as before (LocalAdapter).
    adapter_obj: PlatformAdapter = (
        adapter
        if adapter is not None
        else LocalAdapter(base_staging_dir=base_staging_dir)
    )
    preflight = _preflight_capabilities(
        adapter=adapter_obj,
        now_iso=now_iso,
        review_destination=review_destination,
    )
    if preflight is not None:
        return preflight

    pre = _step_pre_verify(
        grant_bytes=grant_bytes,
        status_bytes=status_bytes,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        now=now,
        job_id=job_id,
        source_ref=source_ref,
        style_id=style_id,
        style_overrides=style_overrides,
        review_destination=review_destination,
        existing_binding=existing,
    )
    if isinstance(pre, OrchestratorResult):
        return pre

    res2 = _step_intake(
        grant_bytes=grant_bytes,
        status_bytes=status_bytes,
        job_id=job_id,
        grant_id=grant_id,
        source_ref=source_ref,
        style_id=style_id,
        style_overrides=style_overrides,
        review_destination=review_destination,
        allowed_actions=allowed_actions,
        allowed_processors=allowed_processors,
        retry_allowance=retry_allowance,
        source_path=source_path,
        trusted_source_root=trusted_source_root,
        base_staging_dir=base_staging_dir,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        now=now,
        now_iso=now_iso,
        existing_binding=existing,
    )
    if isinstance(res2, OrchestratorResult):
        return res2
    spec, checkpoint = res2

    res3 = _step_to_authorized(spec=spec, checkpoint=checkpoint, now_iso=now_iso)
    if isinstance(res3, OrchestratorResult):
        return res3
    checkpoint = res3

    collected: tuple[str, ...] = ()
    res4 = _step_transcript_g1(
        spec=spec,
        checkpoint=checkpoint,
        transcript_data=transcript_data,
        script_text=script_text,
        script_lines=script_lines,
        transcript_tool_metadata=transcript_tool_metadata,
        now_iso=now_iso,
    )
    if isinstance(res4, OrchestratorResult):
        return res4
    checkpoint, ids4 = res4
    collected = collected + ids4

    res5 = _step_mfa_g2(
        spec=spec,
        checkpoint=checkpoint,
        mfa_artifact_bytes=mfa_artifact_bytes,
        expected_script_sha256=_sha256_text_value(script_text),
        expected_audio_sha256=expected_audio_sha256,
        media_duration_seconds=media_duration_seconds,
        script_text=script_text,
        mfa_tool_metadata=mfa_tool_metadata,
        now_iso=now_iso,
    )
    if isinstance(res5, OrchestratorResult):
        return res5
    checkpoint, g2_record, ids5 = res5
    collected = collected + ids5

    res6 = _step_reverify_g3(
        spec=spec,
        checkpoint=checkpoint,
        grant_bytes=grant_bytes,
        status_bytes=status_bytes,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        now=now,
        script_text=script_text,
        cues=cues,
        mfa_evidence=g2_record,
        now_iso=now_iso,
    )
    if isinstance(res6, OrchestratorResult):
        return res6
    checkpoint, ids6 = res6
    collected = collected + ids6

    # หมายเหตุ: evidence_ids เก็บเฉพาะ id ของ EvidenceRecord ที่บันทึกเป็นไฟล์แล้ว
    # (ev_transcript, ev_g1, ev_g2, ev_g3) ส่วน 'ev_grant_verified' เป็นแค่ evidence ref
    # ใน transition ไม่ใช่ไฟล์ record จึงไม่อยู่ในรายการนี้ และทุกขั้นใช้ now ตัวเดียวกัน (C12)
    # ห้ามอ่านนาฬิกาจริง
    render_outcome = _step_auto_mode_render(
        spec=spec,
        checkpoint=checkpoint,
        script_text=script_text,
        mfa_artifact_bytes=mfa_artifact_bytes,
        fonts_dir=fonts_dir,
        auto_mode=auto_mode,
        evidence_ids=collected,
        now_iso=now_iso,
        now=now,
        grant_bytes=grant_bytes,
        status_bytes=status_bytes,
        trusted_grant_keys=trusted_grant_keys,
        trusted_status_keys=trusted_status_keys,
        sleep_fn=sleep_fn,
    )
    if not render_outcome.success:
        return render_outcome

    # P2-009 section 5.4: fresh QC after every successful render, in the
    # same run. If QC reports a system fault and the retry quota remains,
    # sleep the decided backoff and re-enter the render loop from
    # RENDERED; otherwise the QC outcome is final.
    render_checkpoint = render_outcome.checkpoint
    render_execution = render_outcome.render_execution
    qc_ids = render_outcome.evidence_ids
    while True:
        qc_outcome = _step_qc_and_review(
            spec=spec,
            checkpoint=render_checkpoint,
            render_execution=render_execution,
            script_sha256=script_sha256,
            grant_bytes=grant_bytes,
            status_bytes=status_bytes,
            trusted_grant_keys=trusted_grant_keys,
            trusted_status_keys=trusted_status_keys,
            now=now,
            now_iso=now_iso,
            evidence_ids=qc_ids,
        )
        if qc_outcome.result is not None:
            return qc_outcome.result
        # Carry the faulted QC run's evidence id forward: the retry
        # signal's evidence_ids already include it, and the next render
        # attempt must not drop it from the job's evidence trail.
        qc_ids = qc_outcome.evidence_ids
        sleep_fn(qc_outcome.retry_decision.backoff_seconds)
        render_outcome = _step_auto_mode_render(
            spec=spec,
            checkpoint=render_checkpoint,
            script_text=script_text,
            mfa_artifact_bytes=mfa_artifact_bytes,
            fonts_dir=fonts_dir,
            auto_mode=auto_mode,
            evidence_ids=qc_ids,
            now_iso=now_iso,
            now=now,
            grant_bytes=grant_bytes,
            status_bytes=status_bytes,
            trusted_grant_keys=trusted_grant_keys,
            trusted_status_keys=trusted_status_keys,
            sleep_fn=sleep_fn,
        )
        if not render_outcome.success:
            return render_outcome
        render_checkpoint = render_outcome.checkpoint
        render_execution = render_outcome.render_execution
        qc_ids = render_outcome.evidence_ids
