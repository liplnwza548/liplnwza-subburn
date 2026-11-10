"""P2-004 audit persistence: atomic JSON files and the replay ledger.

Files: job_spec.json, checkpoint.json and events.jsonl live inside the job
folder jobs/<job_id>/; replay_ledger.json lives at the staging root. All writes
are atomic (temp file in the same directory, flush, fsync, os.replace, fsync
the parent directory). This log is verifiable, never tamper-proof: a writer
with write access to the folder can rewrite it, so it must not be called
tamper-proof. Single writer per job is assumed; no file locking in this ticket.
"""

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .authorization import canonical
from .job import JobSpec, JobSpecError, deserialize_job_spec, serialize_job_spec
from .state import (
    CheckpointError,
    JobCheckpoint,
    JobEvent,
    deserialize_checkpoint,
    deserialize_event,
    serialize_checkpoint,
    serialize_event,
)

JOB_SPEC_FILENAME = 'job_spec.json'
CHECKPOINT_FILENAME = 'checkpoint.json'
EVENT_LOG_FILENAME = 'events.jsonl'
LEDGER_FILENAME = 'replay_ledger.json'

_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


class AuditError(ValueError):
    """Audit persistence is inconsistent or corrupt; fail closed."""


@dataclass(frozen=True)
class ReplayRecord:
    grant_id: str
    job_id: str
    bound_at: str


def _validate_timestamp(value) -> bool:
    if type(value) is not str or not _TIMESTAMP.match(value):
        return False
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return True
    except ValueError:
        return False


def _strict_json_loads(data: bytes) -> object:
    if not isinstance(data, bytes):
        raise AuditError('AUDIT_CORRUPT')

    def _pairs_hook(pairs):
        res = {}
        for k, v in pairs:
            if k in res:
                raise AuditError('AUDIT_CORRUPT')
            res[k] = v
        return res

    def _reject(_):
        raise AuditError('AUDIT_CORRUPT')

    try:
        return json.loads(
            data.decode('utf-8'),
            object_pairs_hook=_pairs_hook,
            parse_float=_reject,
            parse_constant=_reject,
        )
    except (CheckpointError, JobSpecError, AuditError):
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AuditError('AUDIT_CORRUPT') from exc


def atomic_write_file(target_path, data: bytes) -> None:
    if not isinstance(target_path, (str, os.PathLike)) or not str(target_path):
        raise ValueError('INVALID_TARGET_PATH')
    if not isinstance(data, bytes):
        raise ValueError('INVALID_DATA')

    target = Path(target_path)
    temp_path = target.parent / f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        with open(temp_path, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    except BaseException:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        raise

    try:
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Ignore OSError from directory fsync (some filesystems do not support it).
        pass


def atomic_write_json(target_path, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError('INVALID_PAYLOAD')
    atomic_write_file(target_path, canonical(payload))


def append_event(event_log_path, event) -> None:
    """Append a serialized JobEvent line to the event log.

    The caller is responsible for creating the job folder first; parent
    directories are not created automatically.
    """
    if not isinstance(event, JobEvent):
        raise ValueError('INVALID_EVENT')
    if not isinstance(event_log_path, (str, os.PathLike)) or not str(event_log_path):
        raise ValueError('INVALID_PATH')
    with open(event_log_path, 'ab') as f:
        f.write(serialize_event(event) + b'\n')
        f.flush()
        os.fsync(f.fileno())


def load_event_log(event_log_path, *, expected_job_id=None) -> list[JobEvent]:
    path = Path(event_log_path)
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AuditError('AUDIT_CORRUPT') from exc
    if not raw:
        return []
    try:
        text = raw.decode('utf-8')
    except UnicodeError as exc:
        raise AuditError('AUDIT_CORRUPT') from exc
    lines = text.splitlines()
    if not lines:
        return []
    events: list[JobEvent] = []
    seen_event_ids: set[str] = set()
    for line in lines:
        if not line or line.isspace():
            raise AuditError('AUDIT_CORRUPT')
        try:
            event = deserialize_event(line.encode('utf-8'), expected_job_id=expected_job_id)
        except CheckpointError as exc:
            raise AuditError('AUDIT_CORRUPT') from exc
        events.append(event)

    if events:
        if events[0].state_revision != 1:
            raise AuditError('AUDIT_CORRUPT')
        seen_event_ids.add(events[0].event_id)
        for i in range(1, len(events)):
            prev = events[i - 1]
            curr = events[i]
            if curr.state_revision != prev.state_revision + 1:
                raise AuditError('AUDIT_CORRUPT')
            if curr.prior_state != prev.next_state:
                raise AuditError('AUDIT_CORRUPT')
            if curr.event_id in seen_event_ids:
                raise AuditError('AUDIT_CORRUPT')
            seen_event_ids.add(curr.event_id)
    return events


def save_checkpoint(checkpoint_path, checkpoint) -> None:
    if not isinstance(checkpoint, JobCheckpoint):
        raise ValueError('INVALID_CHECKPOINT')
    atomic_write_file(checkpoint_path, serialize_checkpoint(checkpoint))


def load_checkpoint(checkpoint_path, *, expected_job_id=None) -> JobCheckpoint | None:
    path = Path(checkpoint_path)
    if not path.exists():
        return None
    return deserialize_checkpoint(path.read_bytes(), expected_job_id=expected_job_id)


def save_job_spec(job_spec_path, spec) -> None:
    if not isinstance(spec, JobSpec):
        raise ValueError('INVALID_SPEC')
    atomic_write_file(job_spec_path, serialize_job_spec(spec))


def load_job_spec(job_spec_path, *, expected_job_id=None) -> JobSpec | None:
    path = Path(job_spec_path)
    if not path.exists():
        return None
    spec = deserialize_job_spec(path.read_bytes())
    if expected_job_id is not None and spec.job_id != expected_job_id:
        raise JobSpecError('AUTH_SCHEMA_INVALID')
    return spec


class ReplayLedger:
    def __init__(self, ledger_file: Path):
        self.ledger_file = Path(ledger_file)

    def _read(self) -> dict:
        if not self.ledger_file.exists():
            return {'schema_version': '1', 'bindings': {}}
        try:
            raw = self.ledger_file.read_bytes()
        except OSError as exc:
            raise AuditError('AUDIT_CORRUPT') from exc
        payload = _strict_json_loads(raw)
        if type(payload) is not dict:
            raise AuditError('AUDIT_CORRUPT')
        if payload.keys() != {'schema_version', 'bindings'}:
            raise AuditError('AUDIT_CORRUPT')
        if payload['schema_version'] != '1':
            raise AuditError('AUDIT_CORRUPT')
        bindings = payload['bindings']
        if type(bindings) is not dict:
            raise AuditError('AUDIT_CORRUPT')
        for k, v in bindings.items():
            if type(k) is not str or not k:
                raise AuditError('AUDIT_CORRUPT')
            if type(v) is not dict:
                raise AuditError('AUDIT_CORRUPT')
            if v.keys() != {'job_id', 'bound_at'}:
                raise AuditError('AUDIT_CORRUPT')
            if type(v['job_id']) is not str or not v['job_id']:
                raise AuditError('AUDIT_CORRUPT')
            if not _validate_timestamp(v['bound_at']):
                raise AuditError('AUDIT_CORRUPT')
        return payload

    def _write(self, payload: dict) -> None:
        atomic_write_json(self.ledger_file, payload)

    def get_binding(self, grant_id: str) -> str | None:
        if type(grant_id) is not str or not grant_id:
            raise ValueError('INVALID_GRANT_ID')
        payload = self._read()
        entry = payload['bindings'].get(grant_id)
        if entry is None:
            return None
        return entry['job_id']

    def bind_grant(self, grant_id: str, job_id: str, bound_at: str) -> bool:
        if type(grant_id) is not str or not grant_id:
            raise ValueError('INVALID_GRANT_ID')
        if type(job_id) is not str or not job_id:
            raise ValueError('INVALID_JOB_ID')
        if not _validate_timestamp(bound_at):
            raise ValueError('INVALID_TIMESTAMP')
        payload = self._read()
        existing = payload['bindings'].get(grant_id)
        if existing is not None:
            if existing['job_id'] == job_id:
                return True
            return False
        payload['bindings'][grant_id] = {'job_id': job_id, 'bound_at': bound_at}
        self._write(payload)
        return True


def load_job_record(job_dir, *, job_id: str) -> tuple[JobSpec, JobCheckpoint, list[JobEvent]] | None:
    if type(job_id) is not str or not job_id:
        raise ValueError('INVALID_JOB_ID')
    base_dir = Path(job_dir)
    spec = load_job_spec(base_dir / JOB_SPEC_FILENAME, expected_job_id=job_id)
    if spec is None:
        return None
    checkpoint = load_checkpoint(base_dir / CHECKPOINT_FILENAME, expected_job_id=job_id)
    if checkpoint is None:
        raise AuditError('AUDIT_CORRUPT')
    events = load_event_log(base_dir / EVENT_LOG_FILENAME, expected_job_id=job_id)
    if checkpoint.grant_id != spec.grant_id:
        raise AuditError('AUDIT_CORRUPT')
    if not events:
        raise AuditError('AUDIT_CORRUPT')
    if events[-1].state_revision != checkpoint.state_revision:
        raise AuditError('AUDIT_CORRUPT')
    if events[-1].event_id != checkpoint.last_event_id:
        raise AuditError('AUDIT_CORRUPT')
    return (spec, checkpoint, events)
