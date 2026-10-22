#!/usr/bin/env python3
"""Tiny offline lifecycle fixture, never a mirror/index or capacity benchmark.

The Docker gate uses actual cgroup admission. Native unshare tests explicitly
bypass only cgroup admission (unshare creates no memory controller), retaining
real Tini, PID namespace, subprocesses, inherited flock and kernel teardown.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("supervise", "child", "grandchild", "verify"))
    parser.add_argument("root", type=Path)
    parser.add_argument("mode", choices=("supervisor-kill", "child-kill", "supervisor-term"))
    parser.add_argument("--tini", default="/usr/bin/tini")
    parser.add_argument("--native-no-cgroup", action="store_true")
    args = parser.parse_args()
    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    if args.action == "grandchild":
        # Deliberately escape the indexer's process group. PID namespace teardown
        # must still terminate this descendant after coordinator failure/exit.
        os.setsid()
        with (root / "ready").open("w") as ready:
            ready.write(str(os.getpid()))
        while True:
            with (root / "heartbeat").open("a") as log:
                log.write("alive\n")
            time.sleep(.025)
    if args.action == "child":
        # Pass the actual inherited lock FD through another generation.
        lock_fd = int(os.environ["LORE_FIXTURE_LOCK_FD"])
        subprocess.Popen([sys.executable, script, "grandchild", str(root), args.mode],
                         pass_fds=(lock_fd,))
        (root / "child.pid").write_text(str(os.getpid()))
        while True:
            time.sleep(.1)
    if args.action == "verify":
        info = json.loads((root / "admission.json").read_text())
        assert info["pid_namespace"], info
        if not args.native_no_cgroup:
            assert info["oom_protection"] == "pid_namespace_init", info
            assert info["swap_bytes"] == 0 and 0 < info["memory_bytes"] <= 128 * 1024**2, info
        assert (root / "ready").exists(), "grandchild did not start"
        with (root / "index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = (root / "heartbeat").stat().st_size
        time.sleep(.15)
        assert (root / "heartbeat").stat().st_size == before, "descendant survived namespace exit"
        if args.mode == "child-kill":
            result = json.loads((root / "result.json").read_text())
            assert result["failed"] and "(-9)" in result["error"], result
            assert result["seconds"] < 5, result
        print(json.dumps({"mode": args.mode, "lock_released": True,
                          "escaped_descendant_stopped": True, "admission": info}))
        return 0

    from lore_mirror import code_index as index
    index.NAMESPACE_INIT = Path(args.tini)
    namespace = index._dedicated_pid_namespace_init()
    assert namespace, "not the verified direct child of namespace PID 1 Tini"
    if args.native_no_cgroup:
        cap = {"version": 2, "path": "/native-fixture/no-cgroup", "memory_bytes": 128 * 1024**2,
               "swap_bytes": 0, "oom_group": 0, "oom_protection": "pid_namespace_init",
               "pid_namespace": namespace, "native_fixture_only": True}
        index._enforced_memory_limit = lambda budget: cap
    else:
        cap = index._enforced_memory_limit(128 * 1024**2)
        # Force this gate to cover the supported Docker 0 value path, not let an
        # unrelated oom.group=1 policy disguise a broken PID-init admission.
        assert cap["oom_protection"] == "pid_namespace_init", cap
    (root / "admission.json").write_text(json.dumps(cap))
    config = {"limits": {"command_output_bytes": 4096}, "min_free_fraction": .30,
              "index": {"memory_bytes": 128 * 1024**2, "timeout_seconds": 30,
                        "max_index_bytes": 1024 * 1024, "symbols": False}}
    with (root / "index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runner = index.BuildRunner(config, root, lock.fileno(), cap)
        original_environment = runner.environment
        runner.environment = lambda: dict(original_environment(), LORE_FIXTURE_LOCK_FD=str(lock.fileno()),
                                          PYTHONPATH=os.environ.get("PYTHONPATH", ""))
        def interrupt():
            deadline = time.monotonic() + 10
            while not ((root / "ready").exists() and (root / "child.pid").exists()):
                if time.monotonic() > deadline:
                    os._exit(99)
                time.sleep(.01)
            # Ensure every process exists and holds the inherited lock first.
            time.sleep(.1)
            target = int((root / "child.pid").read_text()) if args.mode == "child-kill" else os.getpid()
            if args.mode == "supervisor-term":
                # Docker stop targets PID 1; prove Tini forwards SIGTERM.
                target = 1
            os.kill(target, signal.SIGTERM if args.mode == "supervisor-term" else signal.SIGKILL)
        threading.Thread(target=interrupt, daemon=True).start()
        started = time.monotonic()
        try:
            runner.run([sys.executable, script, "child", str(root), args.mode])
        except index.CodeIndexError as exc:
            (root / "result.json").write_text(json.dumps({"failed": True, "error": str(exc),
                                                          "seconds": time.monotonic() - started}))
            return 42
        raise AssertionError("fixture should fail, not complete")


if __name__ == "__main__":
    sys.exit(main())
