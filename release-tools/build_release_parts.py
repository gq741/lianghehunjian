"""Build a stored ZIP and GitHub-sized raw parts from a verified file allowlist.

No third-party Python dependencies. This script never edits the input tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import zipfile


PRODUCT = "量禾混剪"
VERSION = "20261024"
ARCHIVE_NAME = "LiangHeMix-20261024.zip"
ROOT_DIR = "LiangHeMix"
PART_BYTES = 1_000_000_000
BUFFER_BYTES = 1024 * 1024
HEX256 = re.compile(r"[a-f0-9]{64}\Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BUFFER_BYTES), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def validate_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"Invalid relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(x in ("", ".", "..") for x in value.split("/")):
        raise ValueError(f"Invalid relative path: {value!r}")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"Control character in relative path: {value!r}")
    return value


def ordinary_file(root: Path, relative: str) -> Path:
    relative = validate_relative(relative)
    candidate = root.joinpath(*relative.split("/"))
    # Check the actual root; its ancestors can be normal user-owned directories.
    root_status = root.lstat()
    if stat.S_ISLNK(root_status.st_mode) or getattr(root_status, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"Input root is a link/reparse point: {root}")
    current = root
    for piece in relative.split("/"):
        current = current / piece
        status = current.lstat()
        if stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Input is a link/reparse point: {relative}")
    status = candidate.stat()
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise ValueError(f"Input is not an independent ordinary file: {relative}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Input escapes source root: {relative}")
    return candidate


def source_records(root: Path) -> tuple[list[dict], str]:
    manifest_file = ordinary_file(root, "FILES.sha256.json")
    manifest_hash = sha256_file(manifest_file)
    rows = read_json(manifest_file)
    if not isinstance(rows, list) or not rows:
        raise ValueError("FILES.sha256.json must be a nonempty array")
    seen = set()
    records = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Manifest row must be an object")
        relative = validate_relative(row.get("path"))
        identity = relative.casefold()
        if identity in seen or identity == "files.sha256.json":
            raise ValueError(f"Duplicate/self-referential manifest path: {relative}")
        if relative.split("/", 1)[0].casefold() == "logs":
            raise ValueError("Manifest must not publish runtime logs")
        seen.add(identity)
        size = row.get("bytes")
        digest = row.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not HEX256.fullmatch(digest):
            raise ValueError(f"Invalid manifest metadata: {relative}")
        path = ordinary_file(root, relative)
        if path.stat().st_size != size:
            raise ValueError(f"Source size mismatch: {relative}")
        records.append({"path": relative, "bytes": size, "sha256": digest})
    records.append({"path": "FILES.sha256.json", "bytes": manifest_file.stat().st_size, "sha256": manifest_hash})
    return sorted(records, key=lambda row: row["path"]), manifest_hash


def create_archive(root: Path, archive: Path, records: list[dict]) -> None:
    with archive.open("xb") as raw:
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as target:
            for row in records:
                path = ordinary_file(root, row["path"])
                info = zipfile.ZipInfo(f"{ROOT_DIR}/{row['path']}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 0
                info.external_attr = 0x20
                info.file_size = row["bytes"]
                digest = hashlib.sha256()
                copied = 0
                with path.open("rb") as source, target.open(info, "w", force_zip64=True) as output:
                    for block in iter(lambda: source.read(BUFFER_BYTES), b""):
                        digest.update(block)
                        copied += len(block)
                        output.write(block)
                if copied != row["bytes"] or digest.hexdigest() != row["sha256"]:
                    raise ValueError(f"Source hash mismatch or source changed during build: {row['path']}")
    with zipfile.ZipFile(archive) as check:
        failed = check.testzip()
        if failed is not None:
            raise ValueError(f"ZIP CRC failed: {failed}")
        if check.namelist() != [f"{ROOT_DIR}/{row['path']}" for row in records]:
            raise ValueError("ZIP file ordering/allowlist mismatch")
        if any(info.compress_type != zipfile.ZIP_STORED for info in check.infolist()):
            raise ValueError("ZIP compression must be ZIP_STORED")


def split_archive(archive: Path, output: Path, part_bytes: int) -> list[dict]:
    if type(part_bytes) is not int or part_bytes <= 0 or part_bytes >= 2**31:
        raise ValueError("Part size must be positive and below 2 GiB")
    parts = []
    with archive.open("rb") as source:
        for number in range(1, 100):
            initial = source.read(min(BUFFER_BYTES, part_bytes))
            if not initial:
                break
            name = f"{ARCHIVE_NAME}.part{number:02d}"
            count = 0
            digest = hashlib.sha256()
            with (output / name).open("xb") as target:
                block = initial
                while block:
                    count += len(block)
                    digest.update(block)
                    target.write(block)
                    block = source.read(min(BUFFER_BYTES, part_bytes - count)) if count < part_bytes else b""
            parts.append({"name": name, "bytes": count, "sha256": digest.hexdigest()})
        else:
            if source.read(1):
                raise ValueError("Archive requires more than 99 parts")
    return parts


def verify_assets(output: Path, archive_dir: Path | None = None) -> dict:
    manifest = read_json(output / "PARTS.json")
    fixed = {"schema": 1, "product": PRODUCT, "version": VERSION, "archive_name": ARCHIVE_NAME, "root_dir": ROOT_DIR}
    if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in fixed.items()):
        raise ValueError("PARTS.json schema/product/version/archive/root mismatch")
    for field in ("archive_sha256", "manifest_sha256"):
        if not isinstance(manifest.get(field), str) or not HEX256.fullmatch(manifest[field]):
            raise ValueError(f"Invalid {field}")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 99:
        raise ValueError("PARTS.json parts must contain 1 to 99 entries")
    total = 0
    whole = hashlib.sha256()
    for number, row in enumerate(parts, 1):
        expected_name = f"{ARCHIVE_NAME}.part{number:02d}"
        if not isinstance(row, dict) or row.get("name") != expected_name:
            raise ValueError(f"Unexpected part name at position {number}")
        size, expected_hash = row.get("bytes"), row.get("sha256")
        if type(size) is not int or not 0 < size < 2**31 or not isinstance(expected_hash, str) or not HEX256.fullmatch(expected_hash):
            raise ValueError(f"Invalid metadata for {expected_name}")
        path = ordinary_file(output, expected_name)
        if path.stat().st_size != size:
            raise ValueError(f"Part size mismatch: {expected_name}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(BUFFER_BYTES), b""):
                whole.update(block)
                digest.update(block)
                total += len(block)
        if digest.hexdigest() != expected_hash:
            raise ValueError(f"Part hash mismatch: {expected_name}")
    if type(manifest.get("archive_bytes")) is not int or total != manifest["archive_bytes"] or whole.hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Concatenated archive size/hash mismatch")
    for field in ("source_files", "unpacked_bytes"):
        if field in manifest and (type(manifest[field]) is not int or manifest[field] <= 0):
            raise ValueError(f"Invalid {field}")
    result = {"parts": len(parts), "archive_bytes": total, "archive_sha256": whole.hexdigest(), "parts_verified": True, "zip_crc_verified": False}
    archive = archive_dir / ARCHIVE_NAME if archive_dir else None
    if archive and archive.exists():
        if archive.stat().st_size != total or sha256_file(archive) != whole.hexdigest():
            raise ValueError("Retained ZIP differs from verified parts")
        with zipfile.ZipFile(archive) as check:
            failed = check.testzip()
            if failed is not None:
                raise ValueError(f"Retained ZIP CRC failed: {failed}")
            entries = check.infolist()
            if "source_files" in manifest and len(entries) != manifest["source_files"]:
                raise ValueError("Retained ZIP file count mismatch")
            if "unpacked_bytes" in manifest and sum(entry.file_size for entry in entries) != manifest["unpacked_bytes"]:
                raise ValueError("Retained ZIP unpacked size mismatch")
            embedded = check.read(f"{ROOT_DIR}/FILES.sha256.json")
            if hashlib.sha256(embedded).hexdigest() != manifest["manifest_sha256"]:
                raise ValueError("Embedded file manifest hash mismatch")
        result["zip_crc_verified"] = True
    return result


def build(source: Path, output: Path, archive_dir: Path, part_bytes: int = PART_BYTES) -> dict:
    source, output, archive_dir = source.absolute(), output.absolute(), archive_dir.absolute()
    if output.resolve().is_relative_to(source.resolve()) or archive_dir.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output/archive directory must not be inside the input tree")
    records, manifest_hash = source_records(source)
    output.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / ARCHIVE_NAME
    if archive.exists() or (output / "PARTS.json").exists() or list(output.glob(f"{ARCHIVE_NAME}.part*")):
        raise FileExistsError("Release ZIP/parts already exist; use a fresh output directory")
    create_archive(source, archive, records)
    parts = split_archive(archive, output, part_bytes)
    manifest = {"schema": 1, "product": PRODUCT, "version": VERSION, "archive_name": ARCHIVE_NAME, "root_dir": ROOT_DIR,
                "archive_bytes": archive.stat().st_size, "archive_sha256": sha256_file(archive), "parts": parts, "manifest_sha256": manifest_hash,
                "source_files": len(records), "unpacked_bytes": sum(row["bytes"] for row in records)}
    write_json(output / "PARTS.json", manifest)
    verified = verify_assets(output, archive_dir)
    return {"source": str(source), "output": str(output), "retained_archive": str(archive), "source_files": len(records),
            "manifest_sha256": manifest_hash, "zip_method": "ZIP_STORED", "part_bytes": part_bytes, **verified}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--verify-assets-only", action="store_true")
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    if not args.verify_assets_only and args.source is None:
        parser.error("--source is required for build")
    evidence = {"command": [sys.executable, *sys.argv], "input": {"source": str(args.source) if args.source else None,
                "output": str(args.output), "archive_dir": str(args.archive_dir) if args.archive_dir else None}, "mode": "verify" if args.verify_assets_only else "build"}
    start = time.monotonic()
    try:
        if args.verify_assets_only:
            result = verify_assets(args.output, args.archive_dir)
        else:
            result = build(args.source, args.output, args.archive_dir or args.output.parent / "archive-cache")
        evidence.update({"result": result, "literal_output": "RELEASE_PARTS=PASS", "exit_status": 0})
    except Exception as exc:
        evidence.update({"error": f"{type(exc).__name__}: {exc}", "literal_output": "RELEASE_PARTS=FAIL", "exit_status": 1})
    evidence["elapsed_seconds"] = round(time.monotonic() - start, 3)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.evidence, evidence)
    print(evidence["literal_output"])
    print(json.dumps(evidence.get("result", evidence.get("error")), ensure_ascii=False))
    return evidence["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())
