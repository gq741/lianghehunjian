"""Folder edition entrypoint. Uses the existing app without business changes."""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys


class StartupError(ValueError):
    """Only fixed, locally authored messages may be shown by this launcher."""


def _helpers():
    path = Path(__file__).resolve().with_name('portable_check.py')
    spec = importlib.util.spec_from_file_location('_lianghe_portable_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_settings(root, data_dir):
    """Only migrate a missing/invalid local model preference. Never copy keys."""
    helpers = _helpers()
    model = root / 'models' / 'faster-whisper-small'
    if not helpers.model_ready(model):
        raise StartupError('随包语音识别模型缺失，请完整复制软件文件夹后重试。')
    from mixstudio.settings import Settings
    settings = Settings(data_dir)
    previous = settings.values.get('asr_model_path', '')
    if previous and helpers.model_ready(previous):
        return 'preserved_existing_model'
    if settings._recovery['needed']:
        # Unknown/corrupt user preferences must be reviewed through the app's
        # existing recovery UI, not normalized by a distribution bootstrap.
        return 'preserved_preferences_needing_review'
    settings.update({'asr_model_path': str(model.resolve()), 'asr_device': 'cpu'})
    return 'configured_bundled_model'


def _notice(message, headless):
    if os.name == 'nt' and not headless:
        ctypes.windll.user32.MessageBoxW(None, message, '量禾混剪', 0x10)
    elif sys.stderr:
        print(message, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description='量禾混剪文件夹版启动器')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--ready-file', type=Path)
    args = parser.parse_args(argv)
    helpers = _helpers()
    root = helpers.configure_environment(helpers.package_root())
    logger = logging.getLogger('lianghe.portable')
    try:
        if not helpers.supported_platform():
            raise StartupError('此版本需要 Windows 10 22H2（19045）/11 x64；请使用兼容电脑。')
        # Explicit app path is needed because the launcher is invoked with -I.
        sys.path.insert(0, str(root / 'app'))
        logs = root / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(logs / 'portable-start.log', maxBytes=256 * 1024,
                                      backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        dependencies = helpers.dependency_versions(root)
        if not dependencies['ok']:
            raise StartupError('随包 Python 依赖不完整或版本不符，请运行“免费自检”查看结果。')
        if not all((root / 'bin' / name).is_file() for name in ('ffmpeg.exe', 'ffprobe.exe')):
            raise StartupError('随包 FFmpeg 文件缺失，请完整复制 bin 文件夹。')
        if not args.headless:
            components = helpers.windows_components()
            if not components['dotnet']:
                raise StartupError('本机缺少 .NET Framework 4.6.2 或更高版本，请按说明书安装系统组件。')
            if not components['webview2']:
                raise StartupError('本机缺少 WebView2，请运行包内“安装系统组件.ps1”或 installers 中的离线安装器，再启动软件。')
        data_dir = (args.data_dir or helpers.default_data_dir()).resolve()
        # Preserve environment compatibility for workers and future subprocesses.
        os.environ['JDMIX_DATA_DIR'] = str(data_dir)
        outcome = prepare_settings(root, data_dir)
        logger.info('START model_preference=%s headless=%s', outcome, args.headless)
        from mixstudio.desktop import main as desktop_main
        desktop_args = ['--port', str(args.port), '--data-dir', str(data_dir)]
        if args.headless:
            desktop_args.append('--headless')
        if args.ready_file:
            desktop_args.extend(['--ready-file', str(args.ready_file.resolve())])
        desktop_main(desktop_args)
        logger.info('STOP clean_exit')
        return 0
    except Exception as error:
        # Raw exception/tracebacks can contain user documents or API strings.
        logger.error('START_FAILED type=%s', type(error).__name__)
        message = (str(error) if isinstance(error, StartupError) else
                   '启动失败，请运行包内免费自检；可把 logs 中自检报告交给 WorkBuddy 检查。')
        _notice(message, args.headless)
        return 1
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


if __name__ == '__main__':
    raise SystemExit(main())
