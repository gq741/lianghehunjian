"""Manifest-bound rollback for explicitly marked NEW JDMixStudio workspaces.

Only listed regular files move. Originals in other projects, unlisted runtime
data, dependency directories, and the rollback controller remain untouched.
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
import uuid

PROJECT = "JDMixStudio-v1-new"
MARKER = ".jdmix-v1-workspace"
CONTROLLERS = {MARKER.casefold(), "rollback.sh", "tools/rollback.py"}


class RollbackError(RuntimeError):
    pass


def _regular_path(path):
    """Reject symlinks, junctions and all Windows reparse points in ancestry."""
    for current in [path, *path.parents]:
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise RollbackError(f"REPARSE_PATH_REJECTED: {current}")


def _root_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise RollbackError("ROOT_MUST_BE_ABSOLUTE_WITHOUT_TRAVERSAL")
    _regular_path(path)
    root = path.resolve(strict=True)
    if not root.is_dir() or root.parent == root:
        raise RollbackError("DRIVE_ROOT_OR_NON_DIRECTORY_REJECTED")
    marker = root / MARKER
    _regular_path(marker)
    if not marker.is_file() or marker.read_text("utf-8").strip() != PROJECT:
        raise RollbackError("NEW_PROJECT_MARKER_REQUIRED")
    return root


def _relative(value):
    if not isinstance(value, str) or not value:
        raise RollbackError("INVALID_MANIFEST_PATH")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        normalized.startswith("/")
        or any(c in normalized for c in "\x00\r\n:")
        or any(p in {"", ".", ".."} or p.endswith((" ", "."))
               or p.split(".")[0].upper() in reserved for p in parts)
        or parts[0].casefold().startswith(".rollback-")
    ):
        raise RollbackError(f"UNSAFE_RELATIVE_PATH: {value}")
    return PurePosixPath(*parts).as_posix()


def _inside(root, relative):
    target = root.joinpath(*PurePosixPath(relative).parts)
    _regular_path(target)
    resolved = target.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise RollbackError("PATH_ESCAPES_WORKSPACE")
    return target


def _digest(path):
    if not path.is_file():
        raise RollbackError(f"EXPECTED_REGULAR_FILE: {path}")
    _regular_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path):
    _regular_path(path)
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise RollbackError("INVALID_METADATA_FILE")
    try:
        value = json.loads(path.read_text("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise RollbackError("INVALID_METADATA_JSON") from exc
    if not isinstance(value, dict) or value.get("project") != PROJECT:
        raise RollbackError("PROJECT_ID_MISMATCH")
    return value


def _entries(data):
    rows = data.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise RollbackError("MANIFEST_REQUIRES_1_TO_10000_FILES")
    entries, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise RollbackError("INVALID_FILE_ENTRY")
        name = _relative(row.get("path"))
        key = name.casefold()
        if key in seen or key in CONTROLLERS:
            raise RollbackError("DUPLICATE_PATH_OR_ROLLBACK_CONTROLLER")
        seen.add(key)
        digest = row.get("sha256", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RollbackError("INVALID_SHA256")
        entries.append({"path": name, "sha256": digest.lower()})
    return entries


def _manifest(root, manifest):
    path = Path(manifest).absolute()
    _regular_path(path)
    rows = _entries(_json(path))
    for row in rows:
        if _inside(root, row["path"]).resolve(strict=False) == path.resolve(strict=True):
            raise RollbackError("MANIFEST_MUST_REMAIN_AVAILABLE")
    return rows


def _verify_files(root, rows):
    for row in rows:
        if _digest(_inside(root, row["path"])) != row["sha256"]:
            raise RollbackError(f"HASH_CHANGED: {row['path']}")


def _write_snapshot(path, data):
    temporary = path.with_name(f".snapshot-{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(temporary, path)  # Only our own snapshot metadata, never a source file.


def _move_no_overwrite(source, destination):
    _regular_path(source)
    _regular_path(destination)
    if destination.exists():
        raise RollbackError(f"DESTINATION_EXISTS: {destination}")
    if os.name == "nt":
        # Windows rename fails if another process has created the destination.
        os.rename(source, destination)
    else:
        # POSIX rename can overwrite; hardlink+unlink retains no-clobber behavior.
        os.link(source, destination, follow_symlinks=False)
        source.unlink()


def _entrypoint_status(root):
    return "ENTRYPOINT_PRESENT" if (root / "app.py").is_file() else "ENTRYPOINT_ABSENT"


def rollback(root, rows):
    _verify_files(root, rows)  # No directory creation or moves before all checks pass.
    snapshot_dir = root / (".rollback-" + uuid.uuid4().hex)
    _regular_path(snapshot_dir)
    snapshot_dir.mkdir()
    storage = snapshot_dir / "files"
    storage.mkdir()
    snapshot_path = snapshot_dir / "snapshot.json"
    data = {"project": PROJECT, "root": str(root), "state": "prepared", "files": rows}
    _write_snapshot(snapshot_path, data)
    moved = []
    try:
        for row in rows:
            source = _inside(root, row["path"])
            destination = _inside(storage, row["path"])
            if _digest(source) != row["sha256"]:
                raise RollbackError(f"HASH_CHANGED_DURING_MOVE: {row['path']}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _move_no_overwrite(source, destination)
            moved.append(row)
            if _digest(destination) != row["sha256"]:
                raise RollbackError(f"HASH_CHANGED_DURING_MOVE: {row['path']}")
        data["state"] = "rolled_back"
        _write_snapshot(snapshot_path, data)
    except Exception as exc:
        recovery_errors = []
        for row in reversed(moved):
            try:
                _move_no_overwrite(_inside(storage, row["path"]), _inside(root, row["path"]))
            except Exception as recovery:
                recovery_errors.append(str(recovery))
        data["state"] = "rollback_aborted" if not recovery_errors else "manual_recovery_required"
        data["recovery_errors"] = recovery_errors
        _write_snapshot(snapshot_path, data)
        raise RollbackError(f"ROLLBACK_ABORTED snapshot={snapshot_path}: {exc}") from exc
    return {"status": "ROLLBACK_OK", "moved": len(rows),
            "entrypoint": _entrypoint_status(root), "snapshot": str(snapshot_path)}


def restore(root, rows, snapshot):
    snapshot_path = Path(snapshot)
    if snapshot_path.is_dir():
        snapshot_path /= "snapshot.json"
    _regular_path(snapshot_path)
    snapshot_path = snapshot_path.resolve(strict=True)
    if (snapshot_path.name != "snapshot.json" or snapshot_path.parent.parent != root
            or not snapshot_path.parent.name.startswith(".rollback-")):
        raise RollbackError("SNAPSHOT_MUST_BE_DIRECT_WORKSPACE_CHILD")
    data = _json(snapshot_path)
    if data.get("root") != str(root) or data.get("state") != "rolled_back":
        raise RollbackError("SNAPSHOT_ROOT_OR_STATE_MISMATCH")
    snapshot_rows = _entries(data)
    if sorted((r["path"], r["sha256"]) for r in snapshot_rows) != sorted(
        (r["path"], r["sha256"]) for r in rows
    ):
        raise RollbackError("SNAPSHOT_MANIFEST_MISMATCH")
    storage = snapshot_path.parent / "files"
    _verify_files(storage, rows)
    for row in rows:
        if _inside(root, row["path"]).exists():
            raise RollbackError(f"RESTORE_DESTINATION_EXISTS: {row['path']}")
    restored = []
    try:
        for row in rows:
            source = _inside(storage, row["path"])
            destination = _inside(root, row["path"])
            if _digest(source) != row["sha256"]:
                raise RollbackError(f"SNAPSHOT_HASH_CHANGED_DURING_RESTORE: {row['path']}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _move_no_overwrite(source, destination)
            restored.append(row)
            if _digest(destination) != row["sha256"]:
                raise RollbackError(f"SNAPSHOT_HASH_CHANGED_DURING_RESTORE: {row['path']}")
        data["state"] = "restored"
        _write_snapshot(snapshot_path, data)
    except Exception as exc:
        recovery_errors = []
        for row in reversed(restored):
            try:
                _move_no_overwrite(_inside(root, row["path"]), _inside(storage, row["path"]))
            except Exception as recovery:
                recovery_errors.append(str(recovery))
        data["state"] = "rolled_back" if not recovery_errors else "manual_recovery_required"
        data["recovery_errors"] = recovery_errors
        _write_snapshot(snapshot_path, data)
        raise RollbackError(f"RESTORE_ABORTED snapshot={snapshot_path}: {exc}") from exc
    return {"status": "RESTORE_OK", "restored": len(rows),
            "entrypoint": _entrypoint_status(root), "snapshot": str(snapshot_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--confirm-new-project", action="store_true", required=True)
    parser.add_argument("--restore")
    args = parser.parse_args(argv)
    try:
        root = _root_path(args.root)
        rows = _manifest(root, args.manifest)
        result = restore(root, rows, args.restore) if args.restore else rollback(root, rows)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (RollbackError, OSError) as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
