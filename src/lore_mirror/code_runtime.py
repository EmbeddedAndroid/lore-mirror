"""Shared bounded process mechanics and exact code-ref syntax.

Callers own network policy, cgroup admission, diagnostics and retry semantics.
This primitive owns pipes, deadlines, sampled memory and child-group cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time


_REF = re.compile(r"refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


def code_ref_valid(value):
    """One syntax policy for exact head/tag names, never revision expressions."""
    return (isinstance(value, str) and len(value) <= 512 and bool(_REF.fullmatch(value))
        and ".." not in value and "//" not in value and not value.endswith(("/", "."))
        and all(not part.startswith(".") and not part.endswith(".lock") for part in value.split("/")))


class CodeRunError(ValueError):
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


@dataclass
class RunResult:
    output: bytes
    stderr: bytes
    returncode: int
    partial_reason: str | None = None



def _group_rss(pid):
    """Sample the whole process group, including helpers reparented to init."""
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pid:
                total += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    return total

def _group_has_live_processes(pid):
    """Zombies keep a group ID but have already closed their inherited FDs."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pid and fields[0] not in ("Z", "X", "x"):
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            # If a process cannot be inspected, do not falsely certify that
            # the group has released its writer-lock descriptors.
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return False
            return True
    return False


def stop_process_group(child, *, timeout=1.0):
    """Kill and boundedly wait for the entire group to release inherited FDs.

    Reaping just the leader is insufficient: an orphaned helper can still own
    the coordinator's flock. Ignore zombies (only their parent can reap them),
    and fail rather than claim cleanup succeeded if a live task outlasts the
    cleanup budget. No unbounded wait is safe, even after SIGKILL.
    """
    deadline = time.monotonic() + max(0, timeout)
    while True:
        group_exists = True
        try:
            # Repeat for helpers forked concurrently with the previous signal.
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            group_exists = False
        leader_exited = child.poll() is not None  # Also reaps our direct child.
        if leader_exited and (not group_exists or not _group_has_live_processes(child.pid)):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(child.args, timeout)
        time.sleep(min(.01, remaining))


def bounded_run(argv, *, env, max_bytes, timeout=None, deadline=None, memory_bytes=None,
                input_bytes=b"", max_input_bytes=65536, stderr_bytes=65536,
                combined_output_bytes=None, pass_fds=(), cwd=None, consume=None,
                guard=None, guard_interval=.25):
    """Run without a shell, retaining bounded prefixes or streaming stdout.

    Exactly one timeout/deadline is required. Returncodes and budget outcomes are
    data so readers may retain partial results while writers fail closed. The
    optional guard(child) enforces caller-specific storage/cgroup policy and its
    exceptions propagate after cleanup. Writer lock descriptors are inherited
    explicitly; readers pass none. RSS is sampled, not a hard memory guarantee.
    """
    if input_bytes is None:
        input_bytes = b""
    if not isinstance(input_bytes, bytes) or len(input_bytes) > max_input_bytes:
        raise CodeRunError("input_limit", "Command input exceeds its byte budget")
    if (timeout is None) == (deadline is None):
        raise ValueError("provide exactly one timeout or deadline")
    if deadline is None:
        deadline = time.monotonic() + max(0, timeout)
    if guard:
        guard(None)
    if time.monotonic() >= deadline:
        return RunResult(b"", b"", -1, "timeout")
    child = subprocess.Popen(list(map(str, argv)), stdin=subprocess.PIPE if input_bytes else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
        start_new_session=True, pass_fds=tuple(pass_fds))
    out, err = bytearray(), bytearray()
    counts = {"out": 0, "err": 0}
    reason = None
    offset = 0
    next_guard = 0.0
    cleanup_attempted = False
    drain_deadline = None
    try:
        with selectors.DefaultSelector() as selector:
            if input_bytes:
                os.set_blocking(child.stdin.fileno(), False)
                selector.register(child.stdin, selectors.EVENT_WRITE, "in")
            for stream, kind in ((child.stdout, "out"), (child.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            # Keep supervising after pipe EOF: a process may close both outputs
            # and still consume CPU/memory or write files before exiting.
            while selector.get_map() or child.poll() is None:
                if not cleanup_attempted and child.poll() is not None:
                    # A failed/OOM-killed leader must not leave us waiting for
                    # inherited pipes until the full command deadline. Kill its
                    # helpers now, then drain bytes already buffered in pipes.
                    cleanup_attempted = True
                    stop_process_group(child)
                    drain_deadline = time.monotonic() + .1
                now = time.monotonic()
                if now >= deadline:
                    reason = "timeout"
                    break
                if drain_deadline is not None and now >= drain_deadline:
                    reason = "pipe_drain_timeout"
                    break
                if memory_bytes is not None and _group_rss(child.pid) > memory_bytes:
                    reason = "memory_limit"
                    break
                if guard and now >= next_guard:
                    guard(child)
                    next_guard = time.monotonic() + guard_interval
                interval = 0 if cleanup_attempted else min(.025, max(0, deadline - time.monotonic()))
                events = selector.select(interval) if selector.get_map() else ()
                if cleanup_attempted and not events:
                    # An escaped helper may still hold a pipe. Drain only
                    # currently buffered bytes, never await its eventual EOF.
                    break
                if not selector.get_map():
                    time.sleep(interval)
                for key, _ in events:
                    if key.data == "in":
                        try:
                            offset += os.write(key.fd, input_bytes[offset:offset + 4096])
                        except BrokenPipeError:
                            offset = len(input_bytes)
                        if offset >= len(input_bytes):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    cap = max_bytes if key.data == "out" else stderr_bytes
                    remaining = max(0, cap - counts[key.data])
                    if combined_output_bytes is not None:
                        remaining = min(remaining, max(0, combined_output_bytes - sum(counts.values())))
                    accepted = chunk[:remaining]
                    counts[key.data] += len(accepted)
                    if key.data == "out" and consume:
                        consume(accepted)
                    else:
                        (out if key.data == "out" else err).extend(accepted)
                    if len(chunk) > remaining:
                        reason = "output_limit" if key.data == "out" or combined_output_bytes is not None else "stderr_limit"
                        break
                if reason:
                    break
            if not reason and guard:
                guard(None)
        if not cleanup_attempted:
            cleanup_attempted = True
            stop_process_group(child)
        return RunResult(bytes(out), bytes(err), child.returncode, reason)
    finally:
        # Includes guard/consumer exceptions and a leader that already exited
        # while a background descendant retained the inherited writer lock.
        try:
            if not cleanup_attempted:
                active_error = sys.exception()
                try:
                    stop_process_group(child)
                except subprocess.TimeoutExpired:
                    if active_error is None:
                        raise
                    active_error.add_note("Process-group cleanup exceeded its bounded wait")
        finally:
            child.stdout.close()
            child.stderr.close()
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
