"""Local-only authenticated HTTP facade for the desktop webview."""
from pathlib import Path
import hmac
import os
import secrets

from flask import Flask, jsonify, request, send_file, g
from werkzeug.exceptions import HTTPException


def _decode_match_selection(data):
    """Bounded dictionary encoding; business/source checks still run in V2."""
    if 'clip_table' not in data:
        return data.get('matches')  # Existing clients and saved fixtures.
    table, rows = data.get('clip_table'), data.get('matches')
    invalid = ValueError('画面选择格式无效，请刷新文案后重新选择')
    if (set(data) != {'clip_table', 'matches'} or not isinstance(table, list)
            or len(table) > 4500 or any(not isinstance(cid, str) or not 1 <= len(cid) <= 200 for cid in table)
            or len(set(table)) != len(table) or not isinstance(rows, list) or not 1 <= len(rows) <= 64):
        raise invalid
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'segment_id', 'clip_indices'}:
            raise invalid
        sid, indices = row['segment_id'], row['clip_indices']
        if (not isinstance(sid, str) or not 1 <= len(sid) <= 80 or not isinstance(indices, list)
                or len(indices) > 4500 or any(type(i) is not int or not 0 <= i < len(table) for i in indices)
                or len(set(indices)) != len(indices)):
            raise invalid
        result.append({'segment_id': sid, 'clip_ids': [table[i] for i in indices]})
    return result


def create_app(studio, web_root=None, token=None):
    root = Path(web_root) if web_root else Path(__file__).resolve().parents[1] / "web"
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
    token = token or secrets.token_urlsafe(32)
    app.config["LOCAL_TOKEN"] = token
    app.config["STUDIO"] = studio
    # Cache a verified digest, never trust the URL key in place of verification.
    # This cache includes file identity/ChangeTime, so changed bytes invalidate it.
    from .media_identity import MediaIdentityCache
    voice_identity_cache = MediaIdentityCache(max_entries=128)

    def ok(value):
        return jsonify(ok=True, data=value)

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("请求必须是JSON对象")
        return data

    @app.before_request
    def authenticate():
        host = request.host.split(":")[0]
        if host not in {"localhost", "127.0.0.1"}:
            return jsonify(ok=False, error="只接受本机访问"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(ok=False, error="来源不匹配"), 403
        if request.path.startswith("/api/"):
            supplied = request.headers.get("X-Local-Token", "")
            if not hmac.compare_digest(supplied, token):
                return jsonify(ok=False, error="会话已失效，请重新打开程序"), 403
            if request.method not in {'GET', 'HEAD', 'OPTIONS'} and getattr(studio, 'storage_cleaning', False):
                return jsonify(ok=False, error='存储清理正在进行，请完成后再提交操作'), 409
            if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                lock = getattr(studio, 'action_lock', None)
                if lock is not None:
                    lock.acquire()
                    g.studio_action_lock = lock
                    if getattr(studio, 'storage_cleaning', False):
                        return jsonify(ok=False, error='存储清理正在进行，请完成后再提交操作'), 409
                    if request.method == 'POST' and request.path == '/api/music/scan':
                        # Scanning only reads an external folder and registers
                        # in-memory candidates under the music library's lock.
                        # Do not hold all unrelated mutations during that walk.
                        g.pop('studio_action_lock').release()
        elif request.path.startswith("/media/"):
            if not hmac.compare_digest(request.cookies.get("jdmix_session", ""), token):
                return jsonify(ok=False, error="会话无效"), 403

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.teardown_request
    def release_action_lock(error=None):
        lock = g.pop('studio_action_lock', None)
        if lock is not None:
            lock.release()

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, ValueError):
            return jsonify(ok=False, error=str(exc)[:400]), 400
        if isinstance(exc, HTTPException):
            return jsonify(ok=False, error="请求不存在或格式无效"), exc.code
        # Never return tracebacks, config, remote bodies or credentials to UI.
        return jsonify(ok=False, error="本地处理异常，请检查设置后重试"), 500

    @app.get("/")
    def index():
        html = (root / "index.html").read_text("utf-8").replace("__LOCAL_TOKEN__", token)
        response = app.make_response(html)
        response.set_cookie("jdmix_session", token, httponly=True, samesite="Strict")
        return response

    @app.get("/<name>")
    def static_asset(name):
        if name not in {"app.js", "styles.css", "brand-logo.jpg", "brand-icon.ico"}:
            return jsonify(ok=False, error="文件不存在"), 404
        return send_file(root / name)

    @app.get("/api/state")
    def state():
        if request.headers.get('X-State-Mode') == 'compact':
            from urllib.parse import unquote
            identifier = unquote(request.headers.get('X-Active-Draft', ''))
            if len(identifier) > 128:
                raise ValueError('文案编号无效')
            return ok(studio.state(compact=True, drafts_token=request.headers.get('X-Drafts-Token'),
                                   draft_id=identifier or None))
        return ok(studio.state())

    @app.get('/api/jobs/<identifier>')
    def job_detail(identifier):
        job = studio.store.job(identifier)
        if not job:
            raise ValueError('任务已不存在')
        return ok({k: v for k, v in job.items() if k != 'payload'})

    @app.get('/api/storage')
    def storage_status():
        return ok(studio.storage_status())

    @app.get('/api/music')
    def music_list():
        return ok(studio.music_state(include_tracks=True))

    @app.post('/api/music/scan')
    def music_scan():
        return ok(studio.scan_music(body().get('folder')))

    @app.post('/api/music/import')
    def music_import():
        data = body()
        return ok(studio.import_music(data.get('scan_id'), data.get('candidate_ids')))

    @app.post('/api/music/select')
    def music_select():
        data = body()
        if 'track_id' not in data:
            raise ValueError('请选择歌曲，或明确选择不加音乐')
        return ok(studio.select_music(data['track_id']))

    @app.post('/api/music/volume')
    def music_volume():
        return ok(studio.music_volume(body().get('gain')))

    @app.post('/api/music/delete')
    def music_delete():
        data = body()
        return ok(studio.delete_music(data.get('track_ids'), data.get('confirm')))

    @app.post('/api/storage/cleanup')
    def storage_cleanup():
        data = body()
        return ok(studio.clean_storage(data.get('confirmed'), data.get('include_caches', False)))

    @app.post('/api/open-logs')
    def open_logs():
        folder = studio.root / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(folder)
        return ok({'folder': str(folder)})

    @app.get("/api/settings")
    def settings_get():
        return ok(studio.settings.public())

    @app.put("/api/settings")
    def settings_put():
        return ok(studio.settings.update(body()))

    @app.post('/api/settings/reset-credentials')
    def reset_credentials():
        return ok(studio.settings.reset_credentials(body().get('confirm')))

    @app.post("/api/library/scan")
    def scan():
        return ok(studio.scan(body().get("folder")))

    @app.post('/api/library/remove')
    def remove_library_assets():
        data = body()
        return ok(studio.remove_library_assets(data.get('asset_ids'), data.get('library'),
                                               data.get('confirmed')))

    @app.post("/api/downloads")
    def downloads():
        data = body()
        return ok(studio.download(data.get("links"), data.get("folder")))

    @app.post('/api/v2/jobs/<identifier>/retry')
    def retry_whole(identifier):
        return ok(studio.retry_whole_job(identifier, body().get('allow_paid')))

    @app.post('/api/v2/preflight')
    def free_preflight():
        return ok(studio.free_preflight(body().get('asset_ids')))

    @app.post('/api/v2/drafts/<identifier>/produce')
    def produce_whole(identifier):
        return ok(studio.produce_whole(identifier, body().get('allow_paid')))

    @app.post('/api/v2/drafts/<identifier>/realign')
    def realign_whole(identifier):
        return ok(studio.realign_whole(identifier, refresh=body().get('refresh') is True))

    @app.post("/api/media/prepare")
    def prepare_media():
        return ok(studio.prepare_media(body().get("asset_ids")))

    @app.post("/api/prepare")
    def prepare():
        return ok(studio.prepare(body()))

    @app.post('/api/product/read')
    def product_read():
        return ok(studio.read_product(body().get('url')))

    @app.post('/api/v2/generate')
    def generate_v2():
        return ok(studio.generate_v2(body()))

    @app.put('/api/v2/drafts/<identifier>')
    def edit_v2(identifier):
        return ok(studio.edit_v2(identifier, body().get('voiceover')))

    @app.post('/api/v2/drafts/<identifier>/match')
    def match_v2(identifier):
        return ok(studio.match_v2(identifier, body().get('allow_paid')))

    @app.get('/api/v2/drafts/<identifier>/matching-budget')
    def matching_budget(identifier):
        return ok(studio.matching_budget(identifier, request.args.get('operation', 'match')))

    @app.put('/api/v2/drafts/<identifier>/matches')
    def replace_matches_v2(identifier):
        # Only this authenticated endpoint accepts a larger bounded body. The
        # compact form covers 64 x 4500 selections even with escaped 200-char
        # legacy IDs. All other endpoints keep their 512 KiB default limit.
        request.max_content_length = 8 * 1024 * 1024
        return ok(studio.replace_matches_v2(identifier, _decode_match_selection(body())))

    @app.put("/api/drafts/<identifier>")
    def draft(identifier):
        return ok(studio.edit_draft(identifier, body().get("segments")))

    @app.post("/api/drafts/<identifier>/render")
    def render(identifier):
        return ok(studio.render(identifier, body().get("allow_paid")))

    @app.post("/api/assets/<identifier>/transcript")
    def transcript(identifier):
        return ok(studio.set_transcript(identifier, body().get("segments")))

    @app.post("/api/jobs/<identifier>/cancel")
    def cancel(identifier):
        return ok(studio.jobs.cancel(identifier))

    @app.post("/api/jobs/<identifier>/retry")
    def retry(identifier):
        return ok(studio.retry(identifier, body().get("allow_paid") is True))

    @app.get("/media/<kind>/<identifier>")
    def media_file(kind, identifier):
        if kind in {"assets", "thumbs"}:
            path = studio.asset_media_path(identifier, thumbnail=kind == 'thumbs')
        elif kind == "voices":
            from .whole_voice import _hash
            draft = studio.store.draft(identifier)
            record = (draft or {}).get('whole_audio', {})
            path = Path(record.get('path', ''))
            requested_key = request.args.get('key')
            if (not path.is_file() or path.resolve().parent != (studio.root/'voice-cache').resolve()
                    or (requested_key is not None and requested_key != record.get('key'))
                    or voice_identity_cache.signature(path, _hash) != record.get('sha256')):
                raise ValueError('当前文案尚无有效整条音频')
        elif kind == "outputs":
            job = studio.store.job(identifier)
            if not job or job["status"] != "completed" or job["kind"] != "render":
                raise ValueError("成片尚未完成")
            path = Path(job["result"]["path"])
        else:
            raise ValueError("媒体类型无效")
        if not path.is_file():
            raise ValueError("媒体文件已移走")
        return send_file(path, conditional=True)

    @app.post("/api/open-output")
    def open_output():
        job = studio.store.job(body().get("job_id"))
        if not job or job["status"] != "completed" or job["kind"] != "render":
            raise ValueError("成片尚未完成")
        path = Path(job["result"]["path"])
        if not path.is_file():
            raise ValueError("成片文件不存在")
        if os.name == "nt":
            os.startfile(path.parent)
        return ok({"folder": str(path.parent)})
    return app
