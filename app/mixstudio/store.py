"""Small SQLite persistence layer; each operation owns a short-lived connection."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.on_draft_saved = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_recent_group ON jobs(
                    (json_extract(data,'$.kind') IN ('scan','download')),
                    json_extract(data,'$.created_at') DESC);
                CREATE INDEX IF NOT EXISTS jobs_active_state ON jobs(json_extract(data,'$.status'));
                CREATE INDEX IF NOT EXISTS drafts_recent ON drafts(json_extract(data,'$.created_at') DESC);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _put(self, table, identifier, data):
        with self.connection() as db:
            db.execute(f"INSERT INTO {table}(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (identifier, json.dumps(data, ensure_ascii=False, allow_nan=False)))
        return data

    def _get(self, table, identifier):
        with self.connection() as db:
            row = db.execute(f"SELECT data FROM {table} WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def _all(self, table):
        with self.connection() as db:
            rows = db.execute(f"SELECT data FROM {table}").fetchall()
        return [json.loads(row[0]) for row in rows]

    def put_asset(self, asset):
        return self._put("assets", asset["id"], asset)

    def activate_asset(self, asset):
        """Record a fresh byte-identity validation without losing history."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM assets WHERE id=?', (asset['id'],)).fetchone()
            current = json.loads(row[0]) if row else {}
            data = dict(current) if current else dict(asset)
            if current:
                # Import snapshots must not replace a transcript or library
                # labels saved while hashing/probing the same source bytes.
                for key in ('name', 'path', 'sha256', 'size', 'mtime_ns', 'duration',
                            'width', 'height', 'thumbnail_url', 'preview_url', 'error', 'media_info'):
                    if key in asset:
                        data[key] = asset[key]
            revision = current.get('validation_revision', 0)
            data.update(availability='available', selectable=True,
                        validation_revision=(revision if type(revision) is int else 0) + 1)
            db.execute('INSERT INTO assets(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (data['id'], json.dumps(data, ensure_ascii=False, allow_nan=False)))
        return data

    def set_prepared_media(self, identifier, signature, record, thumbnail_url=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM assets WHERE id=?", (identifier,)).fetchone()
            data = json.loads(row[0]) if row else None
            if not data or data.get("sha256") != signature or data.get("selectable") is False:
                raise ValueError("素材登记已改变，请重新选择")
            data["prepared_media"] = record
            if thumbnail_url:
                data["thumbnail_url"] = thumbnail_url
            db.execute("UPDATE assets SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), identifier))
            return data

    def mark_asset_library(self, identifier, library):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM assets WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('素材不存在')
            data = json.loads(row[0])
            legacy_download = db.execute("SELECT 1 FROM jobs WHERE json_extract(data,'$.kind')='download' "
                "AND json_extract(data,'$.result.asset_id')=? LIMIT 1", (identifier,)).fetchone()
            labels = set(data.get('libraries', ['douyin'] if legacy_download else []))
            labels.add(library)
            data['libraries'] = sorted(labels)
            db.execute('UPDATE assets SET data=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False, allow_nan=False), identifier))
        return data

    def set_asset_transcript(self, identifier, clips):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM assets WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('素材不存在')
            data = json.loads(row[0])
            data.update(clips=clips, transcript_status='ready')
            db.execute('UPDATE assets SET data=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False, allow_nan=False), identifier))
        return data

    def reconcile_assets(self, changes):
        """Apply one scan's stale records atomically, conditional on validation.

        A download/reimport which verified an ID after the scan snapshot wins;
        failed/cancelled scans do not supply speculative changes here.
        """
        changed = []
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for change in changes:
                if change.get('availability') not in {'missing', 'replaced', 'invalid'}:
                    raise ValueError('素材状态无效')
                row = db.execute('SELECT data FROM assets WHERE id=?', (change['id'],)).fetchone()
                if not row:
                    continue
                data = json.loads(row[0])
                if data.get('validation_revision', 0) != change.get('validation_revision', 0):
                    continue
                data.update(availability=change['availability'], selectable=False)
                db.execute('UPDATE assets SET data=? WHERE id=?',
                           (json.dumps(data, ensure_ascii=False, allow_nan=False), change['id']))
                changed.append(change['id'])
        return changed

    def asset(self, identifier):
        return self._get("assets", identifier)

    def assets(self):
        return sorted(self._all("assets"), key=lambda a: a.get("imported_at", 0), reverse=True)

    def mark_assets_trashed(self, identifiers):
        """Keep historical IDs but retire every reference to the recycled path."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for identifier in identifiers:
                row = db.execute('SELECT data FROM assets WHERE id=?', (identifier,)).fetchone()
                if row:
                    data = json.loads(row[0])
                    revision = data.get('validation_revision', 0)
                    data.update(availability='trashed', selectable=False,
                                validation_revision=(revision if type(revision) is int else 0)+1)
                    db.execute('UPDATE assets SET data=? WHERE id=?',
                               (json.dumps(data, ensure_ascii=False, allow_nan=False),identifier))


    def put_draft(self, draft):
        result = self._put("drafts", draft["id"], draft)
        if self.on_draft_saved:
            self.on_draft_saved()
        return result

    def remove_draft_if_current(self, draft):
        """Called only after a recoverable copy has entered the Recycle Bin."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM drafts WHERE id=?', (draft['id'],)).fetchone()
            if not row or json.loads(row[0]) != draft:
                return False
            db.execute('DELETE FROM drafts WHERE id=?', (draft['id'],))
        return True

    def draft(self, identifier):
        return self._get("drafts", identifier)

    def drafts(self):
        return sorted(self._all("drafts"), key=lambda d: d.get("created_at", 0), reverse=True)

    def draft_snapshot(self, limit=10, include_id=None):
        """Return bounded raw rows so unchanged polling need not decode large clips."""
        with self.connection() as db:
            rows = db.execute("SELECT id,data FROM drafts ORDER BY json_extract(data,'$.created_at') DESC LIMIT ?",
                              (limit,)).fetchall()
            if include_id and include_id not in {r[0] for r in rows}:
                row = db.execute('SELECT id,data FROM drafts WHERE id=?', (include_id,)).fetchone()
                if row:
                    rows.append(row)
        return rows

    def meta(self, name, value=None):
        if value is not None:
            return self._put("meta", name, value)
        return self._get("meta", name)

    def create_job(self, identifier, kind, payload):
        result = self._put("jobs", identifier, {"id": identifier, "kind": kind, "payload": payload,
            "status": "queued", "progress": 0, "message": "等待处理", "error": "",
            "created_at": time.time(), "result": None})
        from .tasklog import record
        record(self.path.parent, result)
        return result

    def job(self, identifier):
        return self._get("jobs", identifier)

    def jobs(self, limit_per_group=200):
        # Independent UI areas must retain their own recent history even when
        # hundreds of downloads are newer than the last generation job.
        with self.connection() as db:
            rows = {}
            for imported in (0, 1):
                for identifier, data in db.execute("SELECT id,data FROM jobs WHERE "
                        "(json_extract(data,'$.kind') IN ('scan','download','music_import'))=? "
                        "ORDER BY json_extract(data,'$.created_at') DESC LIMIT ?",
                        (imported, limit_per_group)):
                    rows[identifier] = data
            # Active jobs never disappear behind a display limit.
            for identifier, data in db.execute("SELECT id,data FROM jobs WHERE "
                    "json_extract(data,'$.status') IN ('queued','running','cancelling')"):
                rows[identifier] = data
        return sorted((json.loads(data) for data in rows.values()),
                      key=lambda job: job['created_at'], reverse=True)

    def active_jobs(self):
        # Display pagination must never weaken concurrency/paid-call guards.
        with self.connection() as db:
            rows = db.execute("SELECT data FROM jobs WHERE json_extract(data,'$.status') IN ('queued','running','cancelling')").fetchall()
        return [json.loads(row[0]) for row in rows]

    def downloaded_asset_ids(self):
        with self.connection() as db:
            rows = db.execute("SELECT json_extract(data,'$.result.asset_id') FROM jobs WHERE json_extract(data,'$.kind')='download'").fetchall()
        return {row[0] for row in rows if row[0]}

    def update_job(self, identifier, **patch):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("任务不存在")
            data = json.loads(row[0])
            data.update(patch)
            db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), identifier))
        from .tasklog import record
        record(self.path.parent, data)
        return data

    def interrupt_unfinished(self):
        for job in self._all("jobs"):
            if job["status"] in {"queued", "running", "cancelling"}:
                self.update_job(job["id"], status="interrupted", error="上次程序退出时任务未完成；请检查结果后手动重试",
                                message="等待手动处理")
