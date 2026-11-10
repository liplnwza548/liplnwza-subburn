"""Offline P2-003 tests: synthetic bytes and ephemeral, in-memory signing keys."""

import base64
import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from subtitle.core.authorization import canonical
from subtitle.core import intake
from subtitle.core.intake import process_job_intake, compute_file_sha256
from subtitle.core.job import (
    JobSpecError, SourceSnapshot, StagingPaths, create_job_spec,
    deserialize_job_spec, serialize_job_spec,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
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


class JobIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_root = self.root / 'trusted-source'
        self.base_staging_dir = self.root / 'staging'
        self.source_root.mkdir()
        self.source_path = self.source_root / 'synthetic.mp4'
        self.source_bytes = b'fake-video-bytes-p2-003\x00' * 4096
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
            trusted_status_keys={('status', 's1'): self.status_key.public_key()}, now=NOW,
        )

    def make_grant(self, **overrides):
        grant = dict(
            schema_version='1', grant_id='grant-1', job_id='job-1',
            issuer_id='owner', key_id='g1', issued_at=ts(NOW - timedelta(hours=1)),
            expires_at=ts(NOW + timedelta(hours=1)),
            source_ref=copy.deepcopy(self.source_ref), style_id='viralpop',
            style_overrides={}, allowed_actions=['intake_copy'], allowed_processors=[],
            review_destination=dict(self.review_destination), retry_allowance=2,
        )
        grant.update(overrides)
        return grant

    def make_status(self, **overrides):
        status = dict(schema_version='1', grant_id='grant-1', job_id='job-1',
                      issuer_id='status', key_id='s1', checked_at=ts(NOW), status='active')
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

    def assert_no_staging_files(self):
        files = [str(Path(directory) / name)
                 for directory, _, names in os.walk(self.base_staging_dir)
                 for name in names]
        self.assertEqual(files, [])

    def assert_denied(self, result, reason):
        self.assertFalse(result.success)
        self.assertIsNone(result.spec)
        self.assertEqual(result.deny_reason, reason)
        self.assert_no_staging_files()

    def test_happy_path_viralpop(self):
        before = compute_file_sha256(self.source_path)
        result = self.run_intake()
        self.assertTrue(result.success)
        self.assertIsNone(result.deny_reason)
        spec = result.spec
        self.assertEqual(spec.schema_version, '1')
        self.assertEqual(spec.style_id, 'viralpop')
        self.assertEqual(spec.source.sha256, self.expected_hash)
        staged = Path(spec.staging.staged_input_path)
        self.assertTrue(staged.is_file())
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o400)
        self.assertEqual(compute_file_sha256(staged), self.expected_hash)
        self.assertEqual(before, compute_file_sha256(self.source_path))
        self.assertEqual(spec.source.size_bytes, len(self.source_bytes))
        self.assertEqual(spec.source.canonical_source_path, str(self.source_path.resolve()))
        self.assertEqual(spec.job_id, 'job-1')
        self.assertEqual(spec.grant_id, 'grant-1')
        self.assertEqual(spec.review_destination, self.review_destination)
        self.assertEqual(spec.retry_allowance, 2)
        self.assertEqual(spec.allowed_actions, frozenset({'intake_copy'}))
        self.assertEqual(spec.allowed_processors, frozenset())
        self.assertEqual(spec.created_at, ts(NOW))
        self.assertEqual(spec.style_overrides, {})
        self.assertIsInstance(spec.style_overrides, MappingProxyType)
        self.assertIsInstance(spec.review_destination, MappingProxyType)
        for directory in (staged.parent.parent, staged.parent,
                          Path(spec.staging.job_output_dir)):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(Path(spec.staging.candidate_output_path).name, 'candidate.mp4')
        self.assertFalse(Path(spec.staging.candidate_output_path).exists())
        with self.assertRaises(FrozenInstanceError):
            spec.job_id = 'changed'
        with self.assertRaises(FrozenInstanceError):
            spec.source.sha256 = 'b' * 64
        with self.assertRaises(FrozenInstanceError):
            spec.staging.staged_input_path = 'changed'
        with self.assertRaises(TypeError):
            spec.style_overrides['font_size'] = 24
        with self.assertRaises(TypeError):
            spec.review_destination['id'] = 'changed'
        self.host['review_destination']['id'] = 'changed'
        self.host['allowed_actions'].append('render_candidate')
        self.assertEqual(spec.review_destination['id'], 'review-1')
        self.assertEqual(spec.allowed_actions, frozenset({'intake_copy'}))

    def test_happy_path_paper_chips(self):
        result = self.run_intake(grant=self.make_grant(style_id='paper-chips'),
                                 style_id='paper-chips')
        self.assertTrue(result.success)
        self.assertEqual(result.spec.style_id, 'paper-chips')

    def test_missing_style_field(self):
        grant = self.make_grant()
        del grant['style_id']
        self.assert_denied(self.run_intake(grant=grant), 'AUTH_SCHEMA_INVALID')

    def test_empty_style(self):
        self.assert_denied(self.run_intake(grant=self.make_grant(style_id='')),
                           'STYLE_NOT_AUTHORIZED')

    def test_unauthorized_style_from_extra_13(self):
        self.assert_denied(self.run_intake(grant=self.make_grant(style_id='karaoke')),
                           'STYLE_NOT_AUTHORIZED')

    def test_non_empty_style_overrides(self):
        grant = self.make_grant(style_overrides={'font_size': 24})
        self.assert_denied(self.run_intake(grant=grant), 'STYLE_NOT_AUTHORIZED')

    def test_malformed_grant_json(self):
        self.assert_denied(self.run_intake(grant_bytes=b'{not json'), 'AUTH_SCHEMA_INVALID')

    def test_expired_grant(self):
        self.assert_denied(self.run_intake(now=NOW + timedelta(hours=2)),
                           'AUTH_EXPIRED_OR_NOT_YET_VALID')

    def test_revoked_grant(self):
        self.assert_denied(self.run_intake(status=self.make_status(status='revoked')),
                           'AUTH_REVOKED')

    def test_stale_revocation_status(self):
        status = self.make_status(checked_at=ts(NOW - timedelta(minutes=6)))
        self.assert_denied(self.run_intake(status=status), 'REVOCATION_STALE_OR_MISMATCH')

    def test_grant_replay_for_another_job(self):
        self.assert_denied(self.run_intake(existing_binding='job-other'), 'GRANT_REPLAY')

    def test_destination_mismatch(self):
        destination = dict(self.review_destination, id='review-other')
        self.assert_denied(self.run_intake(review_destination=destination),
                           'DESTINATION_NOT_AUTHORIZED')

    def test_source_metadata_mismatch(self):
        for field, value in (('provider', 'other'), ('id', 'source-other'), ('revision', 'r2')):
            with self.subTest(field=field):
                self.assert_denied(self.run_intake(source_ref=dict(self.source_ref, **{field: value})),
                                   'SOURCE_MISMATCH')

    def test_source_digest_mismatch(self):
        self.source_path.write_bytes(b'different-synthetic-video')
        self.assert_denied(self.run_intake(), 'SOURCE_DIGEST_MISMATCH')

    def test_symlink_source_file(self):
        link = self.source_root / 'link.mp4'
        link.symlink_to(self.source_path)
        self.assert_denied(self.run_intake(source_path=link), 'UNSAFE_SOURCE_SYMLINK')

    def test_symlink_parent_directory(self):
        link = self.source_root / 'linked-parent'
        link.symlink_to(self.source_root, target_is_directory=True)
        self.assert_denied(self.run_intake(source_path=link / self.source_path.name),
                           'UNSAFE_SOURCE_SYMLINK')

    def test_path_traversal_source(self):
        outside = self.root / 'outside.mp4'
        outside.write_bytes(self.source_bytes)
        self.assert_denied(self.run_intake(source_path=self.source_root / '..' / outside.name),
                           'UNSAFE_PATH_TRAVERSAL')

    def test_staging_aliasing_source(self):
        self.base_staging_dir = self.source_root / 'staging'
        before = compute_file_sha256(self.source_path)
        self.assert_denied(self.run_intake(base_staging_dir=self.base_staging_dir),
                           'UNSAFE_OUTPUT_ALIAS')
        self.assertEqual(before, compute_file_sha256(self.source_path))
        self.assertFalse(self.base_staging_dir.exists())
        self.assert_no_staging_files()

    def test_source_immutability_hash_only(self):
        before_hash = compute_file_sha256(self.source_path)
        before_size = self.source_path.stat().st_size
        self.assertTrue(self.run_intake().success)
        self.assertEqual(before_hash, compute_file_sha256(self.source_path))
        self.assertEqual(before_size, self.source_path.stat().st_size)

    def test_job_spec_serialize_round_trip(self):
        spec = self.run_intake().spec
        serialized = serialize_job_spec(spec)
        self.assertEqual(deserialize_job_spec(serialized), spec)
        self.assertEqual(serialize_job_spec(deserialize_job_spec(serialized)), serialized)
        payload = json.loads(serialized)
        tampered = [dict(payload, unknown=True), dict(payload, schema_version='2'),
                    dict(payload, source=dict(payload['source'], sha256='bad'))]
        for value in tampered:
            with self.subTest(value=value), self.assertRaises(JobSpecError):
                deserialize_job_spec(canonical(value))

    def test_source_not_found(self):
        self.assert_denied(self.run_intake(source_path=self.source_root / 'missing.mp4'),
                           'SOURCE_NOT_FOUND')

    def test_invalid_trusted_host_parameters(self):
        cases = [
            ({'style_id': 'karaoke'}, 'STYLE_NOT_AUTHORIZED'),
            ({'style_overrides': {'font_size': 24}}, 'STYLE_NOT_AUTHORIZED'),
            ({'style_overrides': []}, 'STYLE_NOT_AUTHORIZED'),
            ({'source_ref': dict(self.source_ref, extra='x')}, 'AUTH_SCHEMA_INVALID'),
            ({'source_ref': dict(self.source_ref, sha256='A' * 64)}, 'AUTH_SCHEMA_INVALID'),
            ({'source_ref': dict(self.source_ref, sha256=123)}, 'AUTH_SCHEMA_INVALID'),
            ({'review_destination': {'provider': 'test', 'id': 'review-1'}},
             'AUTH_SCHEMA_INVALID'),
            ({'retry_allowance': True}, 'AUTH_SCHEMA_INVALID'),
            ({'retry_allowance': -1}, 'AUTH_SCHEMA_INVALID'),
            ({'retry_allowance': 3}, 'AUTH_SCHEMA_INVALID'),
        ]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides), patch.object(intake, 'verify') as verify:
                self.assert_denied(self.run_intake(**overrides), reason)
                verify.assert_not_called()

    def test_trusted_grant_permission_and_retry_bindings(self):
        cases = [
            ({'grant_id': 'grant-other'}, 'AUTH_INVALID_OR_UNTRUSTED'),
            ({'allowed_actions': ['intake_copy', 'render_candidate']}, 'ACTION_NOT_AUTHORIZED'),
            ({'allowed_processors': ['unapproved']}, 'ACTION_NOT_AUTHORIZED'),
            ({'retry_allowance': 1}, 'AUTH_SCHEMA_INVALID'),
        ]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                self.assert_denied(self.run_intake(**overrides), reason)

    def test_unauthorized_intake_action(self):
        grant = self.make_grant(allowed_actions=['prepare_script'])
        self.assert_denied(self.run_intake(grant=grant), 'ACTION_NOT_AUTHORIZED')

    def test_invalid_signature(self):
        raw = sign(self.make_grant(), self.status_key, GRANT_DOMAIN)
        self.assert_denied(self.run_intake(grant_bytes=raw), 'AUTH_INVALID_OR_UNTRUSTED')

    def test_source_mutates_during_copy(self):
        copy_source = intake.copy_source_to_staging

        def mutate_then_copy(source, destination, digest):
            source.write_bytes(b'changed-during-copy')
            return copy_source(source, destination, digest)

        with patch.object(intake, 'copy_source_to_staging', side_effect=mutate_then_copy):
            self.assert_denied(self.run_intake(), 'SOURCE_MUTATED_ERROR')
        self.assertFalse((self.base_staging_dir / 'jobs' / 'job-1').exists())
        self.assert_no_staging_files()

    def test_source_mutates_after_copy(self):
        copy_source = intake.copy_source_to_staging

        def copy_then_mutate(source, destination, digest):
            result = copy_source(source, destination, digest)
            source.write_bytes(b'x' * len(self.source_bytes))
            return result

        with patch.object(intake, 'copy_source_to_staging', side_effect=copy_then_mutate):
            self.assert_denied(self.run_intake(), 'SOURCE_MUTATED_ERROR')
        self.assertFalse((self.base_staging_dir / 'jobs' / 'job-1').exists())
        self.assert_no_staging_files()

    def test_job_spec_error_cleans_staging(self):
        with patch.object(intake, 'create_job_spec', side_effect=ValueError('AUTH_SCHEMA_INVALID')):
            self.assert_denied(self.run_intake(), 'AUTH_SCHEMA_INVALID')
        self.assertFalse((self.base_staging_dir / 'jobs' / 'job-1').exists())
        self.assert_no_staging_files()

    def test_unexpected_exception_propagates(self):
        with patch.object(intake, 'create_job_spec', side_effect=RuntimeError('synthetic bug')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic bug'):
                self.run_intake()
        self.assert_no_staging_files()

    def test_staged_input_and_output_hardlink_aliases(self):
        allocate = intake.allocate_staging_paths
        before = compute_file_sha256(self.source_path)
        for field in ('staged_input_path', 'candidate_output_path'):
            def allocate_alias(*args):
                paths = allocate(*args)
                os.link(self.source_path, getattr(paths, field))
                return paths

            with self.subTest(field=field), patch.object(
                    intake, 'allocate_staging_paths', side_effect=allocate_alias):
                self.assert_denied(self.run_intake(), 'UNSAFE_OUTPUT_ALIAS')
                self.assertEqual(before, compute_file_sha256(self.source_path))
                self.assert_no_staging_files()

    def test_symlink_before_parent_traversal(self):
        directory = self.source_root / 'directory'
        directory.mkdir()
        link = self.source_root / 'link'
        link.symlink_to(directory, target_is_directory=True)
        path = link / '..' / self.source_path.name
        self.assert_denied(self.run_intake(source_path=path), 'UNSAFE_SOURCE_SYMLINK')

    def test_staging_directory_symlink(self):
        self.base_staging_dir.mkdir()
        (self.base_staging_dir / 'jobs').symlink_to(self.source_root, target_is_directory=True)
        before = compute_file_sha256(self.source_path)
        self.assert_denied(self.run_intake(), 'UNSAFE_OUTPUT_ALIAS')
        self.assertEqual(before, compute_file_sha256(self.source_path))
        self.assert_no_staging_files()

    def test_invalid_staging_job_identifier(self):
        for job_id in ('job/escape', '.', '..', 'a' * 129):
            with self.subTest(job_id=job_id):
                self.assert_denied(self.run_intake(
                    grant=self.make_grant(job_id=job_id),
                    status=self.make_status(job_id=job_id), job_id=job_id),
                    'AUTH_SCHEMA_INVALID')

    def test_staging_suffix_validation(self):
        for index, (name, suffix) in enumerate((('video.MP4', '.MP4'), ('video', ''),
                                               ('video.bad!', ''), ('video.abcdefghijk', ''))):
            with self.subTest(name=name):
                paths = intake.allocate_staging_paths(self.base_staging_dir, f'job-{index}', name)
                self.assertEqual(Path(paths.staged_input_path).name, 'source' + suffix)

    def test_strict_job_spec_parser(self):
        serialized = serialize_job_spec(self.run_intake().spec)
        payload = json.loads(serialized)
        invalid = [
            'not bytes', bytearray(serialized), b' ' * 65537, b'\xff', b'{not json',
            b'[]', b'NaN', b'Infinity', b'-Infinity', b'1.0', b'1e0',
            b'{"schema_version":"1","schema_version":"1"}',
            serialized.replace(b'"retry_allowance":2', b'"retry_allowance":2.0'),
            serialized.replace(b'"provider":"test"', b'"provider":"test","provider":"test"', 1),
        ]
        for overrides in (
            {'schema_version': 1}, {'style_id': ''}, {'style_overrides': {'font_size': 24}},
            {'style_overrides': []}, {'retry_allowance': True}, {'retry_allowance': 3},
            {'source': dict(payload['source'], size_bytes=-1)},
            {'source': dict(payload['source'], size_bytes=True)},
            {'source': dict(payload['source'], sha256='A' * 64)},
            {'source': dict(payload['source'], unknown=True)},
            {'staging': dict(payload['staging'], unknown=True)},
            {'staging': {}}, {'job_id': ''}, {'grant_id': ''},
            {'allowed_actions': 'intake_copy'}, {'allowed_actions': ['intake_copy'] * 2},
            {'allowed_processors': [1]}, {'review_destination': {}},
            {'created_at': '2026-02-30T12:00:00Z'},
        ):
            invalid.append(canonical(dict(payload, **overrides)))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(JobSpecError):
                deserialize_job_spec(value)

    def test_job_spec_factory_rejects_invalid_invariants(self):
        args = dict(
            job_id='job-1', grant_id='grant-1',
            source=SourceSnapshot('test', 'source-1', 'r1', self.expected_hash,
                                  len(self.source_bytes), str(self.source_path)),
            style_id='viralpop', style_overrides={}, allowed_actions=['intake_copy'],
            allowed_processors=[], review_destination=dict(self.review_destination),
            retry_allowance=2, staging=StagingPaths('work', 'work/source', 'output',
                                                  'output/candidate.mp4'), created_at=ts(NOW),
        )
        for overrides in (
            {'job_id': ''}, {'grant_id': 1}, {'retry_allowance': True}, {'retry_allowance': -1},
            {'source': replace(args['source'], sha256='bad')},
            {'source': replace(args['source'], size_bytes=-1)},
            {'source': replace(args['source'], size_bytes=True)},
            {'style_id': 'karaoke'}, {'style_overrides': MappingProxyType({})},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                create_job_spec(**dict(args, **overrides))


if __name__ == '__main__':
    unittest.main()
