"""Local per-batch semantic checkpoints. Only confirmed, validated JSON is saved."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

from . import providers

MAX_BYTES = 2 * 1024 * 1024
_LOCKS = tuple(threading.Lock() for _ in range(32))
# Only undurable, already-paid responses live here. Retain them until a later
# retry can save successfully; do not turn a local disk failure into a new call.
_UNSAVED = {}


def invalid():
    return providers.ProviderError('语义匹配缓存校验失败，已停止；未重新提交已保存批次。', code='invalid_cache')


def fingerprint(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf8')
        if len(encoded) > 32 * 1024 * 1024:
            raise ValueError
        return hashlib.sha256(encoded).hexdigest()
    except (TypeError, ValueError, RecursionError):
        raise providers.ProviderError('语义匹配缓存上下文格式无效，尚未提交。', code='invalid_input') from None


def _recovery_path(path):
    # A separately writable sibling, never below the possibly read-only primary
    # cache directory. The root digest prevents contexts from colliding.
    root_id = hashlib.sha256(os.fsencode(str(path.parent))).hexdigest()
    return path.parent.parent / 'semantic-recovery' / root_id / path.name


def _present(path):
    return path.exists() or path.is_symlink()


def _atomic_write(path, body):
    name = None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf8', dir=path.parent,
                prefix='semantic-write-', suffix='.tmp', delete=False) as out:
            name = Path(out.name)
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if name is not None and name.exists():
            try:
                name.unlink()
            except OSError:
                pass


def _writable(path):
    probe = path.parent / ('semantic-probe-' + uuid.uuid4().hex + '.tmp')
    try:
        _atomic_write(probe, '{}')
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        try:
            if probe.is_file():
                probe.unlink()
        except OSError:
            pass


@contextmanager
def locked(directory, identity, cancel):
    if directory is None:
        yield None
        return
    try:
        root = Path(directory)
        if root.is_symlink():
            raise invalid()
        # Do not mkdir here: failure of the primary must still allow recovery.
        path = root.absolute() / ('semantic-' + identity + '.json')
    except (OSError, TypeError, ValueError):
        raise invalid() from None
    lock = _LOCKS[int(identity, 16) % len(_LOCKS)]
    while not lock.acquire(timeout=.1):
        providers._check_cancel(cancel)
    try:
        providers._check_cancel(cancel)
        recovery = _recovery_path(path)
        # Existing entries must go through strict load/semantic validation even
        # when the disk is read-only. A bad entry is not a paid cache miss.
        if (not _present(path) and not _present(recovery) and (str(path), identity) not in _UNSAVED
                and not (_writable(path) or _writable(recovery))):
            raise providers.ProviderError('主缓存和备用检查点均不可写，已在收费调用前停止；请检查磁盘空间和目录权限。',
                                          code='cache_write')
        yield path
    finally:
        lock.release()


def load(path, identity, decode, *, persist_memory=True):
    if path is None:
        return None
    from_memory = False
    try:
        for source in (path, _recovery_path(path)):
            if not _present(source):
                continue
            if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BYTES:
                raise ValueError
            data = decode(source.read_text(encoding='utf8'))
            break
        else:
            body = _UNSAVED.get((str(path), identity))
            if body is None:
                return None
            data = decode(body)
            from_memory = True
        if (not isinstance(data, dict) or set(data) != {'version', 'identity', 'result'}
                or type(data['version']) is not int or data['version'] != 1
                or data['identity'] != identity or not isinstance(data['result'], dict)):
            raise ValueError
        if from_memory and persist_memory:
            # Once storage is repaired, promote the already validated response
            # before clearing its only remaining copy from process memory.
            save(path, identity, data['result'])
        return data['result']
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise invalid() from None


def save(path, identity, result):
    if path is None:
        return
    key = (str(path), identity)
    try:
        body = json.dumps({'version': 1, 'identity': identity, 'result': result},
                          ensure_ascii=False, allow_nan=False)
        if len(body.encode('utf8')) > MAX_BYTES:
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise providers.ProviderError('语义匹配已返回，但本地检查点编码失败；未重新提交。',
                                      code='cache_write') from None
    _UNSAVED[key] = body
    for destination in (path, _recovery_path(path)):
        try:
            _atomic_write(destination, body)
            _UNSAVED.pop(key, None)
            return
        except (OSError, ValueError, TypeError):
            continue
    raise providers.ProviderError('语义匹配已完成，但主缓存和备用检查点保存失败；结果保留在本进程内存，'
                                  '请勿关闭程序，修复磁盘后重试可复用结果；未重新提交。',
                                  code='cache_write', result_unknown=False)
