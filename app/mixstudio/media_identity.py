"""Bounded single-flight reuse of hashes for an unchanged filesystem version.

Windows st_ctime is creation time, not a change counter. FileBasicInfo supplies
ChangeTime in addition to file identity, length and write time. If the driver
cannot supply that information, callers retain full-hash validation per request.
"""
from collections import OrderedDict
import os
from pathlib import Path
import threading


if os.name == 'nt':
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [('CreationTime', ctypes.c_longlong),
                    ('LastAccessTime', ctypes.c_longlong),
                    ('LastWriteTime', ctypes.c_longlong),
                    ('ChangeTime', ctypes.c_longlong),
                    ('FileAttributes', wintypes.DWORD)]

    _query_info = ctypes.WinDLL('kernel32', use_last_error=True).GetFileInformationByHandleEx
    _query_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _query_info.restype = wintypes.BOOL


def file_version(path):
    """Return a cheap change-sensitive identity, or None to disable reuse.

    Microsoft contract: FileBasicInfo (0), FILE_BASIC_INFO including ChangeTime:
    https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex
    https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_basic_info
    """
    try:
        with Path(path).open('rb') as source:
            info = os.fstat(source.fileno())
            if not info.st_ino:
                return None
            if os.name == 'nt':
                basic = _FileBasicInfo()
                handle = msvcrt.get_osfhandle(source.fileno())
                if not _query_info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                    return None
                if basic.ChangeTime <= 0:
                    return None
                changed = basic.ChangeTime
            else:
                changed = info.st_ctime_ns
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed)
    except (OSError, ValueError):
        return None


class MediaIdentityCache:
    """One hash per file version; no paths or digests persist beyond this run."""
    def __init__(self, max_entries=512):
        self.max_entries = max(1, int(max_entries))
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._slots = [threading.Lock() for _ in range(64)]

    def signature(self, path, compute):
        key = os.path.normcase(os.path.abspath(path))
        # Fixed stripes bound lock memory independently of the number of files.
        with self._slots[hash(key) % len(self._slots)]:
            before = file_version(path)
            with self._lock:
                cached = self._entries.get(key)
                if before is not None and cached is not None and cached[0] == before:
                    self._entries.move_to_end(key)
                    return cached[1]
                self._entries.pop(key, None)
            signature = compute(path)
            after = file_version(path)
            if before is not None and after is not None:
                if before != after:
                    raise ValueError('素材读取期间发生改变，请重新读取')
                with self._lock:
                    self._entries[key] = (after, signature)
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
            return signature
