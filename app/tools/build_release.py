"""Allowlisted V2 source-only handoff packager; no runtime or verification data.

The bundled ROLLBACK.sh is the legacy new-workspace remove/restore controller,
not a V2-to-V1 downgrade. This release's downgrade evidence stays outside ZIP.
"""
from pathlib import Path
import argparse
import difflib
import hashlib
import json
import re
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = [
    ".gitignore", ".jdmix-v1-workspace", "AGENTS.md", "CONTRACT.md", "V2_CONTRACT.md",
    "README.md", "HANDOFF.md", "ACCEPTANCE.md", "DEPENDENCIES.md", "app.py",
    "install.ps1", "start.ps1", "run_tests.ps1", "启动混剪工作台.vbs",
    "requirements.txt", "requirements-lock.txt", "ROLLBACK.sh",
]
PROMPT_FILES = ["prompts/script-v2.md"]
LEGACY_NOTICE = """京东自动混剪 V2 源码交接包 — 回滚说明

本包 ROLLBACK.sh 与 tools/rollback.py 是 legacy（V1 留存）的“标记新工作区源码撤销/恢复”控制器。
它们不是 V2 降级到 V1 的工具，不会恢复上一版业务实现；执行时会按 SOURCE_MANIFEST.json
移动当前标记工作区的源码。保留 .jdmix-v1-workspace 和旧 project ID 只为该控制器兼容。

本轮 V2→修改前版本的专用回滚脚本与验收证据在交接方的外部 verification/v2-20261021
证据目录维护，不打入此源码 ZIP。需要版本降级时应使用对应证据目录内的专用脚本与备份。
不要把本包根目录 ROLLBACK.sh 当作 V2 降级命令。

打包产物 DIFF_FILE.patch 是将白名单源码从 /dev/null 导出的完整源码补丁，
不是 V1→V2 的增量差异。源码版本见 release 字段；legacy manifest project 字段不代表版本。
本包不含密钥、.env、运行数据库、浏览器登录态、日志、缓存、依赖安装目录、模型或媒体。
"""
SECRET_PATTERNS = {
    "api_key_token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "literal_bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9_.-]{25,}"),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_bytes(path):
    """Refuse link/junction ancestors and keep a single scanned byte snapshot."""
    for current in (path, *path.parents):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise RuntimeError(f"Source reparse path rejected: {current}")
        if current == ROOT:
            break
    if not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"Required regular source missing: {path}")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise RuntimeError(f"Source file exceeds packaging limit: {path}")
    return path.read_bytes()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError("Output directory already exists; choose a new directory without existing files")
    target = output / "京东自动混剪_第二版_源码.zip"
    paths = [ROOT / p for p in ROOT_FILES + PROMPT_FILES]
    paths += sorted((ROOT / "mixstudio").glob("*.py"))
    paths += sorted((ROOT / "tests").glob("*.py"))
    paths += sorted((ROOT / "tools").glob("*.py"))
    paths += [ROOT / "web" / p for p in ["app.js", "index.html", "styles.css"]]
    paths = sorted(set(paths), key=lambda p: p.relative_to(ROOT).as_posix())
    entries, findings, contents = [], [], {}
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        contents[relative] = source_bytes(path)
    contents["LEGACY_ROLLBACK_NOTICE.txt"] = LEGACY_NOTICE.encode("utf-8")
    for relative, data in sorted(contents.items()):
        text = data.decode("utf-8-sig")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append({"path": relative, "pattern": label})
        entries.append({"path": relative, "sha256": digest(data), "bytes": len(data)})
    if findings:
        raise RuntimeError("Potential secret detected; stop packaging: " + json.dumps(findings))
    controllers = {".jdmix-v1-workspace", "ROLLBACK.sh", "tools/rollback.py"}
    manifest = {"project": "JDMixStudio-v1-new",
                "release": "JDMixStudio-v2",
                "rollback_mode": "legacy_new_workspace_remove_restore_NOT_v2_downgrade",
                "description": "V2 sources; project ID retained solely for legacy source remove/restore controller. NOT a V2-to-V1 downgrade. Runtime and old projects untouched.",
                "files": [{"path": e["path"], "sha256": e["sha256"]}
                          for e in entries if e["path"] not in controllers]}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    # Do not overwrite even an empty existing output directory. Scan before writes.
    output.mkdir(parents=True, exist_ok=False)
    (output / "SOURCE_MANIFEST.json").write_bytes(manifest_bytes)
    scan = {"status": "PASS", "files_checked": len(entries), "findings": [],
            "method": "Explicit source-only allowlist + source/docs token/private-key patterns; heuristic scan, no runtime inputs",
            "excluded": [".env", "keys", "DPAPI", "runtime", "SQLite", "browser profiles",
                         "logs", "caches", ".venv", "node_modules", "models", "video", "audio", "verification"]}
    (output / "SECRET_SCAN.json").write_text(json.dumps(scan, indent=2), encoding="utf-8")
    (output / "PACKAGE_FILES.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    patch = []
    for relative, data in sorted(contents.items()):
        patch.extend(difflib.unified_diff([], data.decode("utf-8-sig").splitlines(True),
                                         fromfile="/dev/null", tofile="b/" + relative))
    (output / "DIFF_FILE.patch").write_text("".join(patch), encoding="utf-8")
    (output / "LEGACY_ROLLBACK_NOTICE.txt").write_bytes(contents["LEGACY_ROLLBACK_NOTICE.txt"])
    (output / "ROLLBACK.sh").write_bytes(contents["ROLLBACK.sh"])
    (output / "tools").mkdir(exist_ok=True)
    (output / "tools" / "rollback.py").write_bytes(contents["tools/rollback.py"])
    (output / "ROLLBACK.sh").chmod(0o755)
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, data in sorted(contents.items()):
            info = zipfile.ZipInfo(relative)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = 0o755 if relative == "ROLLBACK.sh" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, data)
        archive.writestr("SOURCE_MANIFEST.json", manifest_bytes)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC check failed")
        if set(archive.namelist()) != set(contents) | {"SOURCE_MANIFEST.json"}:
            raise RuntimeError("ZIP entry allowlist verification failed")
        for entry in entries:
            if digest(archive.read(entry["path"])) != entry["sha256"]:
                raise RuntimeError("ZIP source hash verification failed")
        if archive.read("SOURCE_MANIFEST.json") != manifest_bytes:
            raise RuntimeError("ZIP manifest verification failed")
        if not (archive.getinfo("ROLLBACK.sh").external_attr >> 16) & 0o111:
            raise RuntimeError("ZIP rollback executable mode missing")
    summary = {"zip": str(target), "sha256": digest(target.read_bytes()), "bytes": target.stat().st_size,
               "files": len(entries) + 1, "release": "JDMixStudio-v2", "zip_crc": "PASS", "source_hashes": "PASS",
               "secret_scan": "PASS", "verification_included": False,
               "rollback_mode": manifest["rollback_mode"]}
    (output / "PACKAGE_RESULT.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
