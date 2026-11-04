"""Offline, reversible program-only updates for the Windows folder edition.

No user-profile files are read, copied or replaced. No model API is called.
Run with the target edition's bundled Python, never an arbitrary system Python.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

GATE = '.lianghe-update.json'
BACKUPS = '.lianghe-updates'
ROOT_FILES = {'RELEASE.json', 'FILES.sha256.json', 'UPDATE_README.txt', 'WORKBUDDY.md',
              'AGENTS.md', 'LICENSE.txt', '先看这里.txt', '验收结果.txt'}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def clean_path(root, relative):
    """Reject traversal, ADS, symlinks, junctions and Windows alias spellings."""
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:
        raise ValueError('更新路径格式异常')
    parts = relative.split('/')
    if not parts or any(not p or p in ('.', '..') or p.rstrip(' .') != p for p in parts):
        raise ValueError('更新路径格式异常')
    for p in parts:
        if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I):
            raise ValueError('更新路径包含保留名称')
    root = Path(root).absolute()
    target = root.joinpath(*parts)
    current = root
    # Include ancestors: a chosen install directory itself may be a junction.
    for current in [*reversed(root.parents), root, *[root.joinpath(*parts[:i]) for i in range(1, len(parts)+1)]]:
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('更新目录包含链接或重定向，请选择实际安装目录')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('更新路径越界')
    return target


def allowed(relative):
    p = PurePosixPath(relative)
    return (relative in ROOT_FILES or
            (len(p.parts) == 3 and p.parts[:2] == ('app', 'mixstudio') and p.suffix == '.py') or
            (len(p.parts) == 3 and p.parts[:2] == ('app', 'tests') and p.suffix == '.py') or
            relative in {'app/web/app.js', 'app/web/index.html', 'app/web/styles.css',
                         'app/AGENTS.md', 'app/README.md', 'app/.impeccable/design.json',
                         'app/.impeccable/direction-contract.md', 'app/tests/fixtures/lianghe_frontend_contract.json',
                         'app/tools/apply_update.py', 'app/tools/portable_boot.py'})


@contextlib.contextmanager
def exclusive(root):
    path = clean_path(root, '.lianghe-update.lock')
    with path.open('a+b') as f:
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('另一更新程序正在运行，请等待完成') from None
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def running_processes(root):
    """Only inspect image paths. Never terminate a process or read command lines."""
    if os.name != 'nt':
        return []
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', w.DWORD),
                    ('cntThreads', w.DWORD), ('th32ParentProcessID', w.DWORD),
                    ('pcPriClassBase', w.LONG), ('dwFlags', w.DWORD), ('szExeFile', w.WCHAR*260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    snap = kernel.CreateToolhelp32Snapshot(2, 0)
    if snap == ctypes.c_void_p(-1).value:
        raise ValueError('进程检查失败，请稍后重试')
    found = []
    try:
        row = Entry(); row.dwSize = ctypes.sizeof(row)
        more = kernel.Process32FirstW(snap, ctypes.byref(row))
        while more:
            if row.th32ProcessID != os.getpid():
                handle = kernel.OpenProcess(0x1000, False, row.th32ProcessID)
                if handle:
                    try:
                        text = ctypes.create_unicode_buffer(32768); n = w.DWORD(len(text))
                        if kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(n)):
                            if Path(text.value).resolve().is_relative_to(Path(root).resolve()):
                                found.append(row.th32ProcessID)
                    finally:
                        kernel.CloseHandle(handle)
            more = kernel.Process32NextW(snap, ctypes.byref(row))
    finally:
        kernel.CloseHandle(snap)
    return found


def wait_closed(root, seconds=0):
    deadline = time.monotonic() + seconds
    while running_processes(root):
        if time.monotonic() >= deadline:
            raise ValueError('请先保存并退出量禾混剪（包括其他窗口），再运行更新')
        time.sleep(.5)


def validate(root, bundle):
    root, bundle = Path(root).absolute(), Path(bundle).absolute()
    for name in ('RELEASE.json', 'FILES.sha256.json', 'runtime/python.exe', 'app/tools/portable_check.py'):
        if not clean_path(root, name).is_file():
            raise ValueError('请选择完整文件夹版的安装目录（含 runtime、app 和 RELEASE.json）')
    manifest = read_json(clean_path(bundle, 'UPDATE.json'))
    if manifest.get('format') != 1 or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}(?:\.\d+)?', manifest.get('version','')):
        raise ValueError('更新包版本格式不符')
    current_digest = sha(root/'FILES.sha256.json')
    target_row = next((r for r in manifest.get('files', []) if r.get('path') == 'FILES.sha256.json'), {})
    if current_digest == target_row.get('sha256'):
        for row in manifest.get('files', []):
            if not allowed(row['path']) or sha(clean_path(root,row['path'])) != row['sha256']:
                raise ValueError('当前版本程序文件校验不符，请保留现场后检查')
        return manifest, None
    base = manifest.get('bases', {}).get(current_digest)
    if not base:
        raise ValueError('此更新包与安装版本不匹配，请下载对应更新包；原程序未修改')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError('更新文件清单无效')
    names = set()
    for row in rows:
        name = row.get('path')
        if not isinstance(name,str) or not allowed(name) or name.casefold() in names:
            raise ValueError('更新清单含未允许或重复文件')
        names.add(name.casefold())
        new = clean_path(bundle/'payload', name)
        if not new.is_file() or new.stat().st_size != row.get('bytes') or sha(new) != row.get('sha256'):
            raise ValueError('更新包校验失败，请重新下载；原程序未修改')
        old = clean_path(root, name)
        if name not in base.get('old', {}):
            raise ValueError('更新包缺少旧版本校验信息')
        expected = base['old'][name]
        actual = sha(old) if old.is_file() else None
        if actual != expected or (expected is None and old.exists()):
            raise ValueError('安装目录中的程序文件已变化，请保留现场并使用对应版本更新包')
        if name.endswith('.py'):
            ast.parse(new.read_text('utf-8-sig'), filename=name)
    # Detect modifications to unchanged executable app files as well.
    for row in read_json(root/'FILES.sha256.json'):
        name = row['path']
        if name.startswith('app/') and Path(name).suffix in ('.py', '.js', '.html', '.css'):
            item = clean_path(root, name)
            if not item.is_file() or sha(item) != row['sha256']:
                raise ValueError('原程序校验不符，请先恢复对应发行版；个人数据未修改')
    expected_new = {r['path']:r for r in read_json(bundle/'payload/FILES.sha256.json')}
    for row in rows:
        if row['path'] != 'FILES.sha256.json' and expected_new.get(row['path']) != row:
            raise ValueError('更新后总清单与补丁不一致')
    return manifest, base


def replace_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.update-tmp')
    try:
        shutil.copyfile(source, temp)
        with temp.open('r+b') as f:
            os.fsync(f.fileno())
        os.replace(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()


def selfcheck(root, output):
    command = [str(root/'runtime/python.exe'), '-I', '-B', '-X', 'utf8',
               str(root/'app/tools/portable_check.py'), '--output', str(output)]
    result = subprocess.run(command, cwd=root, capture_output=True, timeout=240,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('新版免费自检未通过，程序将恢复到更新前版本；请查看更新报告')
    return {'command': command, 'exit_status': result.returncode}


def restore(root, backup):
    journal = read_json(backup/'journal.json')
    if journal['root'] != str(root):
        raise ValueError('备份不属于此安装目录')
    # Verify everything first, so a later user edit is never overwritten.
    for row in journal['files']:
        path = clean_path(root, row['path'])
        current = sha(path) if path.is_file() else None
        if current not in (row['before'], row['after']):
            raise ValueError('程序文件在更新后再次变化，已停止回退以保留改动')
        if row['before'] is not None and sha(clean_path(backup/'original', row['path'])) != row['before']:
            raise ValueError('回退备份校验失败')
    for row in reversed(journal['files']):
        path = clean_path(root, row['path'])
        if row['before'] is None:
            if path.is_file():
                path.unlink()
        else:
            replace_file(clean_path(backup/'original', row['path']), path)
    for row in journal['files']:
        path = clean_path(root, row['path'])
        if (sha(path) if path.is_file() else None) != row['before']:
            raise ValueError('回退校验未通过，请保留备份与报告')
    journal['status'] = 'rolled_back'
    write_json(backup/'journal.json', journal)
    gate = clean_path(root, GATE)
    if gate.is_file() and read_json(gate).get('backup') == backup.name:
        gate.unlink()
    return {'status':'rolled_back', 'backup':str(backup), 'restored_files':len(journal['files'])}


def apply(root, bundle, *, checker=selfcheck, wait_seconds=0):
    root, bundle = Path(root).absolute(), Path(bundle).absolute()
    clean_path(root, 'RELEASE.json')
    with exclusive(root):
        wait_closed(root, wait_seconds)
        gate = clean_path(root, GATE)
        if gate.exists():
            name = read_json(gate).get('backup', '')
            if not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}', name):
                raise ValueError('更新恢复信息无效，请保留更新报告')
            backup = clean_path(root, BACKUPS+'/'+name)
            journal = read_json(backup/'journal.json')
            if journal['status'] == 'complete':
                gate.unlink()
            else:
                return restore(root, backup)
        manifest, base = validate(root, bundle)
        if base is None:
            return {'status':'already_current','version':manifest['version']}
        size = sum(row['bytes'] + (clean_path(root,row['path']).stat().st_size
                   if clean_path(root,row['path']).is_file() else 0) for row in manifest['files'])
        if shutil.disk_usage(root).free < size*2 + 64*1024*1024:
            raise ValueError('安装盘空间不足，请至少留出更新备份所需空间')
        name = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        backup = clean_path(root, BACKUPS+'/'+name)
        backup.mkdir(parents=True, exist_ok=False)
        rows = sorted(manifest['files'], key=lambda r:(r['path']!='app/tools/portable_boot.py',
                      r['path']=='FILES.sha256.json', r['path']))
        journal = {'root':str(root),'version':manifest['version'],'status':'backing_up','files':[]}
        for row in rows:
            source = clean_path(root, row['path']); old = base['old'][row['path']]
            if old is not None:
                saved = clean_path(backup/'original', row['path'])
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, saved)
                if sha(saved) != old:
                    raise ValueError('备份校验失败，原程序未修改')
            journal['files'].append({'path':row['path'],'before':old,'after':row['sha256']})
        journal['status'] = 'prepared'; write_json(backup/'journal.json', journal)
        write_json(gate, {'backup':name,'version':manifest['version']})
        try:
            wait_closed(root)
            journal['status'] = 'applying'; write_json(backup/'journal.json', journal)
            for row in rows:
                replace_file(clean_path(bundle/'payload',row['path']), clean_path(root,row['path']))
            for row in rows:
                if sha(clean_path(root,row['path'])) != row['sha256']:
                    raise ValueError('更新文件落盘校验失败')
            journal['status'] = 'validating'; write_json(backup/'journal.json',journal)
            check = checker(root, backup/'selfcheck.json')
            journal.update(status='complete',selfcheck=check)
            write_json(backup/'journal.json',journal)
            gate.unlink()
            return {'status':'updated','version':manifest['version'],'backup':str(backup),
                    'files':len(rows),'selfcheck':check,'personal_data':'untouched'}
        except Exception:
            restore(root, backup)
            raise


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--target', required=True, type=Path)
    p.add_argument('--bundle', type=Path)
    p.add_argument('--rollback', action='store_true')
    p.add_argument('--wait', type=int, default=0)
    p.add_argument('--notify', action='store_true')
    a = p.parse_args(argv); root = a.target.absolute()
    try:
        if Path(sys.executable).resolve() != (root/'runtime/python.exe').resolve():
            raise ValueError('请通过更新入口使用随包 Python 运行')
        if a.rollback:
            with exclusive(root):
                wait_closed(root, a.wait)
                backups = clean_path(root, BACKUPS)
                candidates = [x for x in backups.iterdir() if re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}',x.name)
                              and read_json(x/'journal.json').get('status') in ('complete','applying','validating','prepared')]
                if not candidates:
                    raise ValueError('没有可回退的更新备份')
                result = restore(root, sorted(candidates)[-1])
        else:
            if a.bundle is None:
                raise ValueError('请通过完整更新包运行')
            result = apply(root,a.bundle,wait_seconds=max(0,min(a.wait,120)))
        message = {'updated':'更新完成，免费自检通过。请重新打开量禾混剪。',
                   'already_current':'当前已是此更新版本。',
                   'rolled_back':'已恢复更新前的程序文件，个人数据保持不变。'}[result['status']]
        code = 0
    except Exception as e:
        message = str(e) if isinstance(e,ValueError) else '更新未完成，请查看更新报告；不要删除备份目录。'
        result={'status':'failed','message':message,'error_type':type(e).__name__}
        code=1
    if root.is_dir():
        write_json(clean_path(root,'update-result.json'),result)
    print(json.dumps(result,ensure_ascii=False))
    if a.notify and os.name=='nt':
        ctypes.windll.user32.MessageBoxW(None,message,'量禾混剪更新',0x40 if code==0 else 0x10)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
