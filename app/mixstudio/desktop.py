"""Native desktop entry point. No external browser required for normal use."""
import argparse
import ctypes
import json
import logging
import os
import threading
from pathlib import Path

from werkzeug.serving import make_server

from .http_app import create_app
from .service import Studio
from .settings import data_root


MAX_CLIPBOARD_BYTES = 1024 * 1024


def _read_clipboard_windows(*, user32=None, kernel32=None):
    """Read Unicode text only on explicit bridge invocation; never retain it.

    Libraries are loaded lazily. Injectable APIs let tests use owned memory
    without opening, replacing or otherwise inspecting the system clipboard.
    """
    if user32 is None or kernel32 is None:
        if os.name != 'nt':
            raise ValueError('请在输入框按Ctrl+V粘贴链接')
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_int
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    if not user32.OpenClipboard(None):
        raise ValueError('剪贴板正被其他程序占用，请稍后再点粘贴')
    try:
        if not user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
            return ''
        handle = user32.GetClipboardData(13)
        if not handle:
            raise ValueError('剪贴板文本读取失败，请重新复制链接后重试')
        size = kernel32.GlobalSize(handle)
        if size < 2:
            raise ValueError('剪贴板文本格式无效，请重新复制链接后重试')
        if size > MAX_CLIPBOARD_BYTES:
            raise ValueError('剪贴板文本过大，请只复制需要解析的商品链接（最多1MiB）')
        address = kernel32.GlobalLock(handle)
        if not address:
            raise ValueError('剪贴板文本读取失败，请重新复制链接后重试')
        try:
            raw = ctypes.string_at(address, size)
            # Find a UTF-16 *code-unit-aligned* terminator before decoding.
            # Allocator padding after the terminator is not clipboard text.
            end = next((i for i in range(0, len(raw)-1, 2) if raw[i:i+2] == b'\0\0'), None)
            if end is None:
                raise ValueError('剪贴板文本格式无效，请重新复制链接后重试')
            try:
                return raw[:end].decode('utf-16-le')
            except UnicodeDecodeError:
                raise ValueError('剪贴板文本格式无效，请重新复制链接后重试') from None
        finally:
            kernel32.GlobalUnlock(handle)
    except OSError:
        raise ValueError('剪贴板文本读取失败，请重新复制链接后重试') from None
    finally:
        user32.CloseClipboard()


class DialogBridge:
    def __init__(self):
        self._window = None

    def choose_folder(self):
        import webview
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return str(result[0]) if result else None

    def choose_music_folder(self):
        return self.choose_folder()

    def read_clipboard(self):
        return _read_clipboard_windows()

    def choose_bgm(self):
        import webview
        result = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
            file_types=("音频文件 (*.mp3;*.wav;*.m4a;*.flac)", "所有文件 (*.*)"))
        return str(result[0]) if result else None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="local HTTP verification mode")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args(argv)
    studio = Studio(args.data_dir or data_root())
    app = create_app(studio)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    address = f"http://127.0.0.1:{server.server_port}"
    if args.ready_file:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(json.dumps({"url": address, "data_dir": str(studio.root)}), encoding="utf-8")
    print("量禾混剪 " + address, flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if args.headless:
            thread.join()
        else:
            import webview
            from .product import JDWindowReader
            product_reader = JDWindowReader()
            studio.product_reader = product_reader
            bridge = DialogBridge()
            bridge._window = webview.create_window("量禾混剪", address,
                js_api=bridge, width=1380, height=900, min_size=(1024, 700),
                background_color="#f5f7f8", text_select=True)
            bridge._window.events.closed += product_reader.close
            webview.start(gui="edgechromium", debug=False, private_mode=False,
                          storage_path=str(studio.root / 'browser-v2'),
                          icon=str(Path(__file__).resolve().parent.parent / 'web' / 'brand-icon.ico'))
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        studio.close()


if __name__ == "__main__":
    main()
