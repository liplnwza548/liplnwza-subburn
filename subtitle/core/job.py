"""P2-003 frozen intake contracts; no Production Auto Mode.

JobSpec freezes the intake-time content, style, permissions and staging binding.
Spec section 7's JobManifest is a separate mutable, non-authoritative execution
record: state, attempts, evidence and script/timing/output digests belong there.
Construct JobSpec through create_job_spec; deserialization confers no authority.
"""

import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from types import MappingProxyType

from .authorization import canonical

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_TIMESTAMP = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$')
_DESTINATION_FIELDS = frozenset({'provider', 'id', 'owner_id'})


@dataclass(frozen=True)
class SourceSnapshot:
    provider: str
    id: str
    revision: str
    sha256: str
    size_bytes: int
    canonical_source_path: str


@dataclass(frozen=True)
class StagingPaths:
    job_work_dir: str
    staged_input_path: str
    job_output_dir: str
    candidate_output_path: str


@dataclass(frozen=True)
class JobSpec:
    schema_version: str
    job_id: str
    grant_id: str
    source: SourceSnapshot
    style_id: str
    style_overrides: MappingProxyType
    allowed_actions: frozenset[str]
    allowed_processors: frozenset[str]
    review_destination: MappingProxyType
    retry_allowance: int
    staging: StagingPaths
    created_at: str


class JobSpecError(ValueError):
    """Serialized JobSpec violates the frozen version 1 contract."""


def validate_style_spec(style_id, style_overrides) -> None:
    if (type(style_id) is not str or style_id not in ('viralpop', 'paper-chips')
            or type(style_overrides) is not dict or style_overrides != {}):
        raise ValueError('STYLE_NOT_AUTHORIZED')


def _nonempty_string(value):
    return type(value) is str and bool(value)


def _freeze_identifiers(values):
    if (type(values) not in (list, tuple, set, frozenset)
            or not all(_nonempty_string(value) for value in values)
            or len(set(values)) != len(values)):
        raise ValueError('AUTH_SCHEMA_INVALID')
    return frozenset(values)


def create_job_spec(*, job_id, grant_id, source, style_id, style_overrides,
                    allowed_actions, allowed_processors, review_destination,
                    retry_allowance, staging, created_at) -> JobSpec:
    validate_style_spec(style_id, style_overrides)
    if not all(_nonempty_string(value) for value in (job_id, grant_id)):
        raise ValueError('AUTH_SCHEMA_INVALID')
    if type(retry_allowance) is not int or not 0 <= retry_allowance <= 2:
        raise ValueError('AUTH_SCHEMA_INVALID')
    if (type(source) is not SourceSnapshot
            or not all(_nonempty_string(getattr(source, name))
                       for name in ('provider', 'id', 'revision', 'canonical_source_path'))
            or type(source.sha256) is not str or not _DIGEST.fullmatch(source.sha256)
            or type(source.size_bytes) is not int or source.size_bytes < 0):
        raise ValueError('AUTH_SCHEMA_INVALID')
    if (type(staging) is not StagingPaths
            or not all(_nonempty_string(getattr(staging, field.name))
                       for field in fields(StagingPaths))):
        raise ValueError('AUTH_SCHEMA_INVALID')
    if (type(review_destination) is not dict
            or review_destination.keys() != _DESTINATION_FIELDS
            or not all(_nonempty_string(value) for value in review_destination.values())):
        raise ValueError('AUTH_SCHEMA_INVALID')
    if type(created_at) is not str or not _TIMESTAMP.fullmatch(created_at):
        raise ValueError('AUTH_SCHEMA_INVALID')
    try:
        datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError as exc:
        raise ValueError('AUTH_SCHEMA_INVALID') from exc
    return JobSpec(
        schema_version='1', job_id=job_id, grant_id=grant_id, source=source,
        style_id=style_id, style_overrides=MappingProxyType(dict(style_overrides)),
        allowed_actions=_freeze_identifiers(allowed_actions),
        allowed_processors=_freeze_identifiers(allowed_processors),
        review_destination=MappingProxyType(dict(review_destination)),
        retry_allowance=retry_allowance, staging=staging, created_at=created_at,
    )


def serialize_job_spec(spec: JobSpec) -> bytes:
    payload = {field.name: getattr(spec, field.name) for field in fields(JobSpec)}
    payload.update(
        source=asdict(spec.source), staging=asdict(spec.staging),
        style_overrides=dict(spec.style_overrides),
        allowed_actions=sorted(spec.allowed_actions),
        allowed_processors=sorted(spec.allowed_processors),
        review_destination=dict(spec.review_destination),
    )
    return canonical(payload)


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JobSpecError('AUTH_SCHEMA_INVALID')
        result[key] = value
    return result


def _reject_number(value):
    raise JobSpecError('AUTH_SCHEMA_INVALID')


def deserialize_job_spec(data: bytes) -> JobSpec:
    if not isinstance(data, bytes) or len(data) > 65536:
        raise JobSpecError('AUTH_SCHEMA_INVALID')
    try:
        payload = json.loads(
            data.decode('utf-8'), object_pairs_hook=_strict_pairs,
            parse_float=_reject_number, parse_constant=_reject_number,
        )
        if (type(payload) is not dict
                or payload.keys() != {field.name for field in fields(JobSpec)}
                or payload['schema_version'] != '1'):
            raise JobSpecError('AUTH_SCHEMA_INVALID')
        for name, contract in (('source', SourceSnapshot), ('staging', StagingPaths)):
            if (type(payload[name]) is not dict
                    or payload[name].keys() != {field.name for field in fields(contract)}):
                raise JobSpecError('AUTH_SCHEMA_INVALID')
        if any(type(payload[name]) is not list
               for name in ('allowed_actions', 'allowed_processors')):
            raise JobSpecError('AUTH_SCHEMA_INVALID')
        payload.pop('schema_version')
        payload['source'] = SourceSnapshot(**payload['source'])
        payload['staging'] = StagingPaths(**payload['staging'])
        return create_job_spec(**payload)
    except JobSpecError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise JobSpecError('AUTH_SCHEMA_INVALID') from exc
