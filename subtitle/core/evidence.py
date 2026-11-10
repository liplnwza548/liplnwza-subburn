"""P2-005 evidence records; no Production Auto Mode.

Every record must carry a human_audio_verification check with result NOT_CHECKED; automated checks are never human listening. This record is verifiable, never tamper-proof.
"""

from dataclasses import dataclass, fields
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType

from .audit import atomic_write_json
from .authorization import canonical

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
_EVIDENCE_ID = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
CHECK_RESULTS = frozenset({'PASSED', 'FAILED', 'NOT_CHECKED', 'NOT_APPLICABLE'})
METHODS = frozenset({'subprocess_execution', 'deterministic_check', 'synthetic_stub'})
STAGES = frozenset({'TRANSCRIBED', 'SCRIPT_EVIDENCE_CHECKED', 'ALIGNED', 'RENDER_PERMITTED', 'QC_EVALUATED'})
EVIDENCE_DIRNAME = 'evidence'


@dataclass(frozen=True)
class CheckRecord:
    name: str
    result: str
    observed_value: str | int | float | bool | None
    threshold: str | int | float | bool | None
    evidence_ref: str | None
    notes: str


@dataclass(frozen=True)
class EvidenceRecord:
    schema_version: str
    evidence_id: str
    job_id: str
    stage: str
    tool: str
    tool_version: str
    method: str
    command: tuple[str, ...]
    input_digest_refs: MappingProxyType
    artifact_sha256: str
    artifact_relpath: str
    captured_at: str
    exit_code: int | None
    checks: tuple[CheckRecord, ...]
    media_duration_seconds: float | None
    notes: str


class EvidenceError(ValueError):
    """Serialized evidence violates the frozen version 1 contract."""


def _validate_check_value(val) -> None:
    if val is None:
        return
    if type(val) not in (str, int, float, bool):
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    if type(val) is float and not math.isfinite(val):
        raise EvidenceError('AUTH_SCHEMA_INVALID')


def create_evidence_record(*, evidence_id, job_id, stage, tool, tool_version,
                           method, command, input_digest_refs, artifact_sha256,
                           artifact_relpath, captured_at, exit_code, checks,
                           media_duration_seconds=None, notes="") -> EvidenceRecord:
    if (type(evidence_id) is not str
            or not _EVIDENCE_ID.fullmatch(evidence_id)
            or evidence_id in ('.', '..')):
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if not all(type(val) is str and bool(val) for val in (job_id, tool, tool_version)):
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if type(stage) is not str or stage not in STAGES:
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if type(method) is not str or method not in METHODS:
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if (type(command) not in (list, tuple)
            or not all(type(arg) is str and bool(arg) for arg in command)):
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if type(input_digest_refs) not in (dict, MappingProxyType):
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    if 'source_media_sha256' not in input_digest_refs or 'script_sha256' not in input_digest_refs:
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    for k, v in input_digest_refs.items():
        if type(k) is not str or not k:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if type(v) is not str or not _DIGEST.fullmatch(v):
            raise EvidenceError('AUTH_SCHEMA_INVALID')

    if type(artifact_sha256) is not str or not _DIGEST.fullmatch(artifact_sha256):
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if type(artifact_relpath) is not str or not artifact_relpath:
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    if artifact_relpath.startswith('/'):
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    rel_path = Path(artifact_relpath)
    if (rel_path.is_absolute()
            or '..' in rel_path.parts
            or any(part == '..' for part in artifact_relpath.split('/'))):
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if type(captured_at) is not str or not _TIMESTAMP.fullmatch(captured_at):
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    try:
        datetime.strptime(captured_at, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError as exc:
        raise EvidenceError('AUTH_SCHEMA_INVALID') from exc

    if exit_code is not None and type(exit_code) is not int:
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if type(checks) not in (list, tuple) or len(checks) == 0:
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    for chk in checks:
        if type(chk) is not CheckRecord:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if type(chk.name) is not str or not chk.name:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if type(chk.result) is not str or chk.result not in CHECK_RESULTS:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        _validate_check_value(chk.observed_value)
        _validate_check_value(chk.threshold)
        if chk.evidence_ref is not None and type(chk.evidence_ref) is not str:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if type(chk.notes) is not str:
            raise EvidenceError('AUTH_SCHEMA_INVALID')

    human_checks = [c for c in checks if c.name == 'human_audio_verification']
    if len(human_checks) != 1 or human_checks[0].result != 'NOT_CHECKED':
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    if media_duration_seconds is not None:
        if (type(media_duration_seconds) is bool
                or type(media_duration_seconds) not in (int, float)):
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if not math.isfinite(media_duration_seconds) or media_duration_seconds < 0:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        stored_duration = float(media_duration_seconds)
    else:
        stored_duration = None

    if type(notes) is not str:
        raise EvidenceError('AUTH_SCHEMA_INVALID')

    return EvidenceRecord(
        schema_version='1',
        evidence_id=evidence_id,
        job_id=job_id,
        stage=stage,
        tool=tool,
        tool_version=tool_version,
        method=method,
        command=tuple(command),
        input_digest_refs=MappingProxyType(dict(input_digest_refs)),
        artifact_sha256=artifact_sha256,
        artifact_relpath=artifact_relpath,
        captured_at=captured_at,
        exit_code=exit_code,
        checks=tuple(checks),
        media_duration_seconds=stored_duration,
        notes=notes,
    )


def serialize_evidence(evidence: EvidenceRecord) -> bytes:
    if type(evidence) is not EvidenceRecord:
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    payload = {
        'schema_version': evidence.schema_version,
        'evidence_id': evidence.evidence_id,
        'job_id': evidence.job_id,
        'stage': evidence.stage,
        'tool': evidence.tool,
        'tool_version': evidence.tool_version,
        'method': evidence.method,
        'command': list(evidence.command),
        'input_digest_refs': dict(evidence.input_digest_refs),
        'artifact_sha256': evidence.artifact_sha256,
        'artifact_relpath': evidence.artifact_relpath,
        'captured_at': evidence.captured_at,
        'exit_code': evidence.exit_code,
        'checks': [
            {
                'name': c.name,
                'result': c.result,
                'observed_value': c.observed_value,
                'threshold': c.threshold,
                'evidence_ref': c.evidence_ref,
                'notes': c.notes,
            }
            for c in evidence.checks
        ],
        'media_duration_seconds': evidence.media_duration_seconds,
        'notes': evidence.notes,
    }
    return canonical(payload)


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        result[key] = value
    return result


def _reject_constant(_):
    raise EvidenceError('AUTH_SCHEMA_INVALID')


def deserialize_evidence(data: bytes, *, expected_job_id=None) -> EvidenceRecord:
    if not isinstance(data, bytes) or len(data) > 65536:
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    try:
        payload = json.loads(
            data.decode('utf-8'),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
        if type(payload) is not dict:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        expected_fields = {field.name for field in fields(EvidenceRecord)}
        if payload.keys() != expected_fields:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if payload['schema_version'] != '1':
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if expected_job_id is not None and payload['job_id'] != expected_job_id:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        if type(payload['command']) is not list:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        command = tuple(payload['command'])
        if type(payload['input_digest_refs']) is not dict:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        input_digest_refs = dict(payload['input_digest_refs'])
        if type(payload['checks']) is not list:
            raise EvidenceError('AUTH_SCHEMA_INVALID')
        check_fields = {field.name for field in fields(CheckRecord)}
        checks = []
        for c in payload['checks']:
            if type(c) is not dict or c.keys() != check_fields:
                raise EvidenceError('AUTH_SCHEMA_INVALID')
            checks.append(CheckRecord(**c))
        checks_tuple = tuple(checks)

        payload.pop('schema_version')
        payload['command'] = command
        payload['input_digest_refs'] = input_digest_refs
        payload['checks'] = checks_tuple
        return create_evidence_record(**payload)
    except EvidenceError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise EvidenceError('AUTH_SCHEMA_INVALID') from exc


def save_evidence_record(job_dir, evidence) -> Path:
    if type(evidence) is not EvidenceRecord:
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    evidence_dir = Path(job_dir) / EVIDENCE_DIRNAME
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target_path = evidence_dir / f'{evidence.evidence_id}.json'
    payload_dict = json.loads(serialize_evidence(evidence).decode('utf-8'))
    atomic_write_json(target_path, payload_dict)
    return target_path


def load_evidence_record(job_dir, evidence_id, *, expected_job_id=None) -> EvidenceRecord | None:
    if (type(evidence_id) is not str
            or not _EVIDENCE_ID.fullmatch(evidence_id)
            or evidence_id in ('.', '..')):
        raise EvidenceError('AUTH_SCHEMA_INVALID')
    path = Path(job_dir) / EVIDENCE_DIRNAME / f'{evidence_id}.json'
    try:
        if not path.exists():
            return None
        data = path.read_bytes()
    except OSError as exc:
        raise EvidenceError('AUTH_SCHEMA_INVALID') from exc
    return deserialize_evidence(data, expected_job_id=expected_job_id)
