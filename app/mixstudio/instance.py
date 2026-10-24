"""Process-owned data-directory lock, held for the lifetime of one Studio.

Acquire before opening the database or recovering jobs. Keep the lock object
alive and release only after background workers have stopped. The lock file is
not deleted: deleting it could allow a second process to lock a different inode.
The operating system releases the lock if the owning process exits or crashes.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import threading

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class DataDirectoryLock:
    """Non-blocking exclusive lock for one resolved application data directory."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / ".instance.lock"
        self._fd: int | None = None
        self._guard = threading.RLock()

    def acquire(self):
        """Return self after acquiring; a second acquire on this object is idempotent."""
        with self._guard:
            if self._fd is not None:
                return self
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    self.path,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOINHERIT", 0),
                    0o600,
                )
            except OSError:
                raise ValueError("程序数据目录不可写，请检查目录位置和当前用户权限。") from None
            try:
                os.set_inheritable(fd, False)
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    # Windows permits a one-byte range lock beyond EOF. Lock
                    # first, then initialize: no competing unprotected writes.
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                self._fd = fd
                return self
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise ValueError(
                        "已有程序正在使用此数据目录，请返回已打开的窗口；关闭后再启动。"
                    ) from None
                raise ValueError("程序数据目录锁定失败，请检查磁盘和目录状态。") from None
            except BaseException:
                os.close(fd)
                raise

    def release(self):
        """Release this object's ownership; repeated release is harmless."""
        with self._guard:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False
