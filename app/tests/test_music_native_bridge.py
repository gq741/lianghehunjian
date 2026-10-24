"""Isolated native bridge tests. Never open the real system clipboard."""
import ctypes
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mixstudio import desktop


class NativeFunction:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result(*args) if callable(self.result) else self.result


class FakeClipboard:
    def __init__(self, text='https://v.douyin.com/示例/\r\n第二行😀', *, raw=None):
        self.raw = text.encode('utf-16-le')+b'\0\0' if raw is None else raw
        self.buffer = ctypes.create_string_buffer(self.raw)
        self.user32 = SimpleNamespace(OpenClipboard=NativeFunction(1), CloseClipboard=NativeFunction(1),
            IsClipboardFormatAvailable=NativeFunction(1), GetClipboardData=NativeFunction(123))
        self.kernel32 = SimpleNamespace(GlobalSize=NativeFunction(len(self.raw)),
            GlobalLock=NativeFunction(ctypes.addressof(self.buffer)), GlobalUnlock=NativeFunction(0))

    def read(self):
        with patch.object(ctypes, 'WinDLL', side_effect=AssertionError('real clipboard API forbidden'), create=True):
            return desktop._read_clipboard_windows(user32=self.user32, kernel32=self.kernel32)


class MusicNativeBridgeTests(unittest.TestCase):
    def test_construction_never_reads_clipboard(self):
        with patch.object(desktop, '_read_clipboard_windows', create=True) as read:
            desktop.DialogBridge()
        read.assert_not_called()

    def test_choose_music_folder_delegates_and_does_not_import(self):
        bridge = desktop.DialogBridge()
        with patch.object(bridge, 'choose_folder', return_value='E:/owned-music') as choose:
            self.assertEqual(bridge.choose_music_folder(), 'E:/owned-music')
        choose.assert_called_once_with()

    def test_cancel_music_folder_returns_none(self):
        bridge = desktop.DialogBridge()
        with patch.object(bridge, 'choose_folder', return_value=None):
            self.assertIsNone(bridge.choose_music_folder())

    def test_existing_folder_and_bgm_interfaces_stay_compatible(self):
        bridge = desktop.DialogBridge()
        bridge._window = SimpleNamespace(create_file_dialog=Mock(side_effect=[['E:/folder'], ['E:/song.mp3'], None]))
        fake = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER='folder', OPEN='open'))
        with patch.dict(sys.modules, {'webview': fake}):
            self.assertEqual(bridge.choose_folder(), 'E:/folder')
            self.assertEqual(bridge.choose_bgm(), 'E:/song.mp3')
            self.assertIsNone(bridge.choose_bgm())
        calls = bridge._window.create_file_dialog.call_args_list
        self.assertEqual(calls[0].args, ('folder',))
        self.assertEqual(calls[1].args, ('open',))
        self.assertFalse(calls[1].kwargs['allow_multiple'])

    def test_explicit_read_calls_native_helper_only_when_invoked(self):
        bridge = desktop.DialogBridge()
        with patch.object(desktop, '_read_clipboard_windows', return_value='用户刚复制的链接', create=True) as read:
            self.assertEqual(bridge.read_clipboard(), '用户刚复制的链接')
        read.assert_called_once_with()

    def test_unicode_newlines_and_non_bmp_text_are_preserved(self):
        fake = FakeClipboard()
        self.assertEqual(fake.read(), 'https://v.douyin.com/示例/\r\n第二行😀')
        self.assertEqual(fake.user32.GetClipboardData.calls, [(13,)])
        self.assertEqual(fake.kernel32.GlobalUnlock.calls, [(123,)])
        self.assertEqual(fake.user32.CloseClipboard.calls, [()])

    def test_empty_text_returns_empty_string(self):
        fake = FakeClipboard(text='')
        self.assertEqual(fake.read(), '')
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_non_text_clipboard_returns_empty_without_requesting_a_handle(self):
        fake = FakeClipboard()
        fake.user32.IsClipboardFormatAvailable.result = 0
        self.assertEqual(fake.read(), '')
        self.assertEqual(fake.user32.GetClipboardData.calls, [])
        self.assertEqual(fake.kernel32.GlobalLock.calls, [])
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_busy_clipboard_has_clear_error_and_is_not_closed_by_non_owner(self):
        fake = FakeClipboard()
        fake.user32.OpenClipboard.result = 0
        with self.assertRaisesRegex(ValueError, '占用'):
            fake.read()
        self.assertEqual(fake.user32.GetClipboardData.calls, [])
        self.assertEqual(fake.user32.CloseClipboard.calls, [])

    def test_missing_handle_closes_clipboard(self):
        fake = FakeClipboard()
        fake.user32.GetClipboardData.result = 0
        with self.assertRaisesRegex(ValueError, '读取'):
            fake.read()
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_oversize_is_rejected_before_lock_or_memory_read(self):
        fake = FakeClipboard()
        fake.kernel32.GlobalSize.result = 1024*1024+2
        with self.assertRaisesRegex(ValueError, '过大'):
            fake.read()
        self.assertEqual(fake.kernel32.GlobalLock.calls, [])
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_zero_or_one_byte_allocation_is_not_valid_unicode_text(self):
        for size in (0, 1):
            fake = FakeClipboard()
            fake.kernel32.GlobalSize.result = size
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, '格式'):
                fake.read()
            self.assertEqual(fake.kernel32.GlobalLock.calls, [])
            self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_lock_failure_closes_without_unlocking_null_address(self):
        fake = FakeClipboard()
        fake.kernel32.GlobalLock.result = 0
        with self.assertRaisesRegex(ValueError, '读取'):
            fake.read()
        self.assertEqual(fake.kernel32.GlobalUnlock.calls, [])
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_missing_null_terminator_is_rejected_and_resources_released(self):
        fake = FakeClipboard(raw='AB'.encode('utf-16-le'))
        with self.assertRaisesRegex(ValueError, '格式'):
            fake.read()
        self.assertEqual(fake.kernel32.GlobalUnlock.calls, [(123,)])
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_invalid_utf16_has_no_content_in_error(self):
        fake = FakeClipboard(raw=b'\0\xd8\0\0')
        with self.assertRaisesRegex(ValueError, '格式') as caught:
            fake.read()
        self.assertNotIn('surrogate', str(caught.exception))
        self.assertEqual(fake.kernel32.GlobalUnlock.calls, [(123,)])
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_allocated_padding_after_nul_is_not_decoded_or_returned(self):
        fake = FakeClipboard(raw='链接'.encode('utf-16-le')+b'\0\0\0\xd8\xff')
        self.assertEqual(fake.read(), '链接')
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_native_pointer_signatures_do_not_truncate_on_64_bit(self):
        fake = FakeClipboard()
        fake.read()
        self.assertIs(fake.user32.GetClipboardData.restype, ctypes.c_void_p)
        self.assertIs(fake.kernel32.GlobalLock.restype, ctypes.c_void_p)
        self.assertIs(fake.kernel32.GlobalSize.restype, ctypes.c_size_t)

    def test_memory_read_error_closes_and_unlocks_without_exposing_details(self):
        fake = FakeClipboard()
        with patch.object(ctypes, 'string_at', side_effect=OSError('private fake memory details')):
            with self.assertRaisesRegex(ValueError, '读取') as caught:
                fake.read()
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(fake.kernel32.GlobalUnlock.calls, [(123,)])
        self.assertEqual(len(fake.user32.CloseClipboard.calls), 1)

    def test_non_windows_fallback_does_not_load_a_native_library(self):
        with patch.object(desktop.os, 'name', 'posix'), \
             patch.object(ctypes, 'WinDLL', side_effect=AssertionError('native loading forbidden'), create=True):
            with self.assertRaisesRegex(ValueError, 'Ctrl\\+V'):
                desktop._read_clipboard_windows()


if __name__ == '__main__':
    unittest.main()
