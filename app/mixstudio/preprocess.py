"""Local HDR -> SDR preparation; original bytes and source clock stay authoritative."""
from pathlib import Path
import hashlib
import json
import math
import os
import tempfile
import threading

from . import media
from .media_identity import file_version

VERSION = 1
PROBE_VERSION = 2
_LOCKS = [threading.Lock() for _ in range(64)]
_CPU = threading.Semaphore(1)


def digest(path, cancel=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            media._cancelled(cancel)
            h.update(block)
    return h.hexdigest()


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _acquire(lock, cancel):
    while not lock.acquire(timeout=.1):
        media._cancelled(cancel)


def _origin(info):
    return info.get('video_start_time', info.get('format_start_time', 0.)) - info.get('format_start_time', 0.)


def validated(record, source_sha256, cancel=None, identity_cache=None):
    """Read only a complete, source-bound cache; reject substituted/truncated files."""
    try:
        if (not isinstance(record, dict) or record.get('version') != VERSION
                or record.get('mode') != 'sdr' or record.get('source_sha256') != source_sha256):
            raise ValueError
        path = Path(record['path'])
        manifest = path.with_suffix('.json')
        if (path.name != source_sha256 + f'-sdr-v{VERSION}.mp4'
                or _linked(path) or _linked(manifest) or _linked(path.parent)
                or not path.is_file() or not manifest.is_file() or manifest.stat().st_size > 32768):
            raise ValueError
        stored = json.loads(manifest.read_text('utf-8'))
        before = file_version(path)
        signature = (identity_cache.signature(path, lambda p: digest(p, cancel))
                     if identity_cache is not None else digest(path, cancel))
        offset = record['time_offset']
        if (stored != record or signature != record['sha256'] or type(offset) not in (int, float)
                or not math.isfinite(offset) or abs(offset) > 86400):
            raise ValueError
        info = media.probe(path)
        if before is not None and file_version(path) != before:
            raise ValueError
        if info['hdr'] or info.get('color_transfer') != 'bt709' or info.get('pix_fmt') != 'yuv420p':
            raise ValueError
        return path, info, float(record['time_offset'])
    except media.MediaCancelled:
        raise
    except (OSError, ValueError, KeyError, TypeError, media.MediaError):
        raise media.MediaError('素材准备缓存缺失或已改变，请重新点击下一步准备素材；尚未提交收费请求') from None


def prepare(asset, directory, *, cancel=None, progress=None, source_info=None):
    """Return a descriptor. SDR passes through; HDR is converted once per source hash."""
    source = media._path(asset['path'])
    signature = asset.get('sha256')
    if not isinstance(signature, str) or len(signature) != 64 or any(c not in '0123456789abcdef' for c in signature):
        raise media.MediaError('素材缺少有效原视频指纹，请重新导入')
    before = file_version(source)
    if digest(source, cancel) != signature:
        raise media.MediaError('原视频已改变，请重新导入')
    # Imported metadata can predate HDR/probe fixes. Only a fresh probe decides
    # passthrough; the service may reuse a version-bound original descriptor.
    info = media.probe(source)
    media._cancelled(cancel)
    if not info.get('hdr'):
        if before is not None and file_version(source)!=before:
            raise media.MediaError('准备期间原视频已改变，未发布缓存')
        return {'version': VERSION, 'mode': 'original', 'source_sha256': signature, 'time_offset': 0.,
                'probe_version':PROBE_VERSION,'source_version':list(before) if before is not None else None}
    if info.get('dolby_vision_profile') == 5:
        raise media.MediaError('该素材为杜比视界 Profile 5，需要专用色彩处理；本次未调用视觉或配音接口')
    root = Path(directory)
    if _linked(root):
        raise media.MediaError('素材准备缓存目录不可为链接')
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    output = root / (signature + f'-sdr-v{VERSION}.mp4')
    manifest = output.with_suffix('.json')
    lock = _LOCKS[int(signature[:2], 16) % len(_LOCKS)]
    _acquire(lock, cancel)
    try:
        if _linked(output) or _linked(manifest):
            raise media.MediaError('素材准备缓存文件不可为链接')
        if output.is_file() and manifest.is_file():
            try:
                if manifest.stat().st_size > 32768:
                    raise ValueError
                record = json.loads(manifest.read_text('utf-8'))
                validated(record, signature, cancel)
                if progress:
                    progress(1., '已复用准备好的 SDR 素材')
                return record
            except media.MediaCancelled:
                raise
            except (OSError, ValueError, TypeError, media.MediaError):
                pass  # Regenerate only owned local output; never rerun a paid request.
        _acquire(_CPU, cancel)
        try:
            with tempfile.TemporaryDirectory(prefix='sdr-staging-', dir=root) as name:
                staged = Path(name) / 'video.mp4'
                # Respect valid source metadata; supply BT.2020 only if absent.
                defaults = []
                if info.get('color_primaries') in (None, 'unknown', 'unspecified'):
                    defaults.append('pin=bt2020')
                if info.get('color_space') in (None, 'unknown', 'unspecified'):
                    defaults.append('min=bt2020nc')
                linear = 'zscale=t=linear:npl=100' + (':' + ':'.join(defaults) if defaults else '')
                vf = ','.join([linear, 'format=gbrpf32le', 'zscale=p=bt709',
                    'tonemap=tonemap=mobius:desat=2', 'zscale=t=bt709:m=bt709:r=limited',
                    'format=yuv420p',
                    'sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA',
                    'sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL',
                    'sidedata=mode=delete:type=DOVI_RPU_BUFFER',
                    'sidedata=mode=delete:type=DOVI_METADATA'])
                media._run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-n',
                    '-copyts', '-start_at_zero', '-i', source, '-map', '0:v:0', '-map', '0:a:0?',
                    '-vf', vf, '-fps_mode', 'passthrough', '-c:v', 'libx264', '-preset', 'fast',
                    '-crf', '18', '-threads', '2', '-filter_threads', '1', '-bf', '0',
                    '-c:a', 'aac', '-b:a', '128k', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                    '-colorspace', 'bt709', '-color_range', 'tv', '-metadata:s:v:0', 'rotate=0',
                    '-movflags', '+faststart', '-progress', 'pipe:1', staged],
                    cancel=cancel, timeout=max(300., info['duration'] * 15), duration=info['duration'],
                    progress=(lambda value: progress(value, '正在将 HDR 准备为普通 SDR 素材')) if progress else None)
                converted = media.probe(staged)
                tolerance = max(.08, 2 / max(1., info.get('fps', 25)))
                if (converted['hdr'] or converted.get('color_transfer') != 'bt709'
                        or converted.get('pix_fmt') != 'yuv420p'
                        or converted['width'] != info['width'] or converted['height'] != info['height']
                        or abs(converted['video_duration'] - info['video_duration']) > tolerance
                        or converted['has_audio'] != info['has_audio']):
                    raise media.MediaError('SDR 素材准备校验未通过，原视频保持不变')
                media._run(['ffmpeg', '-hide_banner', '-v', 'error', '-xerror', '-nostdin',
                            '-i', staged, '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'],
                           cancel=cancel, timeout=max(120., info['duration'] * 5))
                if digest(source, cancel) != signature or (before is not None and file_version(source) != before):
                    raise media.MediaError('准备期间原视频已改变，未发布缓存')
                record = {'version': VERSION, 'mode': 'sdr', 'source_sha256': signature,
                    'path': str(output), 'sha256': digest(staged, cancel),
                    'time_offset': _origin(info) - _origin(converted)}
                staged_json = Path(name) / 'video.json'
                staged_json.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
                media._cancelled(cancel)
                os.replace(staged, output)
                os.replace(staged_json, manifest)
                return record
        finally:
            _CPU.release()
    finally:
        lock.release()
