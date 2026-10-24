"""Explicit, recoverable maintenance of allowlisted application-owned files.

No caller-supplied deletion paths. The service holds its action lock and blocks
new jobs while cleanup runs; this module also refuses an active job snapshot.
"""
from datetime import date
from pathlib import Path
import json
import os
import re
import time

from .recycle import recycle_file

TEMP_DAYS = 7
LOG_DAYS = 30
_CATEGORIES = (('voice-cache', '语音缓存'), ('visual-cache', '画面识别缓存'),
    ('semantic-cache', '画面匹配缓存'), ('sdr-cache', 'SDR 转码缓存'),
    ('subtitle-cache', '旧字幕定位缓存'),
    ('work', '临时文件'), ('logs', '任务日志'))
_HEX = r'[0-9a-f]{64}'
_LOG = re.compile(r'tasks-(\d{4}-\d{2}-\d{2})(?:\.\d{4,})?\.jsonl')
_WORK = re.compile(r'[0-9a-f]{32}|(?:preflight-|whole-voice-|voice-align-|subtitle-locate-)[A-Za-z0-9_-]{6,}')


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _safe(path, root):
    """Check lexical containment before resolution and every reparse ancestor."""
    if not path.is_relative_to(root):
        return False
    return not any(_linked(part) for part in (path, *path.parents))


def _root(value):
    root = Path(os.path.abspath(value))
    if any(_linked(part) for part in (root, *root.parents)):
        raise ValueError('程序数据目录包含链接，未执行缓存清理')
    return root


def _version(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_json(path):
    try:
        if path.stat().st_size > 4*1024*1024:
            return {}
        value = json.loads(path.read_text('utf8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _references(root, store, protected_paths):
    protected = {Path(os.path.abspath(p)) for p in protected_paths}
    signatures = set()
    drafts = store.drafts() if store is not None else []
    assets = store.assets() if store is not None else []

    def paths(value):
        if isinstance(value, dict):
            for item in value.values(): paths(item)
        elif isinstance(value, list):
            for item in value: paths(item)
        elif isinstance(value, str):
            try:
                if Path(value).is_absolute(): protected.add(Path(os.path.abspath(value)))
            except (OSError, ValueError):
                pass

    referenced = {identifier for draft in drafts for identifier in draft.get('asset_ids', [])}
    for asset in assets:
        # Even an original mistakenly imported from a cache directory is not ours
        # to clean. Only a draft-referenced SDR copy is additionally protected.
        paths(asset.get('path'))
        if asset.get('id') in referenced:
            paths(asset)
            signatures.add(asset.get('sha256'))
    for draft in drafts: paths(draft)
    if store is not None:
        # Finished exports are not temporary files, even if the user previously
        # chose an output directory inside work/cache. Read all output paths,
        # not the UI-limited job history or the full, potentially large results.
        with store.connection() as db:
            for row in db.execute("SELECT json_extract(data,'$.result.path') FROM jobs "
                                  "WHERE json_extract(data,'$.kind')='render'"):
                paths(row[0])
    for path in list(protected):
        if path.parent == root/'voice-cache':
            stem = path.stem
            protected.update(path.parent/(stem+suffix) for suffix in ('.wav', '.json', '.pending.json'))
        elif path.parent == root/'sdr-cache':
            protected.update(path.with_suffix(suffix) for suffix in ('.mp4', '.json'))
    # Legacy segmented voice records did not persist their cache filenames.
    # Semantic identities and local alignment filenames are irreversible hashes.
    # Keeping ambiguous references is preferable to making a paid retry likely.
    legacy_voice = any(not d.get('whole_audio') and not d.get('whole_voice') for d in drafts)
    return protected, signatures, bool(drafts), legacy_voice


def _kind(path, folder, now):
    parts = path.relative_to(folder).parts
    name = path.name
    age = now-path.stat().st_mtime
    if folder.name == 'logs':
        matched = _LOG.fullmatch(name) if len(parts) == 1 else None
        if matched and age >= LOG_DAYS*86400:
            try:
                if (date.fromtimestamp(now)-date.fromisoformat(matched[1])).days >= LOG_DAYS:
                    return 'log'
            except ValueError:
                pass
        return None
    if folder.name == 'work':
        return 'temporary' if len(parts)>1 and _WORK.fullmatch(parts[0]) and age>=TEMP_DAYS*86400 else None
    temporary = ((folder.name=='visual-cache' and
                  ((len(parts)>1 and re.fullmatch(r'vision-frames-[A-Za-z0-9_-]{6,}', parts[0]))
                   or re.fullmatch(r'vision-write-[A-Za-z0-9_-]+\.tmp', name))) or
                 (folder.name=='semantic-cache' and re.fullmatch(r'semantic-write-[A-Za-z0-9_-]+\.tmp', name)) or
                  (folder.name=='subtitle-cache' and re.fullmatch(r'[0-9a-f]{64}\.json\.[0-9a-f]{32}\.tmp', name)) or
                 (folder.name=='sdr-cache' and len(parts)>1
                  and re.fullmatch(r'sdr-staging-[A-Za-z0-9_-]{6,}', parts[0])))
    if temporary:
        return 'temporary' if age >= TEMP_DAYS*86400 else None
    if len(parts) != 1:
        return None
    patterns = {'voice-cache':rf'(?:whole-v1-)?{_HEX}\.(?:wav|json)|alignment-{_HEX}\.json',
                'visual-cache':rf'vision-{_HEX}\.json',
                'semantic-cache':rf'semantic-{_HEX}\.json',
                'subtitle-cache':rf'{_HEX}\.json',
                'sdr-cache':rf'{_HEX}-sdr-v[1-9][0-9]*\.(?:mp4|json)'}
    return 'cache' if re.fullmatch(patterns[folder.name], name) else None


def _entries(root, store, protected_paths, now):
    protected, signatures, drafts, legacy_voice = _references(root, store, protected_paths)
    entries, warnings = [], []
    for category, _ in _CATEGORIES:
        folder = root/category
        if not _safe(folder, root):
            warnings.append(category+' 包含链接，已跳过')
            continue
        if not folder.is_dir(): continue
        for current, dirs, files in os.walk(folder, followlinks=False):
            current = Path(current)
            dirs[:] = [name for name in dirs if _safe(current/name, root)]
            for name in files:
                path = current/name
                try:
                    if not _safe(path, root) or not path.is_file(): continue
                    version = _version(path)
                    kind = _kind(path, folder, now)
                    blocked = any(path==p or p in path.parents for p in protected)
                    if category=='voice-cache':
                        pending = (path.name.endswith('.pending.json') or
                                   path.with_suffix('.pending.json').exists())
                        blocked = blocked or pending or legacy_voice or (drafts and path.name.startswith('alignment-'))
                    if kind=='cache' and store is None: blocked = True
                    if category=='semantic-cache' and drafts: blocked = True
                    if category=='visual-cache' and kind=='cache':
                        identity = _read_json(path).get('identity', {})
                        signature = identity.get('sha256') if isinstance(identity, dict) else None
                        blocked = blocked or not re.fullmatch(_HEX, str(signature)) or signature in signatures
                    if category=='subtitle-cache' and kind=='cache':
                        signature=_read_json(path).get('source_sha256')
                        # Legacy entries lack provenance, but contain only local
                        # OCR results; current source references stay protected.
                        blocked=blocked or signature in signatures
                    entries.append({'path':path, 'category':category, 'bytes':version[2],
                                    'version':version, 'kind':kind, 'protected':blocked})
                except OSError:
                    warnings.append(category+' 中部分文件暂时不可读取')
    return entries, list(dict.fromkeys(warnings))


def inventory(root, *, store=None, protected_paths=(), now=None):
    """Read-only sizes; cache_eligible_* are opt-in, not default candidates."""
    root = _root(root)
    entries, warnings = _entries(root, store, protected_paths, time.time() if now is None else now)
    categories = []
    for identifier, label in _CATEGORIES:
        group = [e for e in entries if e['category']==identifier]
        default = [e for e in group if e['kind'] in {'temporary','log'} and not e['protected']]
        cache = [e for e in group if e['kind']=='cache' and not e['protected']]
        categories.append({'id':identifier, 'label':label,
            'bytes':sum(e['bytes'] for e in group), 'files':len(group),
            'eligible_bytes':sum(e['bytes'] for e in default), 'eligible_files':len(default),
            'cache_eligible_bytes':sum(e['bytes'] for e in cache), 'cache_eligible_files':len(cache),
            'protected_files':sum(e['protected'] for e in group)})
    return {'categories':categories, 'warnings':warnings,
        **{field:sum(c[field] for c in categories) for field in
           ('eligible_bytes','eligible_files','cache_eligible_bytes','cache_eligible_files','protected_files')},
        'total_bytes':sum(c['bytes'] for c in categories), 'total_files':len(entries),
        'temporary_days':TEMP_DAYS, 'log_days':LOG_DAYS,
        'cache_warning':'清理未引用缓存后，再次使用可能重新调用收费 API；请求待确认标记和现有文案引用继续保留'}


def cleanup(root, *, confirmed=False, include_caches=False, store=None,
            protected_paths=(), cancel=None, now=None):
    """Recycle only an idle, explicitly confirmed snapshot; no unlink fallback."""
    if confirmed is not True or type(include_caches) is not bool:
        raise ValueError('请先明确确认缓存清理')
    root = _root(root)
    if store is None or Path(os.path.abspath(store.path)).parent != root:
        raise ValueError('清理需要当前程序数据库，以保护素材与文案引用')
    if store.active_jobs(): raise ValueError('任务正在进行，请在任务结束后清理')
    now = time.time() if now is None else now
    entries, warnings = _entries(root, store, protected_paths, now)
    result = {'recycled_files':0, 'recycled_bytes':0, 'skipped':0,
              'failed':[], 'warnings':warnings, 'cancelled':False}
    # Move a cached media file before its validation JSON. A locked media file
    # must retain its sidecar, otherwise the surviving paid result becomes
    # unusable even though cleanup reported that file as preserved.
    entries.sort(key=lambda e: e['path'].suffix=='.json')
    for entry in entries:
        if cancel is not None and cancel.is_set():
            result['cancelled'] = True
            break
        if entry['protected'] or entry['kind'] is None or (entry['kind']=='cache' and not include_caches):
            result['skipped'] += 1
            continue
        if store.active_jobs():
            result['warnings'].append('检测到新任务，剩余清理已停止')
            break
        path = entry['path']
        try:
            if not _safe(path, root) or not path.is_file() or _version(path)!=entry['version']:
                raise ValueError('文件已改变，已保留')
            if (entry['kind']=='cache' and path.suffix=='.json'
                    and entry['category'] in {'voice-cache','sdr-cache'}
                    and any(path.with_suffix(ext).exists() for ext in ('.wav','.mp4'))):
                result['skipped'] += 1
                continue
            recycle_file(path)
            result['recycled_files'] += 1
            result['recycled_bytes'] += entry['bytes']
        except (OSError, ValueError):
            result['failed'].append({'category':entry['category'], 'name':path.name,
                                     'error':'文件被占用、已改变或回收站不可用，已保留；没有永久删除'})
    result['inventory'] = inventory(root, store=store, protected_paths=protected_paths, now=now)
    return result
