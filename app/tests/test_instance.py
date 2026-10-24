"""Real OS-lock tests; no network, model APIs, user data, or credentials."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mixstudio.instance import DataDirectoryLock


HELPER = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from mixstudio.instance import DataDirectoryLock
lock = DataDirectoryLock(sys.argv[2])
try:
    lock.acquire()
except ValueError:
    print("LOCK_BUSY", flush=True)
    sys.exit(3)
print("LOCK_ACQUIRED", flush=True)
if sys.argv[3] == "crash":
    os._exit(7)
if sys.argv[3] == "hold":
    sys.stdin.readline()
lock.release()
print("LOCK_RELEASED", flush=True)
"""


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"
        self.workspace = Path(__file__).resolve().parents[1]
        self.owned = []

    def tearDown(self):
        for lock in self.owned:
            lock.release()
        self.temp.cleanup()

    def make_lock(self, root=None):
        lock = DataDirectoryLock(root or self.root)
        self.owned.append(lock)
        return lock

    def command(self, mode="once"):
        return [sys.executable, "-I", "-B", "-u", "-X", "utf8", "-c", HELPER,
                str(self.workspace), str(self.root), mode]

    def child(self, mode="once"):
        return subprocess.run(
            self.command(mode), input="", text=True, encoding="utf-8",
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_same_object_idempotent_and_descriptor_not_inherited(self):
        lock = self.make_lock()
        self.assertIs(lock.acquire(), lock)
        fd = lock._fd
        self.assertIs(lock.acquire(), lock)
        self.assertEqual(lock._fd, fd)
        self.assertFalse(os.get_inheritable(fd))
        lock.release()
        lock.release()
        self.assertIsNone(lock._fd)

    def test_second_object_same_process_refused_then_released(self):
        first, second = self.make_lock(), self.make_lock()
        first.acquire()
        with self.assertRaisesRegex(ValueError, "已有程序"):
            second.acquire()
        first.release()
        self.assertIs(second.acquire(), second)

    def test_resolved_alias_refers_to_same_lock(self):
        first = self.make_lock()
        first.acquire()
        alias = self.make_lock(self.root / ".." / "data")
        self.assertEqual(alias.root, first.root)
        with self.assertRaisesRegex(ValueError, "已有程序"):
            alias.acquire()

    def test_real_second_process_refused_then_allowed_after_release(self):
        first = self.make_lock()
        first.acquire()
        blocked = self.child()
        self.assertEqual(blocked.returncode, 3, blocked.stderr)
        self.assertEqual(blocked.stdout.strip(), "LOCK_BUSY")
        first.release()
        allowed = self.child()
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout.splitlines(), ["LOCK_ACQUIRED", "LOCK_RELEASED"])

    def test_crash_releases_os_lock_without_file_removal(self):
        crashed = self.child("crash")
        self.assertEqual(crashed.returncode, 7, crashed.stderr)
        self.assertEqual(crashed.stdout.strip(), "LOCK_ACQUIRED")
        lock = self.make_lock()
        self.assertTrue(lock.path.exists())
        lock.acquire()
        lock.release()
        self.assertTrue(lock.path.exists())

    def test_child_holds_lock_until_release(self):
        process = subprocess.Popen(
            self.command("hold"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            # Read the acquired signal in a helper thread so the parent waits
            # with a deadline rather than blocking indefinitely on readline.
            import queue
            import threading
            messages = queue.Queue()
            reader = threading.Thread(target=lambda: messages.put(process.stdout.readline()),
                                      daemon=True)
            reader.start()
            self.assertEqual(messages.get(timeout=5).strip(), "LOCK_ACQUIRED")
            reader.join(timeout=1)
            lock = self.make_lock()
            with self.assertRaisesRegex(ValueError, "已有程序"):
                lock.acquire()
            stdout, stderr = process.communicate("\n", timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("LOCK_RELEASED", stdout)
            lock.acquire()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_context_manager_releases_on_exception(self):
        lock = self.make_lock()
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with lock:
                raise RuntimeError("fixture")
        self.make_lock().acquire()

    def test_different_directories_can_run_independently(self):
        self.make_lock().acquire()
        self.make_lock(Path(self.temp.name) / "other-data").acquire()

    def test_non_directory_root_is_reported_cleanly(self):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_bytes(b"not-a-directory")
        with self.assertRaisesRegex(ValueError, "数据目录不可写"):
            self.make_lock().acquire()


if __name__ == "__main__":
    unittest.main()
