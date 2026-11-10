"""P2-003 fail-closed, one-clip intake; no Production Auto Mode.

The trusted host supplies every expected binding as a separate parameter, never
by extracting expectations from grant_bytes. Only intake_copy with no processor
is authorized here. No renderer, transcription, alignment or delivery runs.
SOURCE_NOT_FOUND means no regular source file exists under the trusted root.
"""

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from .authorization import verify
from .job import (JobSpec, SourceSnapshot, StagingPaths, create_job_spec,
                  validate_style_spec)

SOURCE_DIGEST_MISMATCH = 'SOURCE_DIGEST_MISMATCH'
UNSAFE_SOURCE_SYMLINK = 'UNSAFE_SOURCE_SYMLINK'
UNSAFE_PATH_TRAVERSAL = 'UNSAFE_PATH_TRAVERSAL'
UNSAFE_OUTPUT_ALIAS = 'UNSAFE_OUTPUT_ALIAS'
SOURCE_MUTATED_ERROR = 'SOURCE_MUTATED_ERROR'
SOURCE_NOT_FOUND = 'SOURCE_NOT_FOUND'

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_JOB_ID = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
_SUFFIX = re.compile(r'^\.[A-Za-z0-9]{1,10}$')


class _Deny(Exception):
    """Expected intake denial, with its reason in args[0]."""


@dataclass(frozen=True)
class IntakeResult:
    success: bool
    spec: JobSpec | None
    deny_reason: str | None


def compute_file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with Path(file_path).open('rb') as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_symlink_components(path: Path, reason: str) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    prefix = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        prefix = prefix / component
        if os.path.islink(prefix):
            raise _Deny(reason)


def safe_resolve_path(target_path: Path, trusted_root: Path) -> Path:
    target_path = Path(target_path)
    _reject_symlink_components(target_path, UNSAFE_SOURCE_SYMLINK)
    resolved = target_path.resolve()
    if not resolved.is_relative_to(Path(trusted_root).resolve()):
        raise _Deny(UNSAFE_PATH_TRAVERSAL)
    if not resolved.exists() or not resolved.is_file():
        raise _Deny(SOURCE_NOT_FOUND)
    return resolved


def assert_paths_do_not_alias(source_path: Path, target_path: Path) -> None:
    source_path, target_path = Path(source_path), Path(target_path)
    if (target_path.resolve() == source_path.resolve()
            or (source_path.exists() and target_path.exists()
                and os.path.samefile(source_path, target_path))):
        raise _Deny(UNSAFE_OUTPUT_ALIAS)


def allocate_staging_paths(base_staging_dir: Path, job_id: str,
                           source_name: str) -> StagingPaths:
    if (type(job_id) is not str or not _JOB_ID.fullmatch(job_id)
            or job_id in ('.', '..')):
        raise _Deny('AUTH_SCHEMA_INVALID')
    base = Path(base_staging_dir).resolve()
    jobs_dir = base / 'jobs'
    job_dir = jobs_dir / job_id
    _reject_symlink_components(job_dir, UNSAFE_OUTPUT_ALIAS)
    suffix = Path(source_name).suffix
    if not _SUFFIX.fullmatch(suffix):
        suffix = ''
    jobs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    jobs_dir.chmod(0o700)
    # Exclusive allocation preserves an existing job on retry or collision.
    job_dir.mkdir(mode=0o700)
    ready = False
    try:
        job_dir.chmod(0o700)
        work, output = job_dir / 'work', job_dir / 'output'
        for directory in (work, output):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        paths = StagingPaths(str(work), str(work / ('source' + suffix)),
                             str(output), str(output / 'candidate.mp4'))
        ready = True
        return paths
    finally:
        if not ready:
            shutil.rmtree(job_dir, ignore_errors=True)


def copy_source_to_staging(source_path: Path, dest_path: Path,
                           expected_sha256: str) -> tuple[str, int]:
    dest_path = Path(dest_path)
    digest, byte_count = hashlib.sha256(), 0
    with Path(source_path).open('rb') as source, dest_path.open('xb') as target:
        while chunk := source.read(65536):
            target.write(chunk)
            digest.update(chunk)
            byte_count += len(chunk)
    dest_path.chmod(0o400)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        try:
            dest_path.unlink()
        except OSError:
            pass
        raise _Deny(SOURCE_MUTATED_ERROR)
    return actual_sha256, byte_count


def _grant_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Deny('AUTH_SCHEMA_INVALID')
        result[key] = value
    return result


def _reject_grant_number(value):
    raise _Deny('AUTH_SCHEMA_INVALID')


def _parse_grant_json(data):
    # P2-002 maps syntax errors to AUTH_INVALID_OR_UNTRUSTED. Intake needs the
    # schema reason instead. These values never supply host expectations.
    if not isinstance(data, bytes) or len(data) > 65536:
        raise _Deny('AUTH_SCHEMA_INVALID')
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_grant_pairs,
                          parse_float=_reject_grant_number,
                          parse_constant=_reject_grant_number)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _Deny('AUTH_SCHEMA_INVALID') from exc


def process_job_intake(grant_bytes: bytes, status_bytes: bytes, *, job_id: str,
                       grant_id: str, source_ref: dict, style_id: str,
                       style_overrides: dict, review_destination: dict,
                       allowed_actions, allowed_processors, retry_allowance: int,
                       source_path, trusted_source_root, base_staging_dir,
                       trusted_grant_keys, trusted_status_keys, now,
                       existing_binding=None) -> IntakeResult:
    job_dir = None
    succeeded = False
    try:
        try:
            validate_style_spec(style_id, style_overrides)
        except ValueError as exc:
            raise _Deny('STYLE_NOT_AUTHORIZED') from exc
        if (type(source_ref) is not dict
                or source_ref.keys() != {'provider', 'id', 'revision', 'sha256'}
                or type(source_ref['sha256']) is not str
                or not _DIGEST.fullmatch(source_ref['sha256'])):
            raise _Deny('AUTH_SCHEMA_INVALID')
        if (type(review_destination) is not dict
                or review_destination.keys() != {'provider', 'id', 'owner_id'}):
            raise _Deny('AUTH_SCHEMA_INVALID')
        if type(retry_allowance) is not int or not 0 <= retry_allowance <= 2:
            raise _Deny('AUTH_SCHEMA_INVALID')
        grant = _parse_grant_json(grant_bytes)
        decision = verify(
            grant_bytes, status_bytes, trusted_grant_keys=trusted_grant_keys,
            trusted_status_keys=trusted_status_keys, now=now, job_id=job_id,
            source_ref=source_ref, style_id=style_id, style_overrides=style_overrides,
            review_destination=review_destination, action='intake_copy',
            processor=None, existing_binding=existing_binding,
        )
        if not decision.allowed:
            return IntakeResult(False, None, decision.reason)
        # Bind the remaining host expectations that verify() does not accept.
        # Compare only after authentication, without deriving expectations here.
        if grant['grant_id'] != grant_id:
            raise _Deny('AUTH_INVALID_OR_UNTRUSTED')
        if (set(grant['allowed_actions']) != set(allowed_actions)
                or set(grant['allowed_processors']) != set(allowed_processors)):
            raise _Deny('ACTION_NOT_AUTHORIZED')
        if grant['retry_allowance'] != retry_allowance:
            raise _Deny('AUTH_SCHEMA_INVALID')

        resolved_source = safe_resolve_path(source_path, trusted_source_root)
        source_root = Path(trusted_source_root).resolve()
        staging_base = Path(base_staging_dir).resolve()
        if (staging_base.is_relative_to(source_root)
                or source_root.is_relative_to(staging_base)):
            raise _Deny(UNSAFE_OUTPUT_ALIAS)
        pre_digest = compute_file_sha256(resolved_source)
        size = resolved_source.stat().st_size
        if pre_digest != source_ref['sha256']:
            raise _Deny(SOURCE_DIGEST_MISMATCH)
        staging = allocate_staging_paths(staging_base, job_id, resolved_source.name)
        job_dir = Path(staging.job_work_dir).parent
        assert_paths_do_not_alias(resolved_source, Path(staging.staged_input_path))
        assert_paths_do_not_alias(resolved_source, Path(staging.candidate_output_path))
        copy_source_to_staging(resolved_source, Path(staging.staged_input_path),
                               source_ref['sha256'])
        if compute_file_sha256(resolved_source) != pre_digest:
            raise _Deny(SOURCE_MUTATED_ERROR)
        source = SourceSnapshot(
            provider=source_ref['provider'], id=source_ref['id'],
            revision=source_ref['revision'], sha256=pre_digest,
            size_bytes=size, canonical_source_path=str(resolved_source),
        )
        try:
            spec = create_job_spec(
                job_id=job_id, grant_id=grant_id, source=source, style_id=style_id,
                style_overrides=style_overrides, allowed_actions=allowed_actions,
                allowed_processors=allowed_processors,
                review_destination=review_destination, retry_allowance=retry_allowance,
                staging=staging,
                created_at=now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            )
        except ValueError as exc:
            raise _Deny(str(exc)) from exc
        succeeded = True
        return IntakeResult(True, spec, None)
    except _Deny as exc:
        return IntakeResult(False, None, exc.args[0])
    finally:
        if job_dir is not None and not succeeded:
            shutil.rmtree(job_dir, ignore_errors=True)
