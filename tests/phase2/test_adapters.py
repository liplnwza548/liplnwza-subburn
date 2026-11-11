"""P2-009 adapter contract tests (plan section 8.1, 14 tests).

Offline only: the "Drive" here is a local folder simulating a Drive
parent (fake Drive). No Google library, no HTTP, no upload, no real
keys. Test keys are labelled ``test-key-...``.
"""

import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from subtitle.adapters import (
    AdapterError,
    ColabAdapter,
    LocalAdapter,
)
from subtitle.core.orchestrator import _preflight_capabilities

NOW_ISO = '2026-11-11T12:00:00Z'
TEST_KEY = 'test-key-adapters-demo-001'
SOURCE_BYTES = b'synthetic-source-bytes' * 40
ARTIFACT_BYTES = b'fake-candidate-mp4-bytes' * 100


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trusted = self.root / 'trusted'
        self.trusted.mkdir()
        self.source_path = self.trusted / 'source.mp4'
        self.source_path.write_bytes(SOURCE_BYTES)
        self.source_sha = _sha(SOURCE_BYTES)
        self.source_ref = {
            'provider': 'test',
            'id': 'source-1',
            'revision': 'r1',
            'sha256': self.source_sha,
        }
        self.staging = self.root / 'staging'
        self.staging.mkdir()
        self.fake_drive = self.root / 'fakedrive'
        self.fake_drive.mkdir()
        (self.fake_drive / 'parent-1').mkdir()
        self.artifact = self.root / 'candidate.mp4'
        self.artifact.write_bytes(ARTIFACT_BYTES)
        self.artifact_sha = _sha(ARTIFACT_BYTES)
        self.local_dest = {
            'provider': 'test',
            'id': 'review-1',
            'owner_id': 'philip',
        }
        self.drive_dest = {
            'provider': 'drive',
            'id': 'parent-1',
            'owner_id': 'philip',
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _local(self) -> LocalAdapter:
        return LocalAdapter(base_staging_dir=self.staging)

    def _colab(self, credential: str | None = TEST_KEY) -> ColabAdapter:
        return ColabAdapter(
            fake_drive_root=self.fake_drive,
            fake_drive_parent_id='parent-1',
            fake_drive_credential=credential,
        )

    def _deliver_local(self):
        return self._local().deliver_review(
            job_id='job-1',
            artifact_path=self.artifact,
            destination=self.local_dest,
            expected_destination=self.local_dest,
            target_state='READY_FOR_REVIEW',
            evidence_id='ev_qc_job-1',
            now=NOW_ISO,
        )

    def _deliver_colab(self):
        return self._colab().deliver_review(
            job_id='job-1',
            artifact_path=self.artifact,
            destination=self.drive_dest,
            expected_destination=self.drive_dest,
            target_state='READY_FOR_REVIEW',
            evidence_id='ev_qc_job-1',
            now=NOW_ISO,
        )

    # 1. Equivalent fixtures decide identically on both adapters.
    def test_same_decision_vm_and_colab(self):
        local = self._local()
        colab = self._colab()
        local_caps = local.check_capabilities(now=NOW_ISO)
        colab_caps = colab.check_capabilities(now=NOW_ISO)
        # Core decisions are identical; only Drive fields differ by design.
        self.assertFalse(local_caps.blocked)
        self.assertFalse(colab_caps.blocked)
        self.assertTrue(local_caps.capabilities.ffmpeg_available)
        self.assertTrue(colab_caps.capabilities.ffmpeg_available)
        self.assertTrue(local_caps.capabilities.ffprobe_available)
        self.assertTrue(colab_caps.capabilities.ffprobe_available)
        self.assertEqual(
            local_caps.capabilities.mfa_tool_available,
            colab_caps.capabilities.mfa_tool_available,
        )

        local_src = local.resolve_source(
            source_ref=self.source_ref,
            source_path=self.source_path,
            trusted_source_root=self.trusted,
        )
        colab_src = colab.resolve_source(
            source_ref=self.source_ref,
            source_path=self.source_path,
            trusted_source_root=self.trusted,
        )
        self.assertEqual(local_src, colab_src)
        self.assertEqual(local_src, self.source_path)

        self.assertEqual(
            local.staging_root(base_staging_dir=self.staging),
            colab.staging_root(base_staging_dir=self.staging),
        )

        local_receipt = self._deliver_local()
        colab_receipt = self._deliver_colab()
        self.assertEqual(
            local_receipt.artifact_sha256, colab_receipt.artifact_sha256,
        )
        self.assertEqual(local_receipt.artifact_sha256, self.artifact_sha)
        self.assertTrue(local_receipt.offline)
        self.assertTrue(colab_receipt.offline)
        self.assertEqual(local_receipt.evidence_id, colab_receipt.evidence_id)

    # 2. A missing ffmpeg flips both adapters' reports the same way.
    def test_same_capability_report_both_adapters(self):
        real_which = shutil.which

        def fake_which(name):
            if name == 'ffmpeg':
                return None
            return real_which(name)

        with patch('shutil.which', side_effect=fake_which):
            local_report = self._local().check_capabilities(now=NOW_ISO)
            colab_report = self._colab().check_capabilities(now=NOW_ISO)
        for report in (local_report, colab_report):
            self.assertTrue(report.blocked)
            self.assertEqual(report.blocking_reason, 'SYSTEM_DEPENDENCY_MISSING')
            self.assertFalse(report.capabilities.ffmpeg_available)
            self.assertTrue(report.capabilities.ffprobe_available)
            self.assertIn('ffmpeg', report.capabilities.missing)
        self.assertEqual(
            local_report.capabilities.ffmpeg_available,
            colab_report.capabilities.ffmpeg_available,
        )
        self.assertEqual(local_report.blocked, colab_report.blocked)

    # 3. A missing staging root is denied; no root is created, no fallback.
    def test_missing_staging_root_denies_no_fallback(self):
        missing = self.root / 'does-not-exist'
        with self.assertRaises(AdapterError) as ctx:
            self._local().staging_root(base_staging_dir=missing)
        self.assertEqual(ctx.exception.code, 'ADAPTER_ROOT_MISSING')
        self.assertFalse(missing.exists())
        # No other folder was silently created as a substitute.
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ['candidate.mp4', 'fakedrive', 'staging', 'trusted'],
        )

    # 4. A missing fake Drive root is denied, never guessed elsewhere.
    def test_missing_fake_drive_root_denies(self):
        missing_root = self.root / 'no-drive-here'
        adapter = ColabAdapter(
            fake_drive_root=missing_root,
            fake_drive_parent_id='parent-1',
            fake_drive_credential=TEST_KEY,
        )
        report = adapter.check_capabilities(now=NOW_ISO)
        self.assertEqual(report.capabilities.drive_access, 'unavailable')
        self.assertIn('fake_drive_root', report.capabilities.missing)
        with self.assertRaises(AdapterError) as ctx:
            adapter.deliver_review(
                job_id='job-1',
                artifact_path=self.artifact,
                destination=self.drive_dest,
                expected_destination=self.drive_dest,
                target_state='READY_FOR_REVIEW',
                evidence_id='ev_qc_job-1',
                now=NOW_ISO,
            )
        self.assertEqual(ctx.exception.code, 'ADAPTER_ROOT_MISSING')
        self.assertFalse(missing_root.exists())

    # 5. A missing ffmpeg blocks the pre-flight as FAILED_SYSTEM.
    def test_missing_ffmpeg_blocks_preflight(self):
        real_which = shutil.which

        def fake_which(name):
            if name == 'ffmpeg':
                return None
            return real_which(name)

        with patch('shutil.which', side_effect=fake_which):
            report = self._local().check_capabilities(now=NOW_ISO)
            self.assertTrue(report.blocked)
            self.assertEqual(report.blocking_reason, 'SYSTEM_DEPENDENCY_MISSING')
            denied = _preflight_capabilities(
                adapter=self._local(),
                now_iso=NOW_ISO,
                review_destination=self.local_dest,
            )
        self.assertIsNotNone(denied)
        self.assertFalse(denied.success)
        self.assertEqual(denied.final_state, 'FAILED_SYSTEM')
        self.assertEqual(denied.deny_code, 'SYSTEM_DEPENDENCY_MISSING')
        # No job folder was created for a pre-flight failure.
        self.assertFalse((self.staging / 'jobs').exists())

    # 6. A missing Drive credential is reported (not a secret), and the
    # orchestrator maps it to BLOCKED_NEEDS_REVIEW only for a Drive
    # destination (plan section 10, D6).
    def test_missing_drive_credential_reported(self):
        adapter = self._colab(credential=None)
        report = adapter.check_capabilities(now=NOW_ISO)
        self.assertEqual(report.capabilities.drive_access, 'unavailable')
        self.assertIn('drive_credential', report.capabilities.missing)
        self.assertFalse(report.blocked)
        with self.assertRaises(AdapterError) as ctx:
            adapter.deliver_review(
                job_id='job-1',
                artifact_path=self.artifact,
                destination=self.drive_dest,
                expected_destination=self.drive_dest,
                target_state='READY_FOR_REVIEW',
                evidence_id='ev_qc_job-1',
                now=NOW_ISO,
            )
        self.assertEqual(ctx.exception.code, 'ADAPTER_NOT_READY')
        denied = _preflight_capabilities(
            adapter=adapter,
            now_iso=NOW_ISO,
            review_destination=self.drive_dest,
        )
        self.assertIsNotNone(denied)
        self.assertFalse(denied.success)
        self.assertEqual(denied.final_state, 'BLOCKED_NEEDS_REVIEW')
        self.assertEqual(denied.deny_code, 'ADAPTER_NOT_READY')
        # A non-Drive destination does not need the credential: no deny.
        self.assertIsNone(
            _preflight_capabilities(
                adapter=adapter,
                now_iso=NOW_ISO,
                review_destination=self.local_dest,
            ),
        )

    # 7. No parent id means delivery is denied before writing.
    def test_missing_parent_id_denies_delivery(self):
        adapter = ColabAdapter(
            fake_drive_root=self.fake_drive,
            fake_drive_parent_id='',
            fake_drive_credential=TEST_KEY,
        )
        with self.assertRaises(AdapterError) as ctx:
            adapter.deliver_review(
                job_id='job-1',
                artifact_path=self.artifact,
                destination=self.drive_dest,
                expected_destination=self.drive_dest,
                target_state='READY_FOR_REVIEW',
                evidence_id='ev_qc_job-1',
                now=NOW_ISO,
            )
        self.assertEqual(ctx.exception.code, 'ADAPTER_ROOT_MISSING')
        self.assertEqual(
            [p.name for p in self.fake_drive.iterdir()], ['parent-1'],
        )
        self.assertEqual(list((self.fake_drive / 'parent-1').iterdir()), [])

    # 8. A destination id that is not the grant's is denied, nothing written.
    def test_external_destination_id_denied(self):
        wrong_dest = dict(self.drive_dest, id='someone-elses-parent')
        with self.assertRaises(AdapterError) as ctx:
            self._colab().deliver_review(
                job_id='job-1',
                artifact_path=self.artifact,
                destination=wrong_dest,
                expected_destination=self.drive_dest,
                target_state='READY_FOR_REVIEW',
                evidence_id='ev_qc_job-1',
                now=NOW_ISO,
            )
        self.assertEqual(ctx.exception.code, 'ADAPTER_DESTINATION_MISMATCH')
        self.assertEqual(list((self.fake_drive / 'parent-1').iterdir()), [])

    # 9. A destination provider that is not the grant's is denied.
    def test_external_destination_provider_denied(self):
        wrong_dest = dict(self.drive_dest, provider='other-provider')
        with self.assertRaises(AdapterError) as ctx:
            self._colab().deliver_review(
                job_id='job-1',
                artifact_path=self.artifact,
                destination=wrong_dest,
                expected_destination=self.drive_dest,
                target_state='READY_FOR_REVIEW',
                evidence_id='ev_qc_job-1',
                now=NOW_ISO,
            )
        self.assertEqual(ctx.exception.code, 'ADAPTER_DESTINATION_MISMATCH')
        self.assertEqual(list((self.fake_drive / 'parent-1').iterdir()), [])

    # 10. Staging that overlaps the source root is unsafe.
    def test_staging_overlapping_source_denied(self):
        for unsafe_root in (
            self.trusted,
            self.trusted / 'staging',
        ):
            unsafe_root.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(AdapterError) as ctx:
                self._local().staging_root(
                    base_staging_dir=unsafe_root,
                    trusted_source_root=self.trusted,
                )
            self.assertEqual(ctx.exception.code, 'ADAPTER_ROOT_UNSAFE')

    # 11. Delivery never touches the staged source.
    def test_delivery_does_not_touch_source(self):
        before = _sha(self.source_path.read_bytes())
        receipt = self._deliver_colab()
        after = _sha(self.source_path.read_bytes())
        self.assertEqual(before, after)
        self.assertEqual(before, self.source_sha)
        delivered = self.fake_drive / 'parent-1' / 'OFFLINE-candidate.mp4'
        self.assertTrue(delivered.is_file())
        self.assertEqual(_sha(delivered.read_bytes()), receipt.artifact_sha256)

    # 12. No secret (test key value) appears in any report or receipt.
    def test_no_secret_in_capability_report(self):
        report = self._colab().check_capabilities(now=NOW_ISO)
        self.assertNotIn(TEST_KEY, repr(report))
        self.assertNotIn(TEST_KEY, report.notes)
        self.assertNotIn(TEST_KEY, str(report.capabilities))
        receipt = self._deliver_colab()
        self.assertNotIn(TEST_KEY, repr(receipt))

    # 13. Every demo artifact is labelled OFFLINE.
    def test_offline_label_on_receipt_and_files(self):
        for receipt in (self._deliver_local(), self._deliver_colab()):
            self.assertTrue(receipt.offline)
            self.assertIn('OFFLINE-', receipt.artifact_relpath)
        self.assertTrue(
            (self.staging / 'delivery' / 'job-1' / 'OFFLINE-candidate.mp4').is_file(),
        )
        self.assertTrue(
            (self.fake_drive / 'parent-1' / 'OFFLINE-candidate.mp4').is_file(),
        )
        # offline=False is refused, not silently accepted.
        with self.assertRaises(AdapterError) as ctx:
            self._local().deliver_review(
                job_id='job-1',
                artifact_path=self.artifact,
                destination=self.local_dest,
                expected_destination=self.local_dest,
                target_state='READY_FOR_REVIEW',
                evidence_id='ev_qc_job-1',
                offline=False,
                now=NOW_ISO,
            )
        self.assertEqual(ctx.exception.code, 'ADAPTER_NOT_READY')

    # 14. Test keys are labelled, and no real-Drive code exists.
    def test_test_keys_are_labeled_and_synthetic(self):
        self.assertTrue(TEST_KEY.startswith('test-key-'))
        repo = Path(__file__).resolve().parents[2]
        forbidden = (
            'googleapiclient',
            'pydrive',
            'gdown',
            'oauth',
            'upload_file',
            'drive.files.create',
        )
        for rel in (
            'subtitle/adapters/base.py',
            'subtitle/adapters/local.py',
            'subtitle/adapters/colab.py',
            'subtitle/adapters/__init__.py',
            'docs/adapters.md',
        ):
            text = (repo / rel).read_text(encoding='utf-8')
            for word in forbidden:
                self.assertNotIn(
                    word, text,
                    f"{rel} must never mention real Drive integration ({word})",
                )


if __name__ == '__main__':
    unittest.main()
