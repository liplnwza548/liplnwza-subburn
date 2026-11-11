"""Tests for the W1 step 1 temp cleanup job (P2-010 remediation, plan 3.2).

The cleanup script lives OUTSIDE the repo (Job Pack bin/) because it is
an operations job, not production code. These tests load it by path:
    <workspace>/subburn-jobpack/bin/cleanup-subburn-tmp.py
If the script is not there (e.g. a bare repo checkout), the tests skip
with a clear message instead of failing.

Rules under test (plan 3.2 — all five are hard rules):
  1. Only burner-prefixed dirs (chipstrip-*, protostrip-*) are candidates.
  2. Only dirs older than 24 h (mtime) are deleted.
  3. Only directories — never single files, never other names.
  4. Default mode is dry-run (delete nothing).
  5. Every deletion is logged (covered implicitly: delete_candidates
     returns one record per candidate with path/size/action).
"""

import importlib.util
import os
import time
import unittest
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "subburn-jobpack" / "bin" / "cleanup-subburn-tmp.py"
)

OLD_AGE_SECONDS = 25 * 3600  # older than the 24 h limit
NEW_AGE_SECONDS = 30 * 60    # younger than the 24 h limit


def load_script():
    if not SCRIPT_PATH.exists():
        raise unittest.SkipTest(
            f"cleanup script not found at {SCRIPT_PATH} — skipping")
    spec = importlib.util.spec_from_file_location(
        "cleanup_subburn_tmp", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTempCleanup(unittest.TestCase):

    def setUp(self):
        self.mod = load_script()

    def tearDown(self):
        tmpdir = getattr(self, "_tmpdir", None)
        if tmpdir is not None:
            tmpdir.cleanup()

    def _make_fixture(self):
        """Fake /tmp with old/new burner dirs, an other-named old dir,
        and a single file carrying a burner prefix."""
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fake-tmp-")
        root = Path(self._tmpdir.name)
        now = time.time()

        def aged_dir(name, age_seconds, with_file=True):
            d = root / name
            d.mkdir()
            if with_file:
                (d / "data.bin").write_bytes(b"x" * 100)
            old = now - age_seconds
            os.utime(d, (old, old))
            return d

        self.old_chip = aged_dir("chipstrip-old123", OLD_AGE_SECONDS)
        self.new_chip = aged_dir("chipstrip-new456", NEW_AGE_SECONDS)
        self.old_proto = aged_dir("protostrip-old789", OLD_AGE_SECONDS)
        self.new_proto = aged_dir("protostrip-new012", NEW_AGE_SECONDS)
        self.old_other = aged_dir("somethingelse-old", OLD_AGE_SECONDS)
        self.single_file = root / "chipstrip-singlefile"
        self.single_file.write_bytes(b"y" * 50)
        self.root = root
        return root

    def test_find_candidates_only_old_burner_dirs(self):
        root = self._make_fixture()
        found = self.mod.find_candidates(root, max_age_hours=24.0)
        self.assertEqual([p.name for p in found],
                         ["chipstrip-old123", "protostrip-old789"])

    def test_find_candidates_ignores_missing_root(self):
        self.assertEqual(
            self.mod.find_candidates(Path("/nonexistent-tmp-xyz"),
                                     max_age_hours=24.0), [])

    def test_is_candidate_rules(self):
        root = self._make_fixture()
        self.assertTrue(self.mod.is_candidate(self.old_chip))
        self.assertFalse(self.mod.is_candidate(self.single_file))
        self.assertFalse(self.mod.is_candidate(self.old_other))

    def test_dry_run_deletes_nothing(self):
        root = self._make_fixture()
        import io
        buf = io.StringIO()
        records = self.mod.delete_candidates(
            self.mod.find_candidates(root, max_age_hours=24.0),
            dry_run=True, out=buf)
        self.assertEqual([r["action"] for r in records],
                         ["would-delete", "would-delete"])
        self.assertTrue(self.old_chip.exists())
        self.assertTrue(self.old_proto.exists())

    def test_real_delete_removes_only_old_burner_dirs(self):
        root = self._make_fixture()
        import io
        buf = io.StringIO()
        records = self.mod.delete_candidates(
            self.mod.find_candidates(root, max_age_hours=24.0),
            dry_run=False, out=buf)
        self.assertEqual([r["action"] for r in records],
                         ["deleted", "deleted"])
        self.assertFalse(self.old_chip.exists())
        self.assertFalse(self.old_proto.exists())
        # Everything else must survive untouched.
        self.assertTrue(self.new_chip.exists())
        self.assertTrue(self.new_proto.exists())
        self.assertTrue(self.old_other.exists())
        self.assertTrue(self.single_file.exists())

    def test_dir_size_counts_files(self):
        root = self._make_fixture()
        self.assertEqual(self.mod.dir_size(self.old_chip), 100)


if __name__ == "__main__":
    unittest.main()
