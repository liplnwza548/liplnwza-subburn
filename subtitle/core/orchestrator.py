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

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import math
from pathlib import Path
import re

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
from .renderer_adapter import RenderExecutionResult, render_one_clip
from .state import JobCheckpoint, TransitionDecision, reduce_transition


class OrchestratorError(Exception):
    """ข้อผิดพลาดระดับโครงสร้างการประสานงานของ Orchestrator"""


AUTO_MODE_DISABLED = 'AUTO_MODE_DISABLED'
RENDER_EXECUTION_FAILED = 'RENDER_EXECUTION_FAILED'
_DIGEST = re.compile(r'^[0-9a-f]{64}$')


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
) -> OrchestratorResult:
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
    result = render_one_clip(
        spec,
        script_text=script_text,
        mfa_artifact_bytes=mfa_artifact_bytes,
        fonts_dir=fonts_dir,
        timeout_seconds=600,
    )
    if not result.success:
        new_checkpoint, _ = _advance(
            checkpoint,
            target_state='FAILED_JOB',
            event_id=f"evt_fail_render_{spec.job_id}",
            evidence_refs=(f"exit_code:{result.exit_code}",),
            actor='orchestrator',
            decision='FAILED',
            now=now_iso,
            reason=(result.error_message or 'Renderer execution failed'),
            job_dir=job_dir,
        )
        return _deny_result(
            final_state='FAILED_JOB',
            deny_code=RENDER_EXECUTION_FAILED,
            reason=(result.error_message or 'Renderer execution failed'),
            spec=spec,
            checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
            evidence_ids=evidence_ids,
            renderer_invoked=True,
            render_execution=result,
        )

    out = Path(result.output_path)
    if (
        not out.is_file()
        or out.stat().st_size == 0
        or out.stat().st_size != result.output_size_bytes
        or result.output_sha256 is None
    ):
        fail_reason = 'Candidate output missing or inconsistent after successful render'
        new_checkpoint, _ = _advance(
            checkpoint,
            target_state='FAILED_JOB',
            event_id=f"evt_fail_render_{spec.job_id}",
            evidence_refs=(f"exit_code:{result.exit_code}",),
            actor='orchestrator',
            decision='FAILED',
            now=now_iso,
            reason=fail_reason,
            job_dir=job_dir,
        )
        return _deny_result(
            final_state='FAILED_JOB',
            deny_code=RENDER_EXECUTION_FAILED,
            reason=fail_reason,
            spec=spec,
            checkpoint=new_checkpoint if new_checkpoint is not None else checkpoint,
            evidence_ids=evidence_ids,
            renderer_invoked=True,
            render_execution=result,
        )

    new_checkpoint, trans = _advance(
        checkpoint,
        target_state='RENDERED',
        event_id=f"evt_render_{spec.job_id}",
        evidence_refs=(f"render:{result.output_sha256}",),
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

    return OrchestratorResult(
        success=True,
        final_state='RENDERED',
        deny_code=None,
        reason='One-clip render completed successfully',
        spec=spec,
        checkpoint=new_checkpoint,
        evidence_ids=evidence_ids,
        renderer_invoked=True,
        render_execution=result,
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
) -> OrchestratorResult:
    if type(grant_bytes) is not bytes or type(status_bytes) is not bytes:
        raise OrchestratorError('grant_bytes and status_bytes must be bytes')
    if type(job_id) is not str or not job_id:
        raise OrchestratorError('job_id must be a non-empty str')
    if type(grant_id) is not str or not grant_id:
        raise OrchestratorError('grant_id must be a non-empty str')
    if type(auto_mode) is not bool:
        raise OrchestratorError('auto_mode must be a bool')
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
    return _step_auto_mode_render(
        spec=spec,
        checkpoint=checkpoint,
        script_text=script_text,
        mfa_artifact_bytes=mfa_artifact_bytes,
        fonts_dir=fonts_dir,
        auto_mode=auto_mode,
        evidence_ids=collected,
        now_iso=now_iso,
    )
