"""Windows Recycle Bin only. No unlink/rmtree/permanent-delete fallback."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import os
import sys
import uuid


RECYCLE_FLAGS = 0x00080000 | 0x20000000 | 0x00100000 | 0x0400 | 0x0010 | 0x0004


def recycle_file(value):
    if os.name != 'nt' or tuple(sys.getwindowsversion()[:2]) < (6, 2):
        raise ValueError('此删除功能需要Windows 8及以上版本的回收站')
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError('原视频文件已不存在，请刷新素材库')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('素材路径包含链接，请重新导入真实文件后删除')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    if kernel.GetDriveTypeW(path.anchor) != 3:
        raise ValueError('当前磁盘不属于本地固定磁盘，未执行删除；请在资源管理器中处理')

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]
        def __init__(self, value):
            super().__init__()
            ctypes.memmove(ctypes.byref(self), uuid.UUID(value).bytes_le, 16)

    ole = ctypes.WinDLL('ole32')
    shell = ctypes.WinDLL('shell32')
    hr = ctypes.c_long
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = hr
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = hr
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell.SHCreateItemFromParsingName.restype = hr

    def check(result):
        if result < 0:
            raise OSError(f'Windows回收站操作失败（0x{result & 0xffffffff:08X}），未执行永久删除')

    def invoke(ptr, slot, *types):
        table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(hr, ctypes.c_void_p, *types)(table[slot])

    initialized = ole.CoInitializeEx(None, 2)
    # An existing COM apartment is valid; only uninitialize our own increment.
    if initialized < 0 and initialized & 0xffffffff != 0x80010106:
        check(initialized)
    operation, item = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        check(ole.CoCreateInstance(ctypes.byref(GUID('3ad05575-8857-4850-9277-11b85bdb8e09')),
            None, 1, ctypes.byref(GUID('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')), ctypes.byref(operation)))
        check(invoke(operation, 5, wintypes.DWORD)(operation, RECYCLE_FLAGS))
        check(shell.SHCreateItemFromParsingName(str(path), None,
            ctypes.byref(GUID('43826d1e-e718-42ee-bc55-a1e261c37bfe')), ctypes.byref(item)))
        check(invoke(operation, 18, ctypes.c_void_p, ctypes.c_void_p)(operation, item, None))
        check(invoke(operation, 21)(operation))
        aborted = wintypes.BOOL()
        check(invoke(operation, 22, ctypes.POINTER(wintypes.BOOL))(operation, ctypes.byref(aborted)))
        if aborted.value or path.exists():
            raise ValueError('文件尚未移入回收站，可能被占用或操作已取消；未执行永久删除')
    finally:
        for ptr in (item, operation):
            if ptr:
                invoke(ptr, 2)(ptr)
        if initialized >= 0:
            ole.CoUninitialize()
