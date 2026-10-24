"""Offline diagnostics for the folder distribution. Never calls model APIs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time


MODEL_FILES = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')
WEBVIEW_ID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
LOCK_LINE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)$')
SYNTHETIC_HDR_SOURCE = ('testsrc2=size=128x128:rate=10:duration=0.5,'
    'format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:'
    'colorspace=bt2020nc:range=limited')
SYNTHETIC_FILTER = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
    'tonemap=tonemap=mobius:desat=2,zscale=t=bt709:m=bt709:r=limited,'
    'format=yuv420p,ass=check.ass:fontsdir=fonts')


def package_root():
    return Path(__file__).resolve().parents[2]


def configure_environment(root):
    """Changes this process and its children only; no registry/global PATH edits."""
    root = Path(root).resolve()
    old = os.environ.get('PATH', '')
    os.environ['PATH'] = os.pathsep.join((str(root / 'bin'), str(root / 'runtime'), old))
    # The package contains every local inference asset. Do not fetch silently.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['PYTHONNET_RUNTIME'] = 'netfx'
    os.environ['JDMIX_FONT_FILE'] = str(root / 'fonts' / 'NotoSansCJKsc-Regular.otf')
    return root


def model_ready(path):
    try:
        return all((Path(path) / name).is_file() and (Path(path) / name).stat().st_size > 0
                   for name in MODEL_FILES)
    except (OSError, TypeError, ValueError):
        return False


def supported_platform():
    if (os.name != 'nt' or struct.calcsize('P') != 8
            or platform.machine().lower() not in ('amd64', 'x86_64')):
        return False
    version = sys.getwindowsversion()
    return (version.major, version.build) >= (10, 19045)


def windows_components():
    """Read the registry only. Native assembly loading is tested separately."""
    result = {'dotnet': False, 'webview2': False}
    if os.name != 'nt':
        return result
    import winreg
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for view in views:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                    r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full',
                    0, winreg.KEY_READ | view) as key:
                release = winreg.QueryValueEx(key, 'Release')[0]
                # Same minimum used by the bundled pywebview Windows backend.
                result['dotnet'] |= isinstance(release, int) and release >= 394802
        except OSError:
            pass
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW_ID}',
                        0, winreg.KEY_READ | view) as key:
                    value = winreg.QueryValueEx(key, 'pv')[0]
                    parts = str(value).split('.')
                    result['webview2'] |= (all(p.isdigit() for p in parts)
                                          and tuple(map(int, parts)) >= (86, 0, 622, 0))
            except OSError:
                pass
    return result


def default_data_dir():
    explicit = os.environ.get('JDMIX_DATA_DIR')
    return Path(explicit) if explicit else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JDMixStudio' / 'v1'


def _run(command, *, timeout, marker=None):
    started = time.monotonic()
    record = {'timeout_seconds': timeout}
    try:
        completed = subprocess.run([os.fspath(x) for x in command], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            check=False)
        record.update(exit_status=completed.returncode, timed_out=False)
        output = completed.stdout.decode('utf-8', errors='replace')
        record['ok'] = completed.returncode == 0 and (marker is None or marker in output)
        # Do not retain untrusted stderr, environment values, or tracebacks.
        return record, output
    except subprocess.TimeoutExpired:
        record.update(exit_status=None, timed_out=True, ok=False)
        return record, ''
    except OSError as error:
        record.update(exit_status=None, timed_out=False, ok=False, error_type=type(error).__name__)
        return record, ''
    finally:
        record['elapsed_ms'] = round((time.monotonic() - started) * 1000)


def _writable(path):
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path) as stream:
            stream.write(b'LIANGHE_PORTABLE_WRITE_CHECK')
            stream.flush()
            os.fsync(stream.fileno())
        return True
    except OSError:
        return False


def dependency_versions(root):
    """Match the release lock, not the sender's mutable machine environment."""
    result = {'ok': False, 'mismatches': [], 'checked': 0}
    try:
        lines = (root / 'requirements-lock.txt').read_text('utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        result['reason'] = 'lock_missing_or_invalid'
        return result
    seen = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = LOCK_LINE.fullmatch(line)
        if not match:
            result['reason'] = 'lock_line_invalid'
            return result
        name, expected = match.groups()
        normalized = re.sub(r'[-_.]+', '-', name).lower()
        if normalized in seen:
            result['reason'] = 'lock_duplicate'
            return result
        seen.add(normalized)
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            result['mismatches'].append({'package': name, 'expected': expected, 'actual': actual})
        result['checked'] += 1
    required = {'flask', 'pywebview', 'yt-dlp', 'faster-whisper', 'av',
                'msgpack', 'rapidocr-onnxruntime', 'onnxruntime', 'pythonnet'}
    if not required.issubset(seen):
        result['reason'] = 'lock_missing_required_packages'
        return result
    result['ok'] = not result['mismatches']
    return result


def run_checks(root, data_dir):
    root = configure_environment(root)
    checks = []

    def add(name, ok, message, **details):
        checks.append({'name': name, 'ok': bool(ok), 'message': message, **details})

    supported = supported_platform()
    add('platform', supported, '需要 Windows 10 22H2（19045）/11 x64 与 64 位 Python',
        system=platform.system(), machine=platform.machine(), python_bits=struct.calcsize('P') * 8)
    add('python', sys.version_info[:3] == (3, 12, 14), '发行版 Python 3.12.14',
        version=platform.python_version())
    add('runtime_origin', Path(sys.prefix).resolve() == (root / 'runtime').resolve()
        and Path(sys.base_prefix).resolve() == (root / 'runtime').resolve(),
        '使用随包独立 Python，不依赖本机 Python / 虚拟环境')
    add('layout', all((root / p).is_file() for p in ('app/app.py',
        'runtime/python.exe', 'runtime/pythonw.exe', 'bin/ffmpeg.exe', 'bin/ffprobe.exe',
        'app/mixstudio/voice_alignment_worker.py', 'app/mixstudio/subtitle_worker.py')),
        '程序、运行时及工作进程文件完整')
    deps = dependency_versions(root)
    add('dependency_lock', deps.pop('ok'), '核对发行版锁定依赖', **deps)
    add('logs_writable', _writable(root / 'logs'), '包内 logs 文件夹可写')
    add('data_writable', _writable(data_dir), '当前用户数据文件夹可写；未读取或改写历史和密钥')
    model = root / 'models' / 'faster-whisper-small'
    add('asr_model_files', model_ready(model), '本地识别模型文件完整')
    add('subtitle_font', (root / 'fonts' / 'NotoSansCJKsc-Regular.otf').is_file(),
        '随包开源中文字体，不依赖主机安装微软雅黑')
    components = windows_components()
    installer = root / 'installers' / 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
    add('dotnet', components['dotnet'], '需要 .NET Framework 4.6.2 或更高版本')
    add('webview2', components['webview2'],
        '缺少时请运行包内“安装系统组件.ps1”或 installers 中 WebView2 离线安装器',
        bundled_installer_present=installer.is_file())
    python = Path(sys.executable)
    # Independent processes catch native DLL / CPU instruction failures without
    # taking down the checker; every process has a fixed timeout.
    native_code = ("import flask,webview,yt_dlp,msgpack,av,numpy,cv2,onnxruntime,ctranslate2;"
                   "from rapidocr_onnxruntime import RapidOCR;"
                   "from faster_whisper import WhisperModel;print('NATIVE_IMPORTS_READY')")
    native, _ = _run([python, '-I', '-B', '-X', 'utf8', '-c', native_code],
                     timeout=30, marker='NATIVE_IMPORTS_READY')
    add('native_imports', native.pop('ok'), '核心 Python / 原生 DLL 加载', **native)
    for name, worker, arguments, marker in (
            ('asr_probe', 'voice_alignment_worker.py', ['--model', model, '--device', 'cpu'], 'VOICE_MODEL_READY'),
            ('ocr_probe', 'subtitle_worker.py', [], 'SUBTITLE_OCR_READY')):
        result, _ = _run([python, '-I', '-B', '-X', 'utf8', root / 'app' / 'mixstudio' / worker,
                          *arguments, '--probe'], timeout=45 if name == 'asr_probe' else 30, marker=marker)
        add(name, result.pop('ok'), '离线模型工作进程可加载；不调用收费接口', **result)
    if supported and components['dotnet'] and components['webview2']:
        code = ("from webview.platforms import edgechromium;"
                "from Microsoft.Web.WebView2.Core import CoreWebView2Environment;"
                "assert CoreWebView2Environment.GetAvailableBrowserVersionString();"
                "print('WEBVIEW_NATIVE_READY')")
        result, _ = _run([python, '-I', '-B', '-X', 'utf8', '-c', code],
                         timeout=15, marker='WEBVIEW_NATIVE_READY')
        add('webview_native', result.pop('ok'), '.NET / WebView2 原生桥接加载', **result)
    else:
        add('webview_native', False, '系统组件缺失或平台不兼容，跳过原生桥接加载')
    ffmpeg = root / 'bin' / 'ffmpeg.exe'
    filters, output = _run([ffmpeg, '-hide_banner', '-filters'], timeout=10)
    names = {line.split()[1] for line in output.splitlines() if len(line.split()) >= 2}
    needed = {'ass', 'subtitles', 'zscale', 'tonemap', 'xfade', 'atempo', 'aresample', 'amix'}
    add('ffmpeg_filters', filters.pop('ok') and needed.issubset(names),
        '字幕 / HDR / 转场 / 音频滤镜', missing=sorted(needed - names), **filters)
    encoders, output = _run([ffmpeg, '-hide_banner', '-encoders'], timeout=10)
    names = {line.split()[1] for line in output.splitlines() if len(line.split()) >= 2}
    add('ffmpeg_encoders', encoders.pop('ok') and {'libx264', 'aac'}.issubset(names),
        'CPU H.264 与 AAC 编码器', **encoders)
    probe, _ = _run([root / 'bin' / 'ffprobe.exe', '-v', 'error', '-version'], timeout=5)
    add('ffprobe', probe.pop('ok'), '媒体信息读取程序可运行', **probe)
    # A real synthetic HDR-to-SDR + audio + subtitle encode exercises the same
    # dependency chain without any personal footage or network use.
    if _writable(root / 'logs'):
        try:
            with tempfile.TemporaryDirectory(prefix='portable-check-', dir=root / 'logs') as temp:
                temp = Path(temp)
                fonts = temp / 'fonts'
                fonts.mkdir()
                shutil.copyfile(root / 'fonts' / 'NotoSansCJKsc-Regular.otf',
                                fonts / 'NotoSansCJKsc-Regular.otf')
                subtitle = temp / 'check.ass'
                subtitle.write_text('[Script Info]\nScriptType: v4.00+\nPlayResX: 128\nPlayResY: 128\n'
                    '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
                    'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
                    'Alignment, MarginL, MarginR, MarginV, Encoding\n'
                    'Style: Default,Noto Sans CJK SC,14,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,4,4,4,1\n'
                    '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                    'Dialogue: 0,0:00:00.00,0:00:00.40,Default,,0,0,0,,量禾自检\n', encoding='utf-8')
                # Filter filename is fixed and relative: no drive/quote escaping.
                command = [ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-n',
                    '-f', 'lavfi', '-i', SYNTHETIC_HDR_SOURCE,
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000:duration=0.5',
                    '-vf', SYNTHETIC_FILTER, '-af', 'atempo=1.0,aresample=44100', '-c:v', 'libx264',
                    '-threads', '1', '-filter_threads', '1', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-t', '0.5', temp / 'check.mp4']
                # The subprocess needs a private cwd only for fixed ASS filename.
                started = time.monotonic()
                try:
                    encoded = subprocess.run([os.fspath(x) for x in command], cwd=temp,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    ok = encoded.returncode == 0 and (temp / 'check.mp4').stat().st_size > 0
                    add('synthetic_encode', ok, '实际 HDR 转 SDR、无底框字幕与音频编码测试',
                        exit_status=encoded.returncode, elapsed_ms=round((time.monotonic() - started) * 1000))
                except subprocess.TimeoutExpired:
                    add('synthetic_encode', False, '合成编码测试超时', timed_out=True)
        except OSError as error:
            add('synthetic_encode', False, '合成编码测试文件操作失败', error_type=type(error).__name__)
    else:
        add('synthetic_encode', False, '日志目录不可写，跳过合成编码测试')
    return {'schema': 1, 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
            'ok': all(check['ok'] for check in checks), 'paid_api_calls': 0,
            'network_downloads': 0, 'checks': checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description='量禾混剪免费离线环境自检（不调用模型接口）')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--package-root', type=Path, default=package_root())
    args = parser.parse_args(argv)
    report = run_checks(args.package_root, args.data_dir or default_data_dir())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, args.output)
    for check in report['checks']:
        print(('PASS ' if check['ok'] else 'FAIL ') + check['name'] + ': ' + check['message'])
    print('PORTABLE_CHECK_OK' if report['ok'] else 'PORTABLE_CHECK_FAILED')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
