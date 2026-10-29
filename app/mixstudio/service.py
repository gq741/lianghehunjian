"""V2 script-first actions with compatible V1 history and rendering."""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import wave

from .jobs import Cancelled, JobManager
from .media_identity import MediaIdentityCache
from .instance import DataDirectoryLock
from .settings import Settings
from .store import Store
from .v2 import V2Actions, validate_duration
from .whole_voice import WholeVoiceActions
from .speech import (selection as speech_selection, metadata as speech_metadata,
    synthesize as synthesize_speech, require_retry_selection, require_confirmed_selection)
from .music_actions import MusicActions
from .music_library import MusicLibrary
from .output_names import reserve_output


def _cancel(event):
    if event and event.is_set():
        raise Cancelled("已取消")


@contextmanager
def _slot(semaphore, event):
    while not semaphore.acquire(timeout=.1):
        _cancel(event)
    try:
        _cancel(event)
        yield
    finally:
        semaphore.release()


def fingerprint(path, event=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            _cancel(event)
            h.update(chunk)
    return h.hexdigest()


class Studio(MusicActions, WholeVoiceActions, V2Actions):
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.instance_lock = DataDirectoryLock(self.root)
        self.instance_lock.acquire()
        try:
            for name in ["thumbs", "work", "voice-cache", "visual-cache"]:
                (self.root / name).mkdir(exist_ok=True)
            self.settings = Settings(self.root)
            self.store = Store(self.root / "state.sqlite")
            self.jobs = JobManager(self.store, workers=4)
        except Exception:
            self.instance_lock.release()
            raise
        self.asr_slot = threading.Semaphore(1)
        self.download_slot = threading.Semaphore(2)
        # Bounded striped single-flight locks: one completed voice per cache key,
        # even when multiple drafts are rendered concurrently.
        self.voice_slots = [threading.Semaphore(1) for _ in range(256)]
        self.render_condition = threading.Condition()
        self.render_active = 0
        self.action_lock = threading.RLock()
        self._timeline_response_cache = {}
        self._media_identity_cache = MediaIdentityCache()
        self.music_library = MusicLibrary(self.root)
        self.draft_history = {}
        self.store.on_draft_saved = self._maintain_draft_history
        self.jobs.on_finished = self._maintain_draft_history
        self._maintain_draft_history()

    def _maintain_draft_history(self):
        from .draft_history import maintain, VISIBLE_LIMIT, SAVED_LIMIT
        with self.action_lock:
            try:
                protected = {j.get('payload', {}).get('draft_id') for j in self.store.active_jobs()}
                self.draft_history = maintain(self.store, self.root, protected)
            except (OSError, ValueError, sqlite3.Error):
                # Saving a paid result succeeded before maintenance started.
                # Never report it as failed merely because history is locked.
                self.draft_history = {'visible_limit': VISIBLE_LIMIT, 'saved_limit': SAVED_LIMIT,
                    'warning': '文案历史清理暂未完成，原数据继续保留；请检查文件占用或磁盘后重试'}

    @staticmethod
    def _modules():
        from . import media, providers
        return media, providers

    def environment(self):
        return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
                "asr_available": importlib.util.find_spec("faster_whisper") is not None,
                "desktop_available": importlib.util.find_spec("webview") is not None,
                "download_available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                "download_route": "mobile_feed",
                "notes": ["第二版先生成脚本，再识别画面；旧版转写仍需本地模型。",
                          "画面识别按短片段抽样，不是逐帧保证；请预览确认匹配。",
                          "画面清理提供手动区域模糊/修补；AI去字幕尚未验收。",
                          "下载取决于公开链接的可用性；不保证全部链接或无水印结果。"]}

    def state(self, *, compact=False, drafts_token=None, draft_id=None):
        assets = self.store.assets()
        folder = self.store.meta("folder") or ""
        downloaded = self.store.downloaded_asset_ids()
        # Legacy records acquire source labels in the response without rewriting history.
        for asset in assets:
            asset.setdefault('libraries', ['douyin'] if asset['id'] in downloaded else ['local'])
            asset.setdefault('availability', 'available')
            asset.setdefault('selectable', asset['availability'] == 'available')
        jobs = []
        for job in self.store.jobs(limit_per_group=20 if compact else 200):
            public_job = {k: v for k, v in job.items() if k != "payload"}
            if job["kind"] in {"render", "match", "alignment"}:
                public_job["draft_id"] = job.get("payload", {}).get("draft_id")
            if compact and isinstance(public_job.get('result'), dict):
                # Full timelines remain in SQLite and the authenticated detail
                # endpoint, not in every periodic progress response.
                result = public_job['result']
                public_job['result'] = {key: result[key] for key in (
                    'duration', 'draft_id', 'draft_ids', 'needs_adjustment_ids',
                    'needs_fact_review_ids', 'asset_id', 'published', 'committed',
                    'voice_duration', 'pipeline') if key in result}
                if result.get('warnings'):
                    public_job['result']['warnings'] = [str(w)[:300] for w in result['warnings'][:8]]
                if job['kind'] == 'preflight':
                    public_job['result'].update({key: result[key] for key in
                        ('status', 'message', 'checked_at') if key in result})
                if job['kind'] == 'music_import':
                    public_job['result'].update(imported_count=len(result.get('imported', [])),
                        duplicate_count=len(result.get('duplicates', [])), failed_count=len(result.get('failed', [])))
            jobs.append(public_job)
        settings = self.settings.public()
        token = None
        if compact:
            rows = self.store.draft_snapshot(10, draft_id)
            context = [settings.get('transition'), settings.get('transition_seconds'),
                       [[a['id'], a.get('sha256'), a.get('path'), a.get('duration'), a.get('selectable')]
                        for a in assets],
                        sorted({j.get('draft_id') for j in jobs if j['kind'] in {'match','render','alignment'}
                               and j['status'] in {'queued','running','cancelling'} and j.get('draft_id')})]
            token = hashlib.sha256((json.dumps(context, ensure_ascii=False) +
                ''.join(identifier + data for identifier, data in rows)).encode()).hexdigest()
            drafts = None if token == drafts_token else [json.loads(data) for _, data in rows]
        else:
            drafts = self.store.drafts()
        for draft in drafts or []:
            if draft.get('workflow') == 'script_first_v2':
                draft['desktop_production_mode'] = 'whole_voice_v1'
                # Re-evaluate old length-only flags for display; leave saved
                # copy, historical task results and paid calls untouched.
                from .script_engine import timing_fields
                from .providers import ProviderError
                try:
                    voice = draft.get('voiceover')
                    if voice is None:
                        voice = ''.join(s['text'] for s in draft['segments'])
                    draft.update(timing_fields(voice, draft['target_seconds']))
                except (ValueError, TypeError, KeyError, ProviderError):
                    draft.update(timing_status='needs_adjustment', timing_mode='needs_adjustment',
                                 timing_message='文案或参考时长无效，请检查并保存后继续。')
            if self._legacy_timeline_refreshable(draft) and not self._draft_busy(draft['id']):
                # Response-only old-policy migration. No model calls or DB writes.
                # Cache the whole projection, so status and selected windows agree.
                import copy
                from .v2 import require_script_timing
                try:
                    require_script_timing(draft)
                    signatures = []
                    for aid in draft['asset_ids']:
                        source = self.store.asset(aid) or {}
                        signatures.append([aid, source.get('sha256'), source.get('path'),
                                           source.get('duration'), source.get('selectable')])
                    key = hashlib.sha256(json.dumps(['flexible_reference_v1', draft,
                        settings['transition'], settings['transition_seconds'], signatures],
                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    if key not in self._timeline_response_cache:
                        if len(self._timeline_response_cache)>=64:
                            self._timeline_response_cache.clear()
                        projected = copy.deepcopy(draft)
                        self._apply_timeline(projected, settings)
                        self._timeline_response_cache[key] = {field: projected[field] for field in
                            ('timeline_plan', 'matches', 'match_candidates', 'unmatched', 'match_status')}
                    draft.update(copy.deepcopy(self._timeline_response_cache[key]))
                except (ValueError,KeyError,TypeError,ProviderError):
                    draft['match_status'] = 'needs_review'
                    draft['timeline_plan'] = {'version':1,'policy':'flexible_reference_v1',
                        'status':'shortage','phase':'estimated',
                        'speed':1.,'repeat_count':0,'segments':[],'windows':[],'shortages':[],
                        'warnings':['旧稿候选已改变，请保存镜头选择后重新规划']}
        result = {"assets": assets, "jobs": jobs,
                "draft_history": dict(self.draft_history),
                "settings": settings, "environment": self.environment(), "folder": folder,
                "product": self.store.meta('current_product'),
                "preflight": self.store.meta('free_preflight'),
                "music": self.music_state(_settings=settings), "version": 2}
        if drafts is not None:
            result['drafts'] = drafts
        if compact:
            result['drafts_token'] = token
            result['compact'] = True
        return result

    def storage_status(self):
        from .storage import inventory
        return inventory(self.root, store=self.store, protected_paths=self._storage_protected_paths())

    def _storage_protected_paths(self):
        settings = self.settings.public()
        return [settings[k] for k in ('output_dir', 'download_dir', 'bgm_path') if settings.get(k)]

    def matching_budget(self, identifier, operation='match'):
        from . import script_engine
        from .providers import ProviderError
        draft = self._v2_draft(identifier)
        assets = self._v2_assets(draft['asset_ids'])
        if operation not in {'match', 'produce'}:
            raise ValueError('匹配预算操作无效')
        if (operation == 'produce' and draft.get('clips') and draft.get('match_candidates') is not None
                and draft.get('analysis_summary', {}).get('complete') is True):
            return {'exact': True, 'available_scene_count': sum(c.get('usable') is True for c in draft['clips']),
                    'new_request_count': 0, 'cached_request_count': 0,
                    'message': '本次直接复用已保存的画面匹配，不追加 GLM 或 DeepSeek 匹配调用。'}
        if not draft.get('clips') or draft.get('analysis_summary', {}).get('complete') is not True:
            return {'exact': False, 'segment_count': len(draft.get('segments', [])),
                    'planned_window_limit': len(assets) * 90,
                    'message': '画面尚未全部分析，匹配请求数将在识别后确定；只补未完成批次，成功缓存会复用。'}
        try:
            return script_engine.match_budget(draft['segments'], draft['clips'],
                model=self.settings.public()['deepseek_model'], product_title=draft['product_name'],
                cache_dir=self.root/'semantic-cache', cache_context={'draft_id': identifier,
                    'revision': draft['revision'], 'sources': [{'id':a['id'],'sha256':a['sha256']} for a in assets]})
        except ProviderError as exc:
            raise ValueError(str(exc)) from None

    def clean_storage(self, confirmed=False, include_caches=False):
        from .storage import cleanup
        if confirmed is not True or type(include_caches) is not bool:
            raise ValueError('请先确认清理范围')
        with self.action_lock:
            if self.store.active_jobs():
                raise ValueError('有任务正在处理，请完成或取消后再清理存储')
            self.storage_cleaning = True
            try:
                return cleanup(self.root, confirmed=True, include_caches=include_caches,
                               store=self.store, protected_paths=self._storage_protected_paths())
            finally:
                self.storage_cleaning = False

    @staticmethod
    def folder(value, create=False):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError("请选择有效的本地文件夹")
        path = Path(value).resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError("文件夹不存在")
        return path

    def _import_one(self, path, event, imported_at=None):
        media, _ = self._modules()
        _cancel(event)
        path = path.resolve(strict=True)
        stat = path.stat()
        signature = fingerprint(path, event)
        after_hash = path.stat()
        if (after_hash.st_size, after_hash.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError('素材读取期间发生改变，请重新读取')
        identifier = hashlib.sha256((str(path).casefold() + signature).encode()).hexdigest()[:24]
        existing = self.store.asset(identifier)
        if existing:
            _cancel(event)
            return self.store.activate_asset({'id': identifier, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
        info = media.probe(path)
        if not info.get("width") or not info.get("height") or info["duration"] <= 0:
            raise ValueError("文件没有有效视频画面")
        thumb = self.root / "thumbs" / f"{identifier}.jpg"
        warning = ""
        try:
            if not thumb.exists():
                media.thumbnail(path, thumb)
        except Exception:
            warning = "缩略图生成失败，仍可尝试预览原视频"
        asset = {"id": identifier, "name": path.name, "path": str(path),
                 "sha256": signature, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                 "duration": info["duration"], "width": info["width"], "height": info["height"], "media_info": info,
                 "imported_at": imported_at or stat.st_mtime, "thumbnail_url": f"/media/thumbs/{identifier}",
                 "preview_url": f"/media/assets/{identifier}", "transcript_status": "pending", "clips": [],
                 "error": warning}
        # Probe/thumbnail can take time; honour cancellation before the DB commit.
        _cancel(event)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError('素材读取期间发生改变，请重新读取')
        return self.store.activate_asset(asset)

    def _prepare_asset(self, asset, event=None, progress=None):
        from . import preprocess
        from .media_identity import file_version
        self._unchanged(asset, event)
        previous = asset.get("prepared_media", {})
        source_version = file_version(Path(asset['path']))
        if (previous.get("version") == preprocess.VERSION and previous.get("mode") == "original"
                and previous.get("source_sha256") == asset["sha256"]
                and previous.get('probe_version') == preprocess.PROBE_VERSION
                and source_version is not None
                and previous.get('source_version') == list(source_version)):
            return asset
        record = preprocess.prepare(asset, self.root / "sdr-cache", cancel=event, progress=progress,
                                    source_info=asset.get("media_info"))
        thumb_url = None
        if record["mode"] == "sdr":
            thumb = self.root / "thumbs" / f"{asset['id']}-sdr-v{preprocess.VERSION}.jpg"
            if not thumb.is_file():
                self._modules()[0].thumbnail(Path(record["path"]), thumb)
            thumb_url = f"/media/thumbs/{asset['id']}?prepared={record['sha256'][:12]}"
        _cancel(event)
        if previous == record and (not thumb_url or asset.get("thumbnail_url") == thumb_url):
            return asset
        return self.store.set_prepared_media(asset["id"], asset["sha256"], record, thumb_url)

    def _prepare_assets(self, identifiers, event, progress):
        assets = self._v2_assets(identifiers)
        prepared = []
        for index, asset in enumerate(assets):
            def report(value, message, i=index):
                progress((i + max(0., min(1., value))) / len(assets),
                         f"素材准备 {i+1}/{len(assets)} · {message}")
            report(0., "检查并复用本地素材")
            prepared.append(self._prepare_asset(asset, event, report))
        return prepared

    def prepare_media(self, identifiers):
        assets = self._v2_assets(identifiers)
        ids = [a["id"] for a in assets]
        with self.action_lock:
            for job in self.store.active_jobs():
                if job["kind"] == "media_prepare" and set(job["payload"].get("asset_ids", [])) == set(ids):
                    return job
            def work(event, progress):
                result = self._prepare_assets(ids, event, progress)
                return {"asset_ids": ids, "prepared": len(result),
                        "sdr_copies": sum(a.get("prepared_media", {}).get("mode") == "sdr" for a in result)}
            return self.jobs.submit("media_prepare", {"asset_ids": ids}, work)

    def asset_media_path(self, identifier, thumbnail=False):
        """Resolve a media ID only while its registered bytes still match.

        Validation happens on scan/preview, never on the frequently polled
        state endpoint. Old assets remain in SQLite for historical drafts.
        """
        asset = self.store.asset(identifier)
        if not asset:
            raise ValueError('素材不存在')
        if asset.get('selectable') is False:
            raise ValueError('素材已失效，请刷新素材库并重新选择')
        if not isinstance(asset.get('sha256'), str) or len(asset['sha256']) != 64:
            raise ValueError('素材缺少文件校验信息，请刷新素材库')
        path = Path(asset['path'])
        change = {'id': identifier, 'validation_revision': asset.get('validation_revision', 0)}
        try:
            if not path.is_file():
                self.store.reconcile_assets([{**change, 'availability': 'missing'}])
                raise ValueError('素材文件已移走，请刷新素材库')
            before = path.stat()
            signature = self._media_identity_cache.signature(path, fingerprint)
            after = path.stat()
        except OSError:
            raise ValueError('素材读取失败，请检查文件后重新读取') from None
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('素材读取期间发生改变，请重新读取')
        if signature != asset['sha256']:
            self.store.reconcile_assets([{**change, 'availability': 'replaced'}])
            raise ValueError('源素材已改变，请刷新素材库并重新选择')
        record = asset.get('prepared_media') or {}
        if record.get('mode') == 'sdr':
            from .preprocess import validated, VERSION
            prepared, _, _ = validated(record, asset['sha256'], identity_cache=self._media_identity_cache)
            resolved = self.root / 'thumbs' / f'{identifier}-sdr-v{VERSION}.jpg' if thumbnail else prepared
        else:
            resolved = self.root / 'thumbs' / f'{identifier}.jpg' if thumbnail else path
        if not resolved.is_file():
            raise ValueError('媒体文件已移走')
        return resolved.resolve()

    def scan(self, folder):
        path = self.folder(folder)
        self.store.meta("folder", str(path))
        payload = {"folder": str(path)}
        def work(event, progress):
            baseline = [a for a in self.store.assets()
                        if Path(a['path']).resolve().parent == path]
            errors, observed, failed = [], {}, set()
            dated_files = []
            for file in path.iterdir():
                _cancel(event)
                if file.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
                    continue
                try:
                    if not file.is_file():
                        continue
                    dated_files.append((file.stat().st_mtime, file))
                except OSError:
                    # Files can disappear between enumeration and sorting.
                    # Keep the per-file failure, but still import other videos.
                    failed.add(os.path.normcase(str(file.resolve())))
                    errors.append({"name": file.name, "error": "读取失败，请检查文件是否完整且包含视频"})
            files = [file for _, file in sorted(dated_files, key=lambda item: item[0], reverse=True)]
            for index, file in enumerate(files):
                _cancel(event)
                progress(index / max(1, len(files)), f"读取素材 {index + 1}/{len(files)}")
                try:
                    asset = self._import_one(file, event)
                    self._mark_library(asset, 'local')
                    observed[os.path.normcase(str(file.resolve()))] = asset['sha256']
                except Cancelled:
                    raise
                except Exception:
                    failed.add(os.path.normcase(str(file.resolve())))
                    errors.append({"name": file.name, "error": "读取失败，请检查文件是否完整且包含视频"})
            changes = []
            for asset in baseline:
                source = Path(asset['path'])
                key = os.path.normcase(str(source.resolve()))
                # Failed probing may mean corrupt replacement bytes. Recheck
                # identity rather than keeping the old card selectable or
                # treating a transient probe failure as source deletion.
                if key in failed:
                    try:
                        status = 'invalid' if fingerprint(source, event) != asset['sha256'] else None
                    except OSError:
                        status = 'invalid'
                else:
                    status = ('replaced' if key in observed and observed[key] != asset['sha256']
                              else 'missing' if not source.is_file() else None)
                if status:
                    changes.append({'id': asset['id'], 'validation_revision': asset.get('validation_revision', 0),
                                    'availability': status})
            _cancel(event)
            self.store.reconcile_assets(changes)
            return {"imported": len(observed), "errors": errors, "folder": str(path)}
        return self.jobs.submit("scan", payload, work)

    def _mark_library(self, asset, library):
        return self.store.mark_asset_library(asset['id'], library)

    def remove_library_assets(self, identifiers, library, confirmed=False):
        from .recycle import recycle_file
        if confirmed is not True:
            raise ValueError('请先确认将原视频移到回收站')
        if (not isinstance(library,str) or library not in {'local','douyin'}
                or not isinstance(identifiers,list) or not 1 <= len(identifiers) <= 10000
                or any(not isinstance(i,str) or not 1 <= len(i) <= 128 for i in identifiers)):
            raise ValueError('请选择有效的素材和素材库')
        ids = list(dict.fromkeys(identifiers))
        with self.action_lock:
            downloaded = self.store.downloaded_asset_ids()
            selected = []
            for identifier in ids:
                asset = self.store.asset(identifier)
                if not asset:
                    raise ValueError('部分素材已不存在，请刷新后重新选择')
                labels = asset.get('libraries',['douyin'] if identifier in downloaded else ['local'])
                if library not in labels:
                    raise ValueError('部分素材不属于当前素材库，请刷新后重新选择')
                selected.append(asset)
            def key(a): return os.path.normcase(os.path.abspath(a['path']))
            paths = {key(a) for a in selected}
            all_assets = self.store.assets()
            affected = [a for a in all_assets if key(a) in paths]
            affected_ids = {a['id'] for a in affected}
            for job in self.store.active_jobs():
                payload = job.get('payload',{})
                used = set(payload.get('asset_ids',[]))
                if payload.get('draft_id'):
                    used.update((self.store.draft(payload['draft_id']) or {}).get('asset_ids',[]))
                folder = payload.get('folder')
                folder_busy = job['kind'] in {'scan','download'} and folder and any(
                    Path(a['path']).parent.resolve()==Path(folder).resolve() for a in affected)
                if used & affected_ids or folder_busy:
                    raise ValueError('选中的素材正在被任务使用，请完成或取消相关任务后删除')
            # New copy-first operations recheck this flag before any paid call.
            self._recycling_asset_ids = affected_ids
            removed, failures, warnings, moved = [], [], [], 0
            try:
                groups = {}
                for asset in selected: groups.setdefault(key(asset),[]).append(asset)
                for path_key, group in groups.items():
                    asset = group[0]
                    try:
                        for entry in group:
                            self._unchanged(entry, None)
                        recycle_file(Path(asset['path']))
                    except (OSError,ValueError) as exc:
                        failures.append({'asset_ids':[a['id'] for a in group],
                                         'name':asset['name'],'error':str(exc)[:200]})
                        continue
                    moved += 1
                    linked = [a['id'] for a in affected if key(a)==path_key]
                    removed.extend(linked)
                    try:
                        self.store.mark_assets_trashed(linked)
                    except sqlite3.Error:
                        warnings.append('文件已移入回收站，列表状态保存失败，请重新读取素材库')
                return {'removed_ids':removed,'removed_count':moved,'failed':failures,
                        'warnings':warnings,'library':library,'destination':'recycle_bin'}
            finally:
                self._recycling_asset_ids = set()

    def download(self, links, folder):
        if not isinstance(links, list) or not 1 <= len(links) <= 50:
            raise ValueError("一次提供1～50个链接")
        _, providers = self._modules()
        urls = []
        for value in links:
            try:
                url = providers.normalize_douyin_url(value)
            except providers.ProviderError as exc:
                raise ValueError(str(exc)) from None
            if url not in urls:
                urls.append(url)
        path = self.folder(folder or self.settings.public()["download_dir"], create=True)
        jobs = []
        for url in urls:
            def work(event, progress, source=url):
                _, providers = self._modules()
                with _slot(self.download_slot, event):
                    result = providers.download_douyin(source, path, cancel=event, progress=progress)
                    progress(.96, "下载完成，正在加入素材库")
                    asset = self._import_one(Path(result["path"]), event, imported_at=time.time())
                    self._mark_library(asset, 'douyin')
                    return {**result, "asset_id": asset["id"]}
            jobs.append(self.jobs.submit("download", {"url": url, "folder": str(path)}, work))
        return jobs

    def set_transcript(self, identifier, segments):
        asset = self.store.asset(identifier)
        if not asset:
            raise ValueError("素材不存在")
        if not isinstance(segments, list) or not 1 <= len(segments) <= 500:
            raise ValueError("转写需包含1～500个片段")
        clips = []
        previous = 0
        for i, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise ValueError("转写片段格式无效")
            start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in [start, end]):
                raise ValueError("片段时间必须是有效秒数")
            if end > asset["duration"] + .1:
                raise ValueError("片段时间需按顺序排列、互不重叠且位于视频内")
            end = min(float(end), asset["duration"])
            if not 0 <= start < end or start < previous:
                raise ValueError("片段时间需按顺序排列、互不重叠且位于视频内")
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError("片段文字不能为空或过长")
            previous = end
            cid = hashlib.sha256(f"{identifier}:{start:.3f}:{end:.3f}:{text}".encode()).hexdigest()[:24]
            clips.append({"clip_id": cid, "asset_id": identifier, "source_path": asset["path"],
                          "start": float(start), "end": end, "text": text.strip()})
        return self.store.set_asset_transcript(identifier, clips)

    def _unchanged(self, asset, event):
        path = Path(asset["path"])
        current = self.store.asset(asset['id'])
        if (asset.get('selectable') is False or (current and current.get('selectable') is False)
                or not path.is_file() or fingerprint(path, event) != asset["sha256"]):
            raise ValueError("源素材已改变，请刷新素材库并重新选择")

    def prepare(self, payload):
        if payload.get("allow_paid") is not True:
            raise ValueError("请确认本次 DeepSeek 文案改写调用")
        identifiers = payload.get("asset_ids")
        product = payload.get("product_name", "").strip()
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 50 or len(set(identifiers)) != len(identifiers):
            raise ValueError("请选择1～50条同商品素材")
        if not product or len(product) > 200:
            raise ValueError("请填写本次商品名称，确认素材属于同一商品")
        settings = self.settings.public()
        target = payload.get("target_seconds", settings["target_seconds"])
        count = payload.get("count", settings["output_count"])
        self.settings._validate("target_seconds", target)
        self.settings._validate("output_count", count)
        if not self.settings.secret("deepseek"):
            raise ValueError("请先在设置中保存 DeepSeek API Key")
        assets = [self.store.asset(i) for i in identifiers]
        if any(a is None for a in assets):
            raise ValueError("部分素材已不存在")
        params = {"asset_ids": identifiers, "product_name": product, "target_seconds": target, "count": count}
        def work(event, progress):
            _, providers = self._modules()
            clips = []
            for index, asset in enumerate(assets):
                self._unchanged(asset, event)
                if not asset["clips"]:
                    with _slot(self.asr_slot, event):
                        asset = self.store.asset(asset["id"])
                        if not asset["clips"]:
                            progress(.05 + .5 * index / len(assets), f"转写 {index + 1}/{len(assets)}")
                            segments = providers.transcribe_video(Path(asset["path"]), settings["asr_model_path"],
                                                                  device=settings["asr_device"], cancel=event)
                            # Keep a transcript the user saved while ASR was running.
                            current_asset = self.store.asset(asset["id"])
                            asset = current_asset if current_asset["clips"] else self.set_transcript(asset["id"], segments)
                clips.extend(asset["clips"])
            progress(.65, "按源片段改写重组文案")
            drafts = providers.rewrite_clips(clips, api_key=self.settings.secret("deepseek"),
                model=settings["deepseek_model"], product_name=product, target_seconds=target,
                count=count, cancel=event)
            result = []
            for draft in drafts:
                stored = {"id": uuid.uuid4().hex, "product_name": product, "title": draft["title"],
                    "segments": draft["segments"], "clips": clips, "asset_ids": identifiers,
                    "target_seconds": target, "revision": 1, "status": "draft", "created_at": time.time(),
                    "voiceover": "".join(s["text"] for s in draft["segments"])}
                self.store.put_draft(stored)
                result.append(stored["id"])
            return {"draft_ids": result}
        return self.jobs.submit("prepare", params, work)

    def edit_draft(self, identifier, segments):
        with self.action_lock:
            draft = self.store.draft(identifier)
            if not draft:
                raise ValueError("文案不存在")
            if draft.get('workflow') == 'script_first_v2':
                raise ValueError('请使用第二版文案编辑入口')
            if any(j["kind"] == "render" and j["payload"].get("draft_id") == identifier and
                   j["status"] in {"queued", "running", "cancelling"} for j in self.store.active_jobs()):
                raise ValueError("请先取消该文案的合成任务，再编辑")
            valid = {c["clip_id"] for c in draft["clips"]}
            if not isinstance(segments, list) or not 1 <= len(segments) <= 64:
                raise ValueError("文案片段数量无效")
            clean = []
            for segment in segments:
                if not isinstance(segment, dict) or segment.get("clip_id") not in valid:
                    raise ValueError("文案引用了未选择的素材片段")
                text = segment.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 500:
                    raise ValueError("每句文案需为1～500个字符")
                clean.append({"clip_id": segment["clip_id"], "text": text.strip()})
            if len({s["clip_id"] for s in clean}) != len(clean):
                raise ValueError("同一文案中请勿重复使用同一片段")
            if sum(len(s["text"]) for s in clean) > 2000:
                raise ValueError("文案过长，请精简")
            draft.update(segments=clean, voiceover="".join(s["text"] for s in clean),
                         revision=draft["revision"] + 1, status="draft")
            return self.store.put_draft(draft)

    def _concat_voice(self, paths, destination, event):
        list_path = destination.with_suffix(".list.txt")
        list_path.write_text("\n".join("file '" + str(p).replace("\\", "/").replace("'", "'\\''") + "'" for p in paths), encoding="utf-8")
        args = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n", "-f", "concat",
                "-safe", "0", "-i", str(list_path), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(destination)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        deadline = time.monotonic() + 90
        while True:
            try:
                _, error = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                if event.is_set() or time.monotonic() > deadline:
                    process.kill()
                    process.communicate()
                    _cancel(event)
                    raise ValueError("配音合并超时")
        if process.returncode:
            raise ValueError("配音合并失败，请检查 FFmpeg 和音频格式")

    def _retime_voice(self, source, destination, speed, duration, event):
        """Pace a work copy, never rewrite the original Fish cache or video."""
        if not 1 <= speed <= 1.5 or not math.isfinite(duration) or duration <= 0:
            raise ValueError('本地配音变速参数无效')
        args = ['ffmpeg','-hide_banner','-v','error','-nostdin','-n','-i',str(source),
                '-af',f'atempo={speed:.9f},apad,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS',
                '-ar','48000','-ac','1','-c:a','pcm_s16le',str(destination)]
        flags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        process = subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=flags)
        deadline = time.monotonic()+90
        while True:
            try:
                process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                if event.is_set() or time.monotonic()>deadline:
                    process.kill();process.communicate();_cancel(event)
                    raise ValueError('本地配音变速超时；原始配音缓存保留')
        if process.returncode:
            raise ValueError('本地配音变速失败；原始配音缓存保留')

    @staticmethod
    def _align_voice_samples(path, expected_seconds, target_seconds, segment_count, event):
        """Correct only accumulated resampling round-off, never a real shortage.

        The concat output is our own 48kHz mono PCM work copy. Raw Fish cache is
        untouched. At most one sample per sentence plus two endpoint samples may
        be padded/trimmed; the final sample target stays inside the user bounds.
        """
        from .v2 import duration_bounds
        low, high = duration_bounds(target_seconds)
        _cancel(event)
        with wave.open(str(path), 'rb') as source:
            rate, channels, width = source.getframerate(), source.getnchannels(), source.getsampwidth()
            actual_frames = source.getnframes()
            if (rate, channels, width) != (48000, 1, 2):
                raise ValueError('合并配音格式异常；原始配音缓存保留')
            if (not math.isfinite(expected_seconds) or
                    not low - 1/rate <= expected_seconds <= high + 1/rate):
                raise ValueError('排片配音目标超出有效成片时长范围（0.1～60秒）')
            desired = min(math.floor(high*rate), max(math.ceil(low*rate), round(expected_seconds*rate)))
            if abs(actual_frames-desired) > segment_count + 2:
                raise ValueError('合并配音时长与排片计划不一致；原始配音缓存保留')
            if actual_frames == desired:
                return desired/rate
            pcm = source.readframes(actual_frames)
            if len(pcm) != actual_frames*width:
                raise ValueError('合并配音数据不完整；原始配音缓存保留')
        _cancel(event)
        aligned = path.with_suffix('.aligned.wav')
        try:
            with wave.open(str(aligned), 'wb') as output:
                output.setnchannels(channels)
                output.setsampwidth(width)
                output.setframerate(rate)
                output.writeframes(pcm[:desired*width] + b'\0' * max(0, (desired-actual_frames)*width))
            _cancel(event)
            os.replace(aligned, path)
        finally:
            aligned.unlink(missing_ok=True)
        return desired/rate

    @staticmethod
    def _voice_key(text, voice_id, *, settings=None):
        if settings is None or settings.get('voice_provider', 'fish') == 'fish':
            # The historical two-argument Fish cache identity remains exact.
            data = {"text":text, "voice":voice_id, "provider":"fish",
                    "model":"s2.1-pro", "format":"wav"}
        else:
            chosen = speech_selection(settings)
            from . import providers
            data = {"text":text, "voice":chosen['voice_id'], "provider":chosen['provider'],
                    "model":chosen['model'], "format":"wav", "endpoint":providers.MINIMAX_URL,
                    "sample_rate":32000, "language_boost":"Chinese"}
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def render(self, identifier, allow_paid, *, retry_unknown=False, retry_payload=None, expected_voice=None):
        if allow_paid is not True:
            raise ValueError("请确认本次配音调用")
        with self.action_lock:
            draft = self.store.draft(identifier)
            if not draft:
                raise ValueError("文案不存在")
            # One authority for whole-voice drafts, including authenticated old
            # clients. A true legacy draft retains its segmented render contract.
            if (draft.get('production_mode') == 'whole_voice_v1' or
                    draft.get('whole_audio') or draft.get('whole_voice')):
                return self.produce_whole(identifier, allow_paid, retry_unknown=retry_unknown,
                    **({"retry_payload":retry_payload} if retry_payload is not None else {}),
                    **({"expected_voice":expected_voice} if expected_voice is not None else {}))
            v2 = draft.get('workflow') == 'script_first_v2'
            if v2:
                if self._draft_busy(identifier):
                    raise ValueError('该文案已有匹配或合成任务')
                draft = {**draft, 'segments': self.bound_v2(draft)}
            segments = draft.get("segments", [])
            known = {c["clip_id"] for c in draft.get("clips", [])}
            if (not 1 <= len(segments) <= 64 or
                any(s.get("clip_id") not in known for s in segments) or
                (not v2 and len({s.get("clip_id") for s in segments}) != len(segments))):
                raise ValueError("文案需包含1～64个不重复的已绑定片段，请重新生成或编辑")
            if any(j["kind"] == "render" and j["payload"].get("draft_id") == identifier and
                   j["status"] in {"queued", "running", "cancelling"} for j in self.store.active_jobs()):
                raise ValueError("该文案已有进行中的合成任务")
            settings = self.settings.public()
            require_confirmed_selection(expected_voice, settings)
            if retry_payload is not None:
                require_retry_selection(retry_payload, settings)
            voice_binding = speech_metadata(settings)
            chosen = speech_selection(settings)
            if not self.settings.secret(chosen['provider']):
                raise ValueError(f"请先在设置中保存 {chosen['label']} API Key")
            output_dir = self.folder(settings["output_dir"], create=True)
            retry_markers = {}
            if retry_unknown:
                for segment in draft["segments"]:
                    key = self._voice_key(segment["text"], settings["voice_id"], settings=settings)
                    marker = self.root / "voice-cache" / f"{key}.pending.json"
                    if marker.exists():
                        try:
                            retry_markers[key] = json.loads(marker.read_text("utf-8"))["attempt"]
                        except (ValueError, KeyError):
                            raise ValueError("配音提交记录损坏，请先检查运行数据") from None
            def work(event, progress):
                media, providers = self._modules()
                acquired = False
                with self.render_condition:
                    while self.render_active >= settings["render_concurrency"]:
                        _cancel(event)
                        self.render_condition.wait(.2)
                    self.render_active += 1
                    acquired = True
                work_dir = None
                try:
                    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                        raise ValueError("请先安装 FFmpeg/ffprobe 并加入 PATH，再生成配音")
                    if settings["bgm_path"]:
                        self.require_music(settings)
                        bgm = Path(settings["bgm_path"])
                        if not bgm.is_file() or not media.probe(bgm).get("has_audio"):
                            raise ValueError("背景音乐文件不存在或没有有效音轨")
                    for asset_id in draft["asset_ids"]:
                        asset = self.store.asset(asset_id)
                        if not asset:
                            raise ValueError("源素材不存在")
                        self._unchanged(asset, event)
                    if v2:
                        self._prepare_assets(draft['asset_ids'], event, lambda v, m: progress(.04*v, m))
                        draft["timeline_plan"] = self.preflight_v2(draft, settings)
                    work_dir = self.root / "work" / uuid.uuid4().hex
                    work_dir.mkdir()
                    index = {c["clip_id"]: c for c in draft["clips"]}
                    used_ids = {s["clip_id"] for s in draft["segments"]}
                    assigned = set(used_ids)
                    audio_paths, captions, render_clips, raw_seconds = [], [], [], []
                    current = 0.0
                    for i, segment in enumerate(draft["segments"]):
                        _cancel(event)
                        progress(.05 + .45 * i / len(draft["segments"]), f"生成配音 {i + 1}/{len(draft['segments'])}")
                        key = self._voice_key(segment["text"], settings["voice_id"], settings=settings)
                        audio = self.root / "voice-cache" / f"{key}.wav"
                        with _slot(self.voice_slots[int(key[:2], 16)], event):
                            if not audio.exists():
                                marker = audio.with_suffix(".pending.json")
                                if marker.exists():
                                    previous = json.loads(marker.read_text("utf-8"))
                                    if retry_markers.get(key) != previous.get("attempt"):
                                        raise providers.ProviderError(
                                            "相同配音的上次请求结果待确认；请核对服务端后点击任务重试，可能再次计费。",
                                            code="voice_result_unknown", result_unknown=True)
                                temporary = work_dir / f"voice-{i}.wav"
                                attempt = {"attempt": uuid.uuid4().hex, "state": "pending"}
                                marker.write_text(json.dumps(attempt), encoding="utf-8")
                                try:
                                    synthesize_speech(segment["text"], temporary, settings=settings,
                                        api_key=self.settings.secret(chosen["provider"]), cancel=event, adapters=providers)
                                    info = media.probe(temporary)
                                    if not .05 <= info["duration"] <= 300:
                                        raise ValueError("配音时长异常")
                                    os.replace(temporary, audio)
                                    marker.unlink()
                                except Exception:
                                    # Conservative durable fence also survives a process crash.
                                    marker.write_text(json.dumps({**attempt, "state": "unknown"}), encoding="utf-8")
                                    raise
                            duration = media.probe(audio)["duration"]
                        if not .05 <= duration <= 300:
                            raise ValueError("配音时长异常")
                        captions.append({"start": current, "end": current + duration, "text": segment["text"]})
                        current += duration
                        audio_paths.append(audio)
                        raw_seconds.append(duration)
                        if v2:
                            continue
                        primary = dict(index[segment["clip_id"]])
                        primary["text"] = segment["text"]
                        primary["additional_clips"] = []
                        capacity = primary["end"] - primary["start"]
                        # V1 compatibility only. V2 continues above and always
                        # uses the common measured timeline below instead.
                        candidates = sorted([c for c in draft["clips"] if c["clip_id"] not in assigned and
                            c["asset_id"] == primary["asset_id"]], key=lambda c: abs(c["start"] - primary["end"]))
                        for extra in candidates:
                            if capacity >= duration + .6:
                                break
                            assigned.add(extra["clip_id"])
                            primary["additional_clips"].append({"source_path": extra["source_path"],
                                                                "start": extra["start"], "end": extra["end"]})
                            capacity += extra["end"] - extra["start"] - settings["transition_seconds"]
                        render_clips.append(primary)
                    if v2:
                        from .timeline import shortage_message
                        plan = self._apply_timeline(draft,settings,voice_seconds=raw_seconds,phase='actual')
                        if plan['status']!='ready':
                            self.store.put_draft(draft)
                            raise ValueError(shortage_message(plan)+' 原始配音已缓存，未自动重试收费。')
                        paced, captions = [], []
                        current = 0.
                        for i,(audio,duration,segment) in enumerate(zip(audio_paths,raw_seconds,draft['segments'])):
                            _cancel(event)
                            if plan['speed']>1.+1e-8:
                                destination = work_dir / f'paced-{i}.wav'
                                expected = duration/plan['speed']
                                self._retime_voice(audio,destination,plan['speed'],expected,event)
                                measured = media.probe(destination)['duration']
                                if abs(measured-expected)>.002:
                                    raise ValueError('本地配音时长未与排片计划对齐；原始配音缓存保留')
                                paced.append(destination)
                            else:
                                measured=duration
                                paced.append(audio)
                            captions.append({'start':current,'end':current+measured,'text':segment['text']})
                            current+=measured
                        audio_paths=paced
                        # The renderer receives every selected original window,
                        # not an arbitrary fixed +.6 second prefix of its options.
                        scenes,_ = self._timeline_inputs(draft)
                        originals = {c['clip_id']:c for c in scenes}
                        for row,segment in zip(plan['segments'],draft['segments']):
                            ids=list(dict.fromkeys(row['clip_ids']))
                            primary={**originals[ids[0]],'text':segment['text']}
                            primary['additional_clips']=[dict(originals[cid]) for cid in ids[1:]]
                            render_clips.append(primary)
                    combined = work_dir / 'voice.wav'
                    self._concat_voice(audio_paths, combined, event)
                    if v2:
                        aligned_seconds = self._align_voice_samples(
                            combined, plan['total_seconds'], draft['target_seconds'], len(audio_paths), event)
                        captions[-1]['end'] = aligned_seconds
                    actual_duration = media.probe(combined)['duration']
                    if v2:
                        validate_duration(draft['target_seconds'],actual_duration)
                        if abs(actual_duration-plan['total_seconds'])>.05:
                            raise ValueError('合并配音时长与排片计划不一致；原始配音缓存保留')
                    final_settings = {**settings, 'subtitle_segments':captions, 'duration_seconds':actual_duration}
                    if v2:
                        final_settings['timeline_plan']=plan
                        final_settings['min_output_seconds']=.1
                        final_settings['max_output_seconds']=60
                    product_name = (draft.get('product') or {}).get('title') or draft.get('product_name', '')
                    with reserve_output(output_dir, product_name) as output:
                        result = media.render_video(render_clips, combined, output, final_settings, cancel=event,
                            progress=lambda v, m="": progress(.55 + .44 * v, m or "正在合成"))
                    result["draft_id"] = identifier
                    result["voice_duration"] = actual_duration
                    if v2:
                        result["timeline_plan"] = plan
                        result["voice_speed"] = plan["speed"]
                    result.setdefault("warnings", [])
                    if abs(actual_duration - draft["target_seconds"]) > draft["target_seconds"] * .2:
                        result["warnings"].append(f"配音实际约{actual_duration:.1f}秒，与目标{draft['target_seconds']}秒有差异；未截断旁白")
                    try:
                        self.store.put_draft({**draft, "status": "rendered"})
                    except sqlite3.Error:
                        # render_video has already validated and atomically
                        # published this output. A secondary metadata failure
                        # must not discard its path or encourage paid retries.
                        if (result.get('published') is not True
                                or not isinstance(result.get('path'), str)
                                or not Path(result['path']).is_file()
                                or Path(result['path']).stat().st_size <= 0):
                            raise
                        result['warnings'].append('成片已生成；草稿状态更新暂未完成，可在成品记录中查看。')
                        result['draft_status_saved'] = False
                    return result
                finally:
                    try:
                        if work_dir is not None:
                            # Only this job's immediate work child is disposable.
                            # Resolve before recursive cleanup; never follow a link
                            # into source media, persistent caches or outputs.
                            try:
                                work_root = (self.root / 'work').resolve()
                                resolved_work = work_dir.resolve()
                                if (resolved_work.parent == work_root and not work_dir.is_symlink()
                                        and not (hasattr(work_dir, 'is_junction') and work_dir.is_junction())):
                                    shutil.rmtree(work_dir, ignore_errors=True)
                            except (OSError, RuntimeError):
                                # A locked directory is retained for later local
                                # inspection; cleanup must not mask the job result.
                                pass
                    finally:
                        # Always release admission even when filesystem cleanup
                        # fails or the task was cancelled.
                        if acquired:
                            with self.render_condition:
                                self.render_active -= 1
                                self.render_condition.notify_all()
            return self.jobs.submit("render", {"draft_id": identifier, "revision": draft["revision"],
                                               "voice":voice_binding}, work)

    def retry(self, identifier, allow_paid=False):
        job = self.store.job(identifier)
        if not job or job["status"] not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("仅失败、取消或中断的任务可以手动重试")
        p = job["payload"]
        if job['kind'] == 'music_import':
            return self.import_music(p['scan_id'], p['candidate_ids'])
        if job['kind'] == 'preflight':
            return self.free_preflight(p['asset_ids'])
        if job['kind'] == 'media_prepare':
            return self.prepare_media(p['asset_ids'])
        if job['kind'] == 'alignment':
            return self.realign_whole(p['draft_id'], refresh=True)
        if job['kind'] == 'product':
            return self.read_product(p['url'])
        if job['kind'] == 'script':
            # Prevent a retry from silently switching to a different product.
            current_product = self.store.meta('current_product')
            if not current_product or current_product.get('sku') != p['product']['sku']:
                raise ValueError('当前商品已改变，请重新读取原商品或新建文案任务')
            return self.generate_v2({**p, 'allow_paid': allow_paid})
        if job['kind'] == 'match':
            return self.match_v2(p['draft_id'], allow_paid)
        if job["kind"] == "scan":
            return self.scan(p["folder"])
        if job["kind"] == "download":
            return self.download([p["url"]], p["folder"])[0]
        if job["kind"] == "prepare":
            return self.prepare({**p, "allow_paid": allow_paid})
        if job["kind"] == "render":
            require_retry_selection(p, self.settings.public())
            if p.get("pipeline") == "whole_voice_v1":
                current = self.store.draft(p['draft_id'])
                if not current or current['revision'] != p['revision']:
                    raise ValueError('文案或画面选择已修改，请确认当前版本后重新生成；旧缓存保留')
                return self.produce_whole(p["draft_id"], allow_paid, retry_unknown=True, retry_payload=p)
            return self.render(p["draft_id"], allow_paid, retry_unknown=True, retry_payload=p)
        raise ValueError("任务类型不支持重试")

    def close(self):
        try:
            self.jobs.shutdown()
        finally:
            self.instance_lock.release()
