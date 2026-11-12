"""Process-lifetime lock contract for tools/locked_batch.py.

All fixtures are generated in temporary directories. No production path, media
or NAS mount is read by these tests.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
LOCKED_BATCH = PROJECT / "tools" / "locked_batch.py"
EXIT_TEMPFAIL = 75


WORKER_SOURCE = textwrap.dedent(
    """
    from __future__ import annotations

    import hashlib
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time


    def atomic_write(path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


    def write_item(state: Path, name: str) -> str:
        payload = ("generated:" + name).encode("utf-8")
        atomic_write(state / name, payload)
        return hashlib.sha256(payload).hexdigest()


    def read_checkpoint(state: Path) -> dict:
        return json.loads((state / "checkpoint.json").read_text(encoding="utf-8"))


    def write_checkpoint(state: Path, checkpoint: dict) -> None:
        payload = json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode("utf-8")
        atomic_write(state / "checkpoint.json", payload)


    def wait_for(path: Path, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        raise SystemExit("fixture timeout waiting for " + str(path))


    def main() -> None:
        mode = sys.argv[1]
        state = Path(sys.argv[2])
        state.mkdir(parents=True, exist_ok=True)

        if mode == "hold":
            atomic_write(state / "ready", b"ready")
            time.sleep(60)
            return

        if mode == "crash-after-commit":
            checksums = {"item-01": write_item(state, "item-01")}
            write_checkpoint(
                state,
                {
                    "completed": ["item-01"],
                    "pending": ["item-02"],
                    "sha256": checksums,
                },
            )
            atomic_write(state / "ready", b"ready")
            time.sleep(60)
            return

        if mode == "complete-pending":
            checkpoint = read_checkpoint(state)
            for name in checkpoint["completed"]:
                actual = digest(state / name)
                expected = checkpoint["sha256"][name]
                if actual != expected:
                    raise SystemExit("checksum mismatch for " + name)
            for name in checkpoint["pending"]:
                checkpoint["sha256"][name] = write_item(state, name)
                checkpoint["completed"].append(name)
            checkpoint["pending"] = []
            write_checkpoint(state, checkpoint)
            atomic_write(state / "done", b"done")
            return

        if mode == "child-hold":
            child_ready = state / "child-ready"
            child = subprocess.Popen(
                [sys.executable, __file__, "child", str(state), "4.0"],
                close_fds=False,
            )
            wait_for(child_ready)
            atomic_write(state / "parent-exit", str(os.getpid()).encode("ascii"))
            return

        if mode == "child":
            atomic_write(state / "child-ready", str(os.getpid()).encode("ascii"))
            time.sleep(float(sys.argv[3]))
            return

        raise SystemExit("unknown fixture mode: " + mode)


    if __name__ == "__main__":
        main()
    """
)


def wait_for(path: Path, proc: subprocess.Popen[bytes] | None = None, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"process exited before creating {path}")
        time.sleep(0.01)
    raise AssertionError(f"timeout waiting for {path}")


def wait_for_process_exit(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise AssertionError(f"child process {pid} did not exit")


class LockedBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="locked-batch-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.lock = self.root / "batch.lock"
        self.worker = self.root / "worker.py"
        self.worker.write_text(WORKER_SOURCE, encoding="utf-8")
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self._cleanup_processes)

    def _cleanup_processes(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        for pid_file in (self.state / "child-ready",):
            if not pid_file.exists():
                continue
            try:
                pid = int(pid_file.read_text(encoding="ascii"))
            except (OSError, ValueError):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def start_locked(self, mode: str, *arguments: str) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(LOCKED_BATCH),
                str(self.lock),
                sys.executable,
                str(self.worker),
                mode,
                str(self.state),
                *arguments,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(proc)
        return proc

    def run_locked(self, mode: str, *arguments: str, timeout: float = 15.0) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable,
                str(LOCKED_BATCH),
                str(self.lock),
                sys.executable,
                str(self.worker),
                mode,
                str(self.state),
                *arguments,
            ],
            capture_output=True,
            timeout=timeout,
        )

    def assert_lock_is_free(self) -> None:
        marker = self.state / "probe-ran"
        if marker.exists():
            marker.unlink()
        result = subprocess.run(
            [
                sys.executable,
                str(LOCKED_BATCH),
                str(self.lock),
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')",
                str(marker),
            ],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertTrue(marker.exists())

    def test_second_instance_exits_75_without_running_command(self) -> None:
        first = self.start_locked("hold")
        wait_for(self.state / "ready", first)

        marker = self.state / "second-ran"
        second = subprocess.run(
            [
                sys.executable,
                str(LOCKED_BATCH),
                str(self.lock),
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')",
                str(marker),
            ],
            capture_output=True,
            timeout=15,
        )

        self.assertEqual(second.returncode, EXIT_TEMPFAIL)
        self.assertIn("Active job holds", second.stderr.decode("utf-8", "replace"))
        self.assertFalse(marker.exists())
        self.assertIsNone(first.poll())

    def test_lock_released_after_normal_exit(self) -> None:
        first = self.start_locked("hold")
        wait_for(self.state / "ready", first)
        first.terminate()
        first.wait(timeout=15)
        self.assert_lock_is_free()

    def test_sigkill_releases_lock_and_preserves_committed_artifacts(self) -> None:
        first = self.start_locked("crash-after-commit")
        wait_for(self.state / "ready", first)
        committed = self.state / "item-01"
        before = committed.stat()
        before_digest = hashlib.sha256(committed.read_bytes()).hexdigest()

        first.kill()
        first.wait(timeout=15)
        self.assert_lock_is_free()

        complete = self.run_locked("complete-pending")
        self.assertEqual(complete.returncode, 0, complete.stderr.decode("utf-8", "replace"))
        self.assertTrue((self.state / "done").exists())
        self.assertTrue((self.state / "item-02").exists())
        after = committed.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(hashlib.sha256(committed.read_bytes()).hexdigest(), before_digest)
        checkpoint = json.loads((self.state / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["completed"], ["item-01", "item-02"])
        self.assertEqual(checkpoint["pending"], [])

    def test_lock_remains_while_child_holds_fd_then_releases(self) -> None:
        first = self.start_locked("child-hold")
        wait_for(self.state / "parent-exit", first)
        first.wait(timeout=15)
        child_pid = int((self.state / "child-ready").read_text(encoding="ascii"))

        held = self.run_locked("hold")
        self.assertEqual(held.returncode, EXIT_TEMPFAIL)
        self.assertIn("Active job holds", held.stderr.decode("utf-8", "replace"))

        wait_for_process_exit(child_pid)
        self.assert_lock_is_free()

    def test_pending_work_is_redone_without_rewriting_committed_output(self) -> None:
        committed = self.state / "item-01"
        committed.write_bytes(b"generated:item-01")
        checksum = hashlib.sha256(committed.read_bytes()).hexdigest()
        (self.state / "checkpoint.json").write_text(
            json.dumps(
                {"completed": ["item-01"], "pending": ["item-02"], "sha256": {"item-01": checksum}},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        before = committed.stat()

        result = self.run_locked("complete-pending")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        after = committed.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertTrue((self.state / "item-02").exists())
        checkpoint = json.loads((self.state / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["completed"], ["item-01", "item-02"])
        self.assertEqual(checkpoint["pending"], [])


if __name__ == "__main__":
    unittest.main()
