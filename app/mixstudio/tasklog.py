"""Append-only local task events; never serialize payloads or remote responses."""
from datetime import datetime
from pathlib import Path
import json
import math
import threading
import time
from collections import OrderedDict

from .diagnostics import normalize_diagnostic

_LOCK = threading.Lock()
MAX_LOG_BYTES = 5 * 1024 * 1024
PROGRESS_INTERVAL = 5.
_RECENT = OrderedDict()
_TERMINAL = {'cancelled', 'failed', 'completed', 'interrupted', 'review_required', 'needs_review'}
_KINDS = {'scan', 'download', 'prepare', 'render', 'product', 'generate', 'script', 'match',
          'v2_generate', 'v2_match', 'product_read', 'media_prepare', 'preflight',
          'alignment', 'music_import'}
_STATES = {'queued', 'running', 'cancelling', 'cancelled', 'failed', 'completed',
           'interrupted', 'review_required', 'needs_review'}


def append_event(root, job):
    now = datetime.now().astimezone()
    identifier = str(job.get('id', ''))
    progress = job.get('progress', 0)
    if type(progress) not in (int, float) or not math.isfinite(progress):
        progress = 0
    event = {
        'time': now.isoformat(timespec='seconds'),
        'job_id': identifier if identifier.isalnum() and len(identifier) <= 64 else 'invalid-id',
        'kind': job.get('kind') if job.get('kind') in _KINDS else 'task',
        'status': job.get('status') if job.get('status') in _STATES else 'unknown',
        'progress': round(max(0, min(1, progress)), 4),
        'error_present': bool(job.get('error')),
        'result_unknown': bool(job.get('result_unknown')),
    }
    diagnostic = normalize_diagnostic(job.get('diagnostic'))
    if diagnostic:
        event['diagnostic'] = diagnostic
    folder = Path(root) / 'logs'
    encoded = (json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n').encode('utf8')
    with _LOCK:
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
               for p in (folder, *folder.parents)):
            raise ValueError('日志目录包含链接')
        clock = time.monotonic()
        key = str(folder.absolute()), event['job_id']
        stable = (event['kind'], event['status'], event['error_present'], event['result_unknown'],
                  json.dumps({k:v for k,v in diagnostic.items() if k not in
                      {'elapsed_ms','response_bytes','request_id','response_sha256'}}, sort_keys=True))
        previous = _RECENT.get(key)
        if (event['status'] not in _TERMINAL and previous is not None
                and previous[0]==stable and 0 <= clock-previous[1] < PROGRESS_INTERVAL):
            return
        folder.mkdir(parents=True, exist_ok=True)
        base = f'tasks-{now:%Y-%m-%d}'
        index = 0
        while True:
            path = folder/(base+(f'.{index:04}' if index else '')+'.jsonl')
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('日志文件包含链接')
            if not path.exists() or path.stat().st_size+len(encoded)<=MAX_LOG_BYTES:
                break
            index += 1
        with path.open('ab') as output:
            output.write(encoded)
        _RECENT[key] = stable, clock
        _RECENT.move_to_end(key)
        while len(_RECENT)>2048:
            _RECENT.popitem(last=False)


def record(root, job):
    # SQLite remains authoritative when a disk/logging failure occurs.
    try:
        append_event(root, job)
    except (OSError, ValueError, TypeError):
        pass
