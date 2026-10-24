"""Bounded background tasks with cooperative cancellation and persistent status."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import uuid

from .diagnostics import normalize_diagnostic


class Cancelled(Exception):
    pass


class JobManager:
    def __init__(self, store, workers=3):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mix-job")
        self.production_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mix-production")
        self.lock = threading.Lock()
        self.events = {}
        self.futures = {}
        self.closed = False
        self.on_finished = None
        store.interrupt_unfinished()

    def submit(self, kind, payload, work, *, production=False):
        with self.lock:
            if self.closed:
                raise ValueError("程序正在关闭")
            identifier = uuid.uuid4().hex
            event = threading.Event()
            self.events[identifier] = event
            job = self.store.create_job(identifier, kind, payload)
            try:
                pool = self.production_pool if production else self.pool
                self.futures[identifier] = pool.submit(self._run, identifier, event, work)
            except Exception:
                self.events.pop(identifier, None)
                self.store.update_job(identifier, status='failed', error='后台任务提交失败')
                raise
        return job

    def _run(self, identifier, event, work):
        last = [None, '', 0.]
        def progress(value, message=""):
            if event.is_set():
                raise Cancelled("已取消")
            value, now = max(0, min(.99, float(value))), time.monotonic()
            if (last[0] is not None and message == last[1] and now-last[2] < 2
                    and (value-last[0] < .01 or now-last[2] < .5)):
                return
            self.store.update_job(identifier, progress=value, message=message)
            last[:] = [value, message, now]
        try:
            # A worker cannot race ahead of submit's Future registration or
            # overwrite a queued cancellation while changing to running.
            with self.lock:
                if event.is_set():
                    raise Cancelled("已取消")
                self.store.update_job(identifier, status="running", message="处理中")
            result = work(event, progress)
            with self.lock:
                # Download asset_id is returned only after the library DB commit.
                # A late cancel cannot undo that durable side effect.
                current = self.store.job(identifier)
                imported_download = (current["kind"] == "download" and
                                     isinstance(result, dict) and bool(result.get("asset_id")))
                # Script/match handlers set this only after every durable draft
                # write succeeds. Cancellation cannot undo that commit either.
                committed_draft = (current['kind'] in {'script', 'match', 'alignment', 'music_import'} and
                                   isinstance(result, dict) and result.get('committed') is True)
                published_render = False
                if (current['kind'] == 'render' and isinstance(result, dict)
                        and result.get('published') is True and isinstance(result.get('path'), str)):
                    try:
                        output = Path(result['path'])
                        published_render = output.is_file() and output.stat().st_size > 0
                    except OSError:
                        pass
                if event.is_set() and not (imported_download or published_render or committed_draft):
                    raise Cancelled("已取消")
                self.store.update_job(identifier, status="completed", progress=1, message="处理完成", result=result)
        except Exception as exc:
            with self.lock:
                unknown = bool(getattr(exc, "result_unknown", False))
                details = {"result_unknown": unknown}
                diagnostic = normalize_diagnostic(getattr(exc, 'diagnostic', None))
                diagnostic.update(normalize_diagnostic({'code': getattr(exc, 'code', None)}))
                if diagnostic:
                    details['diagnostic'] = diagnostic
                if unknown:
                    details["error"] = str(exc)[:500]
                if event.is_set() or isinstance(exc, Cancelled):
                    self.store.update_job(identifier, status="cancelled",
                        message="本地已取消，服务端结果待确认" if unknown else "已取消", **details)
                else:
                    # Work handlers convert arbitrary exceptions into sanitized errors.
                    self.store.update_job(identifier, status="failed", message="处理失败",
                        **dict(details, error=str(exc)[:500]))
        finally:
            with self.lock:
                self.events.pop(identifier, None)
                self.futures.pop(identifier, None)
            # Maintenance is outside the job lock and cannot undo a published
            # result or turn a completed paid operation into a failed task.
            if self.on_finished:
                try:
                    self.on_finished()
                except Exception:
                    pass

    def _cancel_locked(self, identifier, event, current):
        event.set()
        future = self.futures.get(identifier)
        if current['status'] == 'queued' and future is not None and future.cancel():
            # Cancelled Futures never enter _run/finally, so release their state
            # here. No work or paid provider can have started for this Future.
            self.store.update_job(identifier, status='cancelled', message='已取消')
            self.events.pop(identifier, None)
            self.futures.pop(identifier, None)
        else:
            self.store.update_job(identifier, status='cancelling', message='正在取消')

    def cancel(self, identifier):
        with self.lock:
            event = self.events.get(identifier)
            current = self.store.job(identifier)
            if event and current and current["status"] in {"queued", "running", "cancelling"}:
                self._cancel_locked(identifier, event, current)
        job = self.store.job(identifier)
        if not job:
            raise ValueError("任务不存在")
        if self.on_finished and job['status'] == 'cancelled':
            try:
                self.on_finished()
            except Exception:
                pass
        return job

    def shutdown(self):
        with self.lock:
            self.closed = True
            for identifier, event in list(self.events.items()):
                current = self.store.job(identifier)
                if current and current['status'] in {'queued', 'running', 'cancelling'}:
                    self._cancel_locked(identifier, event, current)
        self.pool.shutdown(wait=True, cancel_futures=False)
        self.production_pool.shutdown(wait=True, cancel_futures=False)
