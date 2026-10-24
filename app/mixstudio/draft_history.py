"""Bounded, recoverable copy history. Never follows media/output/cache paths."""
from pathlib import Path
import json
import os
import re
import uuid

VISIBLE_LIMIT = 10
SAVED_LIMIT = 50


def _directory(root):
    root = Path(root).absolute()
    folder = root / 'draft-history'
    for part in (folder, *folder.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('文案历史目录包含链接')
    folder.mkdir(exist_ok=True)
    return folder


def _snapshot(folder, draft):
    identifier = draft.get('id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
        raise ValueError('文案标识无效')
    destination = folder / (identifier + '.json')
    if destination.is_symlink() or (hasattr(destination, 'is_junction') and destination.is_junction()):
        raise ValueError('文案历史文件包含链接')
    data = json.dumps(draft, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if destination.is_file() and destination.read_bytes() == data:
        return destination
    temporary = folder / ('.' + identifier + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
        os.replace(temporary, destination)
    finally:
        if temporary.is_file():
            temporary.unlink()
    return destination


def maintain(store, root, protected=()):
    """Caller serializes app actions. Remove a DB row only after recycling its JSON."""
    from .recycle import recycle_file
    folder = _directory(root)
    drafts = store.drafts()  # created_at descending; editing does not reset age.
    protected = set(protected)
    warnings, deferred, recycled = [], 0, 0
    for index, draft in enumerate(drafts):
        try:
            path = _snapshot(folder, draft)
            if index < SAVED_LIMIT:
                continue
            if draft['id'] in protected:
                deferred += 1
                continue
            # JSON holds the whole record, including the readable copy. Do not
            # touch paths contained in it: they can be shared or user originals.
            recycle_file(path)
            if store.remove_draft_if_current(draft):
                recycled += 1
            else:
                warnings.append('旧文案已变更，最新内容继续保留')
        except (OSError, ValueError):
            warnings.append('旧文案暂未移入回收站，数据继续保留；请检查文件占用或磁盘后重试')
    if deferred:
        warnings.append('正在处理的旧文案暂时保留，任务结束后自动清理')
    return {'visible_limit': VISIBLE_LIMIT, 'saved_limit': SAVED_LIMIT,
            'saved_count': len(store.drafts()), 'recycled_count': recycled,
            'warning': '；'.join(dict.fromkeys(warnings))}
