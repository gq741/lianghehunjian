from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).with_name("build_release_parts.py")
SPEC = importlib.util.spec_from_file_location("release_parts", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ReleasePartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "源目录 空格"
        self.source.mkdir()
        self.output = self.root / "assets"
        self.archive_dir = self.root / "archive"
        self.make_source({"app/界面.py": "print('量禾')\n".encode(), "runtime/bytes.bin": bytes(range(256)) * 4, "zero.txt": b""})

    def tearDown(self):
        self.temp.cleanup()

    def make_source(self, files):
        rows = []
        for name, data in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.write_manifest(rows)

    def write_manifest(self, rows):
        (self.source / "FILES.sha256.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def build(self):
        return module.build(self.source, self.output, self.archive_dir, part_bytes=512)

    def manifest(self):
        return json.loads((self.output / "PARTS.json").read_text(encoding="utf-8"))

    def test_build_stored_crc_hash_and_fixed_raw_parts(self):
        result = self.build()
        self.assertTrue(result["parts_verified"])
        self.assertTrue(result["zip_crc_verified"])
        parts = self.manifest()["parts"]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(row["bytes"] == 512 for row in parts[:-1]))
        self.assertLessEqual(parts[-1]["bytes"], 512)
        merged = b"".join((self.output / row["name"]).read_bytes() for row in parts)
        archive = self.archive_dir / module.ARCHIVE_NAME
        self.assertEqual(merged, archive.read_bytes())
        self.assertEqual(hashlib.sha256(merged).hexdigest(), result["archive_sha256"])
        with zipfile.ZipFile(archive) as check:
            self.assertIsNone(check.testzip())
            self.assertTrue(all(row.compress_type == zipfile.ZIP_STORED for row in check.infolist()))
            self.assertEqual(check.namelist(), sorted(check.namelist()))
            self.assertEqual(check.read("LiangHeMix/app/界面.py"), "print('量禾')\n".encode())
            for row in check.infolist():
                self.assertEqual(row.date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(self.manifest()["source_files"], len(check.infolist()))
            self.assertEqual(self.manifest()["unpacked_bytes"], sum(row.file_size for row in check.infolist()))

    def test_nonallowlisted_personal_files_logs_and_secrets_not_published(self):
        (self.source / "logs").mkdir()
        (self.source / "logs" / "private.log").write_text("private")
        (self.source / "api-keys.txt").write_text("private")
        self.build()
        with zipfile.ZipFile(self.archive_dir / module.ARCHIVE_NAME) as check:
            self.assertEqual(len(check.namelist()), 4)
            self.assertNotIn("LiangHeMix/api-keys.txt", check.namelist())

    def test_input_bytes_unchanged(self):
        before = {p.relative_to(self.source).as_posix(): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.build()
        after = {p.relative_to(self.source).as_posix(): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_build_is_reproducible(self):
        first = self.build()
        second = module.build(self.source, self.root / "assets2", self.root / "archive2", part_bytes=512)
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])

    def test_source_hash_mismatch_stops_before_publish(self):
        path = self.source / "runtime" / "bytes.bin"
        path.write_bytes(b"Q" * path.stat().st_size)
        with self.assertRaisesRegex(ValueError, "Source hash mismatch"):
            self.build()
        self.assertFalse((self.output / "PARTS.json").exists())

    def test_wrong_source_size_fails(self):
        (self.source / "zero.txt").write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "Source size mismatch"):
            self.build()

    def test_path_traversal_and_windows_absolute_paths_fail(self):
        for value in ("../outside", "/absolute", "C:/absolute", "a\\b", "a//b", "a/./b", "a/../b", "a\n.txt"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate_relative(value)

    def test_duplicate_manifest_paths_case_insensitive_fail(self):
        row = {"path": "zero.txt", "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
        self.write_manifest([row, {**row, "path": "ZERO.txt"}])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.build()

    def test_logs_in_manifest_fail(self):
        self.make_source({"logs/private.log": b"private"})
        with self.assertRaisesRegex(ValueError, "runtime logs"):
            self.build()

    def test_hardlinks_fail(self):
        target = self.source / "zero.txt"
        os.link(target, self.root / "hardlinked.txt")
        with self.assertRaisesRegex(ValueError, "independent ordinary file"):
            self.build()

    def test_existing_release_not_overwritten(self):
        self.build()
        before = (self.output / "PARTS.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(before, (self.output / "PARTS.json").read_bytes())

    def test_output_inside_source_fails(self):
        with self.assertRaisesRegex(ValueError, "must not be inside"):
            module.build(self.source, self.source / "assets", self.archive_dir)

    def test_modified_part_fails_hash(self):
        self.build()
        part = self.output / self.manifest()["parts"][0]["name"]
        data = bytearray(part.read_bytes())
        data[-1] ^= 1
        part.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "Part hash mismatch"):
            module.verify_assets(self.output)

    def test_missing_part_fails(self):
        self.build()
        (self.output / self.manifest()["parts"][0]["name"]).unlink()
        with self.assertRaises(FileNotFoundError):
            module.verify_assets(self.output)

    def test_part_path_injection_fails(self):
        self.build()
        manifest = self.manifest()
        manifest["parts"][0]["name"] = "../outside"
        (self.output / "PARTS.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected part name"):
            module.verify_assets(self.output)

    def test_verify_without_retained_zip_needs_no_merge(self):
        self.build()
        verified = module.verify_assets(self.output)
        self.assertTrue(verified["parts_verified"])
        self.assertFalse(verified["zip_crc_verified"])

    def test_redundant_unpacked_metadata_verified_against_zip(self):
        self.build()
        manifest = self.manifest()
        manifest["unpacked_bytes"] += 1
        (self.output / "PARTS.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unpacked size mismatch"):
            module.verify_assets(self.output, self.archive_dir)

    def test_cli_build_and_verification_evidence(self):
        evidence = self.root / "build-evidence.json"
        args = [sys.executable, "-B", "-X", "utf8", str(SCRIPT), "--source", str(self.source), "--output", str(self.output),
                "--archive-dir", str(self.archive_dir), "--evidence", str(evidence)]
        done = subprocess.run(args, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertIn("RELEASE_PARTS=PASS", done.stdout)
        record = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual(record["exit_status"], 0)
        verify_evidence = self.root / "verify-evidence.json"
        done = subprocess.run([sys.executable, "-B", "-X", "utf8", str(SCRIPT), "--output", str(self.output), "--archive-dir", str(self.archive_dir),
                               "--verify-assets-only", "--evidence", str(verify_evidence)], text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertTrue(json.loads(verify_evidence.read_text(encoding="utf-8"))["result"]["zip_crc_verified"])

    def test_invalid_part_sizes_fail(self):
        for value in (0, -1, True, 2**31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.split_archive(Path("unused"), self.output, value)

    def test_cli_part_bytes_override(self):
        evidence = self.root / "small-build-evidence.json"
        done = subprocess.run([sys.executable, "-B", "-X", "utf8", str(SCRIPT), "--source", str(self.source),
                               "--output", str(self.output), "--archive-dir", str(self.archive_dir),
                               "--part-bytes", "512", "--evidence", str(evidence)],
                              text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        record = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual(record["input"]["part_bytes"], 512)
        self.assertEqual(record["result"]["part_bytes"], 512)
        self.assertGreater(record["result"]["parts"], 1)
        self.assertTrue(all(part["bytes"] == 512 for part in self.manifest()["parts"][:-1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
