"""Shared process mechanics, with only local bounded fixture subprocesses."""
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from lore_mirror.code_runtime import (CodeRunError, _group_has_live_processes,
    bounded_run, code_ref_valid, stop_process_group)


class RuntimeTests(unittest.TestCase):
    def run_child(self, script, **kwargs):
        options = dict(env={}, timeout=2, max_bytes=65536, memory_bytes=128 * 1024**2)
        options.update(kwargs)
        return bounded_run([sys.executable, "-c", script], **options)

    def test_exact_refs_share_one_strict_syntax(self):
        for ref in ("refs/heads/main", "refs/tags/v6.12", "refs/heads/topic/fix"):
            self.assertTrue(code_ref_valid(ref))
        for ref in (None, "HEAD", "refs/heads/../main", "refs/heads/.private", "refs/heads/main.lock",
                    "refs/heads/main~1", "refs/heads/snow-雪", "refs/heads/" + "x" * 512):
            self.assertFalse(code_ref_valid(ref))

    def test_input_guard_precedes_process_creation(self):
        with mock.patch("lore_mirror.code_runtime.subprocess.Popen", side_effect=AssertionError("spawn")):
            with self.assertRaises(CodeRunError) as caught:
                self.run_child("", input_bytes=b"12345", max_input_bytes=4)
        self.assertEqual(caught.exception.reason, "input_limit")

    def test_full_duplex_pipes_and_streaming_keep_bounded_output(self):
        chunks = []
        result = self.run_child("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())",
            input_bytes=b"x" * 100000, max_input_bytes=100000, max_bytes=100000, consume=chunks.append)
        self.assertEqual(result.output, b"")
        self.assertEqual(b"".join(chunks), b"x" * 100000)
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.partial_reason)

    def test_combined_output_and_stderr_budget(self):
        result = self.run_child("import os;os.write(1,b'x'*80);os.write(2,b'y'*80)",
            max_bytes=100, stderr_bytes=100, combined_output_bytes=100)
        self.assertEqual(len(result.output) + len(result.stderr), 100)
        self.assertEqual(result.partial_reason, "output_limit")

    def test_guard_continues_after_all_pipes_close(self):
        calls = []
        def guard(child):
            calls.append(child)
        result = self.run_child("import os,time;os.close(1);os.close(2);time.sleep(10)",
            timeout=.15, guard=guard, guard_interval=.01)
        self.assertEqual(result.partial_reason, "timeout")
        self.assertGreater(len(calls), 3)

    def test_reparented_helper_remains_in_memory_budget(self):
        # Keep the group leader alive while an intermediate helper exits and
        # leaves its own child orphaned. An exited leader is now cleaned up
        # immediately instead of being allowed to leave a growing helper.
        script = ("import os,time\n"
                  "if os.fork() == 0:\n"
                  " if os.fork(): os._exit(0)\n"
                  " x=bytearray(32*1024*1024)\n"
                  "time.sleep(10)\n")
        started = time.monotonic()
        result = self.run_child(script, memory_bytes=16 * 1024**2)
        self.assertEqual(result.partial_reason, "memory_limit")
        self.assertLess(time.monotonic() - started, 2)

    def test_nonzero_returncode_and_diagnostic_are_caller_policy(self):
        result = self.run_child("import sys;sys.stderr.write('not found');sys.exit(128)")
        self.assertEqual(result.returncode, 128)
        self.assertEqual(result.stderr, b"not found")
        self.assertIsNone(result.partial_reason)

    def test_guard_exception_reaps_leader_and_retains_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pid"
            def guard(child):
                if path.exists():
                    raise RuntimeError("policy rejected")
            with self.assertRaisesRegex(RuntimeError, "policy rejected"):
                self.run_child(f"import os,time;open({str(path)!r},'w').write(str(os.getpid()));time.sleep(10)",
                    guard=guard, guard_interval=.01)
            self.assertFalse(Path("/proc", path.read_text()).exists())

    def test_exited_leader_cleanup_releases_real_inherited_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "writer.lock"
            # An independent open file description must acquire the flock
            # immediately on return, not after a test-side settling sleep.
            for attempt in range(5):
                with self.subTest(attempt=attempt), lock.open("a+b") as held, lock.open("a+b") as probe:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    child = subprocess.Popen([sys.executable, "-c",
                        "import os,time\nif os.fork(): os._exit(0)\ntime.sleep(10)\n"],
                        start_new_session=True, pass_fds=(held.fileno(),),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        child.wait(timeout=2)
                        held.close()
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        stop_process_group(child)
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        stop_process_group(child)

    def test_success_cleanup_releases_background_helpers_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "writer.lock"
            with lock.open("a+b") as held, lock.open("a+b") as probe:
                fcntl.flock(held, fcntl.LOCK_EX)
                result = self.run_child("import os,time\n"
                    "if os.fork(): os._exit(0)\n"
                    "os.close(1);os.close(2);time.sleep(10)\n", pass_fds=(held.fileno(),))
                held.close()
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(result.returncode, 0)
                self.assertIsNone(result.partial_reason)

    def test_exited_leader_does_not_wait_for_helpers_inherited_pipes(self):
        for killed in (False, True):
            with self.subTest(leader_killed=killed):
                script = ("import os,signal,time\n"
                    "if os.fork() == 0: time.sleep(10);os._exit(0)\n"
                    "os.write(1,b'buffered stdout');os.write(2,b'buffered stderr')\n"
                    + ("os.kill(os.getpid(),signal.SIGKILL)\n" if killed else "os._exit(0)\n"))
                started = time.monotonic()
                result = self.run_child(script, timeout=2)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(result.output, b"buffered stdout")
                self.assertEqual(result.stderr, b"buffered stderr")
                self.assertEqual(result.returncode, -signal.SIGKILL if killed else 0)
                self.assertIsNone(result.partial_reason)

    def test_killed_leader_does_not_wait_for_escaped_helpers_pipe_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "escaped.pid"
            script = ("import os,signal,time\n"
                "if os.fork() == 0:\n"
                " os.setsid()\n"
                f" open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
                " time.sleep(10);os._exit(0)\n"
                f"while not os.path.exists({str(pidfile)!r}): time.sleep(.001)\n"
                "os.write(1,b'buffered stdout');os.write(2,b'buffered stderr')\n"
                "os.kill(os.getpid(),signal.SIGKILL)\n")
            try:
                started = time.monotonic()
                result = self.run_child(script, timeout=2)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(result.output, b"buffered stdout")
                self.assertEqual(result.stderr, b"buffered stderr")
                self.assertEqual(result.returncode, -signal.SIGKILL)
                self.assertIsNone(result.partial_reason)
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_timeout_cleanup_releases_helpers_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "writer.lock"
            with lock.open("a+b") as held, lock.open("a+b") as probe:
                fcntl.flock(held, fcntl.LOCK_EX)
                result = self.run_child("import os,time;os.fork();time.sleep(10)",
                    pass_fds=(held.fileno(),), timeout=.1)
                held.close()
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(result.returncode, -signal.SIGKILL)
                self.assertEqual(result.partial_reason, "timeout")

    def test_guard_exception_cleanup_releases_helpers_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock, ready = Path(tmp) / "writer.lock", Path(tmp) / "ready"
            with lock.open("a+b") as held, lock.open("a+b") as probe:
                fcntl.flock(held, fcntl.LOCK_EX)
                def guard(child):
                    if ready.exists():
                        raise RuntimeError("policy rejected helper")
                with self.assertRaisesRegex(RuntimeError, "policy rejected helper"):
                    self.run_child("import os,time\n"
                        f"if os.fork(): open({str(ready)!r}, 'w').write('ready')\n"
                        "time.sleep(10)\n", pass_fds=(held.fileno(),),
                        guard=guard, guard_interval=.01)
                held.close()
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_group_scan_ignores_zombies_but_not_live_helpers(self):
        entry = mock.MagicMock()
        entry.name = "42"
        stat = mock.Mock()
        entry.__truediv__.return_value = stat
        # The command field may contain parentheses; process group is field 5.
        with mock.patch("lore_mirror.code_runtime.Path.iterdir", return_value=[entry]):
            for state, live in (("Z", False), ("X", False), ("x", False), ("S", True), ("D", True)):
                with self.subTest(state=state):
                    stat.read_text.return_value = f"42 (helper (git)) {state} 1 123 123"
                    self.assertEqual(_group_has_live_processes(123), live)
            stat.read_text.return_value = "42 (unrelated) S 1 456 456"
            self.assertFalse(_group_has_live_processes(123))

    def test_cleanup_wait_is_bounded_for_leader_and_helpers(self):
        for returncode in (None, 0):
            with self.subTest(leader_returncode=returncode):
                child = mock.Mock(pid=123, args=["fixture"])
                child.poll.return_value = returncode
                with mock.patch("lore_mirror.code_runtime.os.killpg") as kill, \
                        mock.patch("lore_mirror.code_runtime._group_has_live_processes", return_value=True), \
                        mock.patch("lore_mirror.code_runtime.time.monotonic", side_effect=[10, 10.01, 10.03]), \
                        mock.patch("lore_mirror.code_runtime.time.sleep") as sleep:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        stop_process_group(child, timeout=.02)
                child.wait.assert_not_called()
                self.assertEqual(kill.call_count, 2)
                self.assertEqual(sleep.call_count, 1)
                self.assertLessEqual(sleep.call_args.args[0], .01)

    def test_cleanup_timeout_is_reported_once_and_closes_pipes(self):
        child = mock.Mock(pid=123)
        child.poll.return_value = 0
        child.stdin = None
        with mock.patch("lore_mirror.code_runtime.subprocess.Popen", return_value=child), \
                mock.patch("lore_mirror.code_runtime.os.set_blocking"), \
                mock.patch("lore_mirror.code_runtime.selectors.DefaultSelector") as selector, \
                mock.patch("lore_mirror.code_runtime.stop_process_group",
                    side_effect=subprocess.TimeoutExpired(["fixture"], 1)) as stop:
            selector.return_value.__enter__.return_value.get_map.return_value = {}
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_child("", memory_bytes=None)
        stop.assert_called_once_with(child)
        child.stdout.close.assert_called_once()
        child.stderr.close.assert_called_once()

    def test_cleanup_timeout_preserves_guard_exception_and_closes_pipes(self):
        child = mock.Mock(pid=123)
        child.poll.return_value = None
        child.stdout, child.stderr = mock.Mock(), mock.Mock()
        child.stdin = None
        guard_error = RuntimeError("policy rejected")
        def guard(process):
            if process is not None:
                raise guard_error
        with mock.patch("lore_mirror.code_runtime.subprocess.Popen", return_value=child), \
                mock.patch("lore_mirror.code_runtime.os.set_blocking"), \
                mock.patch("lore_mirror.code_runtime.selectors.DefaultSelector"), \
                mock.patch("lore_mirror.code_runtime.stop_process_group",
                    side_effect=subprocess.TimeoutExpired(["fixture"], 1)) as stop:
            with self.assertRaisesRegex(RuntimeError, "policy rejected") as caught:
                self.run_child("", guard=guard, memory_bytes=None)
        self.assertIs(caught.exception, guard_error)
        self.assertIn("cleanup exceeded", guard_error.__notes__[0])
        stop.assert_called_once_with(child)
        child.stdout.close.assert_called_once()
        child.stderr.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
