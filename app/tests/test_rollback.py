"""Rollback tests only touch temporary, explicitly marked miniature projects."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tools import rollback as rb


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jdmix-rollback-test-")
        self.root = Path(self.temp.name)
        (self.root / rb.MARKER).write_text(rb.PROJECT, encoding="utf-8")
        (self.root / "app.py").write_text("print('source baseline')\n", encoding="utf-8")
        (self.root / "package").mkdir()
        (self.root / "package" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "runtime").mkdir()
        (self.root / "runtime" / "state.sqlite").write_bytes(b"UNLISTED_USER_DATA")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "keep.txt").write_text("installed dependencies")
        self.rows = [{"path": p, "sha256": rb._digest(self.root / p)}
                     for p in ["app.py", "package/core.py"]]
        self.manifest = self.root / "manifest.json"
        self.write_manifest(self.rows)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, rows, project=rb.PROJECT):
        self.manifest.write_text(json.dumps({"project": project, "files": rows}), encoding="utf-8")

    def inventory(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}

    def cli(self, *extra):
        return subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(Path(rb.__file__).resolve()),
             "--root", str(self.root), "--manifest", str(self.manifest),
             "--confirm-new-project", *extra],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )

    def test_roundtrip_and_unlisted_runtime_preserved(self):
        before = self.inventory()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "ROLLBACK_OK")
        self.assertEqual(report["moved"], 2)
        self.assertEqual(report["entrypoint"], "ENTRYPOINT_ABSENT")
        self.assertFalse((self.root / "app.py").exists())
        self.assertEqual((self.root / "runtime/state.sqlite").read_bytes(), b"UNLISTED_USER_DATA")
        self.assertEqual((self.root / ".venv/keep.txt").read_text(), "installed dependencies")
        result = self.cli("--restore", report["snapshot"])
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = json.loads(result.stdout)
        self.assertEqual(restored["status"], "RESTORE_OK")
        self.assertEqual(restored["entrypoint"], "ENTRYPOINT_PRESENT")
        for path, digest in before.items():
            self.assertEqual(rb._digest(self.root / path), digest)

    def test_changed_source_refused_without_any_side_effect(self):
        (self.root / "package/core.py").write_text("MODIFIED AFTER MANIFEST")
        before = self.inventory()
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("HASH_CHANGED", result.stderr)
        self.assertEqual(self.inventory(), before)
        self.assertEqual(list(self.root.glob(".rollback-*")), [])

    def test_restore_hash_change_refused_without_moving_other_files(self):
        report = rb.rollback(self.root, self.rows)
        snapshot = Path(report["snapshot"])
        (snapshot.parent / "files/package/core.py").write_text("changed snapshot")
        before = self.inventory()
        result = self.cli("--restore", str(snapshot))
        self.assertEqual(result.returncode, 2)
        self.assertIn("HASH_CHANGED", result.stderr)
        self.assertEqual(self.inventory(), before)

    def test_restore_existing_destination_refused_without_overwrite(self):
        report = rb.rollback(self.root, self.rows)
        (self.root / "app.py").write_text("new work")
        before = self.inventory()
        result = self.cli("--restore", report["snapshot"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("RESTORE_DESTINATION_EXISTS", result.stderr)
        self.assertEqual(self.inventory(), before)

    def test_missing_marker_refused(self):
        (self.root / rb.MARKER).unlink()
        before = self.inventory()
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.inventory(), before)

    def test_wrong_project_refused(self):
        self.write_manifest(self.rows, project="different")
        before = self.inventory()
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("PROJECT_ID_MISMATCH", result.stderr)
        self.assertEqual(self.inventory(), before)

    def test_bad_relative_paths_refused_before_actions(self):
        for path in ["../old.py", "/absolute.py", r"C:\old.py", r"..\old.py",
                     "app.py:secret", ".rollback-previous/files/a.py", "AUX.txt", "a//b.py"]:
            with self.subTest(path=path):
                self.write_manifest([{"path": path, "sha256": "0" * 64}])
                before = self.inventory()
                result = self.cli()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.inventory(), before)
                self.assertEqual(list(self.root.glob(".rollback-*")), [])

    def test_drive_root_refused(self):
        with self.assertRaisesRegex(rb.RollbackError, "DRIVE_ROOT"):
            rb._root_path(Path(self.root.anchor))

    def test_duplicate_paths_casefold_refused(self):
        with self.assertRaisesRegex(rb.RollbackError, "DUPLICATE"):
            rb._entries({"files": [self.rows[0], {**self.rows[0], "path": "APP.PY"}]})

    def test_controller_files_must_remain_available(self):
        for name in [rb.MARKER, "ROLLBACK.sh", "tools/rollback.py"]:
            with self.subTest(name=name), self.assertRaises(rb.RollbackError):
                rb._entries({"files": [{"path": name, "sha256": "0" * 64}]})

    def test_manifest_cannot_be_moved(self):
        rows = [*self.rows, {"path": "manifest.json", "sha256": rb._digest(self.manifest)}]
        self.write_manifest(rows)
        with self.assertRaisesRegex(rb.RollbackError, "MANIFEST_MUST_REMAIN_AVAILABLE"):
            rb._manifest(self.root, self.manifest)

    def test_symlink_source_refused(self):
        outside = self.root.parent / (self.root.name + "-outside.py")
        outside.write_text("OUTSIDE OLD SOURCE")
        link = self.root / "linked.py"
        try:
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            with self.assertRaisesRegex(rb.RollbackError, "REPARSE"):
                rb._inside(self.root, "linked.py")
            self.assertEqual(outside.read_text(), "OUTSIDE OLD SOURCE")
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    def test_symlink_guard_branch_without_os_privilege(self):
        with patch.object(Path, "lstat", return_value=SimpleNamespace(
            st_mode=stat.S_IFLNK, st_file_attributes=0
        )):
            with self.assertRaisesRegex(rb.RollbackError, "REPARSE"):
                rb._regular_path(self.root / "app.py")

    def test_failed_move_compensates_previous_moves(self):
        original = rb._move_no_overwrite
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated file lock")
            original(source, destination)
        with patch.object(rb, "_move_no_overwrite", side_effect=fail_second):
            with self.assertRaisesRegex(rb.RollbackError, "ROLLBACK_ABORTED"):
                rb.rollback(self.root, self.rows)
        rb._verify_files(self.root, self.rows)
        snapshot = next(self.root.glob(".rollback-*/snapshot.json"))
        self.assertEqual(json.loads(snapshot.read_text("utf-8"))["state"], "rollback_aborted")

    def test_snapshot_outside_workspace_refused(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other) / "snapshot.json"
            path.write_text(json.dumps({"project": rb.PROJECT, "files": self.rows}))
            with self.assertRaisesRegex(rb.RollbackError, "DIRECT_WORKSPACE_CHILD"):
                rb.restore(self.root, self.rows, path)

    def test_failed_restore_compensates_previous_moves(self):
        report = rb.rollback(self.root, self.rows)
        original = rb._move_no_overwrite
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated restore lock")
            original(source, destination)
        with patch.object(rb, "_move_no_overwrite", side_effect=fail_second):
            with self.assertRaisesRegex(rb.RollbackError, "RESTORE_ABORTED"):
                rb.restore(self.root, self.rows, report["snapshot"])
        self.assertFalse((self.root / "app.py").exists())
        snapshot = Path(report["snapshot"])
        rb._verify_files(snapshot.parent / "files", self.rows)
        self.assertEqual(json.loads(snapshot.read_text("utf-8"))["state"], "rolled_back")

    def test_confirmation_flag_required(self):
        result = subprocess.run(
            [sys.executable, "-B", str(Path(rb.__file__).resolve()),
             "--root", str(self.root), "--manifest", str(self.manifest)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.root / "app.py").exists())


if __name__ == "__main__":
    unittest.main()
