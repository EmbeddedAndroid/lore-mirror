"""Offline index coordinator tests. Fake engine never bypasses cgroup admission.

The process-lock regression uses a child process only; no engine/network access.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from lore_mirror.code_config import code_catalog_fingerprint, normalize_code_config
from lore_mirror.code_index import (BuildRunner, CodeIndexError, ENGINE_COMMIT, ZoektIndexAdapter,
    _cgroup_directory, _cgroup_metrics, _dedicated_pid_namespace_init, _enforced_memory_limit, index_code_once)
from lore_mirror.code_sync import SAFE_REPO_CONFIG

CAP = {"version": 2, "path": "/fixture/cgroup", "memory_bytes": 512 * 1024**2,
       "swap_bytes": 0, "oom_group": 1}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


class FakeIndexAdapter:
    def __init__(self):
        self.builds = 0
        self.identities = 0
        self.fail = False
        self.wrong_oid = False
        self.during_build = None

    def identity(self, runner):
        self.identities += 1
        identity = {"engine_commit": ENGINE_COMMIT, "indexer_sha256": "a" * 64, "query_bridge_sha256": "b" * 64}
        if runner.options["symbols"]:
            identity["ctags_sha256"] = "c" * 64
        return identity

    def build(self, repo, oid, directory, runner):
        self.builds += 1
        (directory / "fixture_v16.00000.zoekt").write_bytes(b"offline fake shard")
        if self.during_build:
            self.during_build(repo, oid, directory, runner)
        if self.fail:
            raise CodeIndexError("simulated indexer failure")

    def verify(self, directory, oid, runner):
        return {"engine_commit": ENGINE_COMMIT, "repository": "boot",
                "commit_oid": "f" * 40 if self.wrong_oid else oid,
                "has_symbols": runner.options["symbols"]}


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.code_root = self.data / "code"
        self.repo = self.code_root / "repositories" / "boot.git"
        self.work = self.root / "work"
        self.work.mkdir()
        git(self.work, "init", "-b", "main")
        git(self.work, "config", "user.name", "Index Test")
        git(self.work, "config", "user.email", "test@example.invalid")
        (self.work / "test.c").write_text("int hello(void) { return 1; }\n")
        git(self.work, "add", "test.c")
        git(self.work, "commit", "-m", "fixture")
        self.oid = git(self.work, "rev-parse", "HEAD")
        self.repo.parent.mkdir(parents=True)
        git(self.work, "clone", "--bare", str(self.work), str(self.repo))
        (self.repo / "config").write_text(SAFE_REPO_CONFIG)
        self.config = {"data_dir": str(self.data), "code": {"enabled": True,
            "trees": [{"id": "boot", "url": "https://git.u-boot-project.org/u-boot/u-boot.git",
                "default_ref": "refs/heads/main", "refs": ["refs/heads/main"], "index_refs": ["refs/heads/main"]}],
            "index": {"enabled": True, "pilot_tree": "boot", "pilot_ref": "refs/heads/main"}}}
        self._publish_code()
        self.adapter = FakeIndexAdapter()
        self.state_path = self.code_root / "index-state.json"
        self.cap_mock = mock.patch("lore_mirror.code_index._enforced_memory_limit", return_value=CAP)
        self.cap_mock.start()
        self.addCleanup(self.cap_mock.stop)

    def _publish_code(self, generation=1):
        code = normalize_code_config(self.config["code"], data_dir=self.data)
        tree = code["trees"][0]
        state = {"schema_version": 1, "generation": generation, "trees": {"boot": {
            "published_refs": {"refs/heads/main": self.oid},
            "config_fingerprint": code_catalog_fingerprint(tree), "upstream_url": tree["url"]}}}
        (self.code_root / "code-state.json").write_text(json.dumps(state))

    def run_once(self):
        return index_code_once(self.config, adapter=self.adapter)

    def test_disabled_has_no_cgroup_or_engine_call(self):
        self.config["code"]["index"]["enabled"] = False
        with mock.patch("lore_mirror.code_index._enforced_memory_limit", side_effect=AssertionError):
            state = self.run_once()
        self.assertEqual(state["status"], "disabled")
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.adapter.identities, 0)

    def test_real_cap_required_even_with_injected_adapter(self):
        with mock.patch("lore_mirror.code_index._enforced_memory_limit", side_effect=CodeIndexError("uncapped cgroup")):
            state = self.run_once()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("uncapped", state["last_error"])
        self.assertEqual(self.adapter.identities, 0)
        self.assertEqual(self.adapter.builds, 0)
        self.assertFalse((self.code_root / "indexes").exists())

    def test_exact_immutable_publication_and_unchanged_skip(self):
        state = self.run_once()
        self.assertEqual(state["status"], "ready", state)
        published = state["published"]
        self.assertEqual(published["commit_oid"], self.oid)
        self.assertEqual(published["engine_commit"], ENGINE_COMMIT)
        self.assertFalse(published["symbols"])
        self.assertFalse(published["has_symbols"])
        self.assertEqual(published["unsafe_paths"], 0)
        self.assertTrue(published["coverage"]["partial"])
        directory = self.code_root / published["directory"]
        self.assertTrue(directory.is_dir())
        again = self.run_once()
        self.assertEqual(again["generation"], 1)
        self.assertEqual(again["last_result"], "unchanged")
        self.assertEqual(self.adapter.builds, 1)

    def test_symbols_and_file_policy_changes_force_new_generation(self):
        first = self.run_once()
        self.config["code"]["index"]["symbols"] = True
        second = self.run_once()
        self.assertEqual(second["generation"], 2)
        self.assertTrue(second["published"]["has_symbols"])
        self.assertEqual(second["published"]["ctags_sha256"], "c" * 64)
        self.assertNotEqual(first["published"]["directory"], second["published"]["directory"])
        self.assertTrue((self.code_root / first["published"]["directory"]).is_dir())
        self.config["code"]["limits"] = {"max_file_bytes": 16}
        third = self.run_once()
        self.assertEqual(third["generation"], 3)
        self.assertNotEqual(second["published"]["policy_fingerprint"], third["published"]["policy_fingerprint"])

    def test_failed_build_keeps_last_good_exact_publication(self):
        first = self.run_once()
        self.config["code"]["index"]["symbols"] = True
        self.adapter.fail = True
        state = self.run_once()
        self.assertEqual(state["status"], "degraded")
        self.assertEqual(state["published"], first["published"])
        self.assertEqual(state["generation"], 1)

    def test_wrong_engine_or_commit_never_published(self):
        self.adapter.wrong_oid = True
        state = self.run_once()
        self.assertEqual(state["status"], "blocked")
        self.assertIsNone(state["published"])
        self.assertIn("exact pilot commit", state["last_error"])
        with mock.patch.object(self.adapter, "identity", return_value={"engine_commit": "wrong"}):
            state = self.run_once()
        self.assertIn("engine identity", state["last_error"])

    def test_killed_indexer_blocks_repeated_attempt_and_preserves_last_good(self):
        first = self.run_once()
        self.config["code"]["index"]["symbols"] = True
        def killed(repo, oid, directory, runner):
            runner.run([sys.executable, "-c", "import os,signal;os.kill(os.getpid(),signal.SIGKILL)"])
        self.adapter.during_build = killed
        failed = self.run_once()
        self.assertEqual(failed["status"], "blocked")
        self.assertEqual(failed["published"], first["published"])
        self.assertIn("(-9)", failed["last_error"])
        before = self.adapter.builds
        self.assertEqual(self.run_once()["last_result"], "limits_unchanged")
        self.assertEqual(self.adapter.builds, before)

    def test_shared_lock_excludes_other_work_and_no_state_write_on_collision(self):
        first = self.run_once()
        before = self.state_path.read_bytes()
        with (self.data / "index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = self.run_once()
        self.assertEqual(state["status"], "waiting_for_io")
        self.assertEqual(state["published"], first["published"])
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_shared_lock_held_through_build_and_publication(self):
        def check(*args):
            with (self.data / "index.lock").open("a") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.adapter.during_build = check
        self.assertEqual(self.run_once()["status"], "ready")

    def test_source_and_index_byte_budgets(self):
        self.config["code"]["index"]["max_source_bytes"] = 1
        state = self.run_once()
        self.assertIn("source byte budget", state["last_error"])
        self.assertEqual(self.adapter.builds, 0)
        self.config["code"]["index"]["max_source_bytes"] = 10000
        self.config["code"]["index"]["max_index_bytes"] = 1
        state = self.run_once()
        self.assertIn("generation byte budget", state["last_error"])
        self.assertIsNone(state["published"])

    def test_budget_block_keeps_publication_and_retries_only_changed_work(self):
        first = self.run_once()
        self.config["code"]["index"]["symbols"] = True
        self.config["code"]["index"]["max_source_bytes"] = 1
        blocked = self.run_once()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["published"], first["published"])
        identities = self.adapter.identities
        self.config["code"]["limits"] = {"max_sessions": 17}
        self.assertEqual(self.run_once()["last_result"], "limits_unchanged")
        self.assertEqual(self.adapter.identities, identities)
        self.config["code"]["index"]["symbols"] = False
        self.assertEqual(self.run_once()["status"], "ready")
        self.assertGreater(self.adapter.identities, identities)
        self.assertNotIn("budget_block", json.loads(self.state_path.read_text()))

    def test_child_storage_diagnostic_is_durable_budget_block(self):
        def quota(repo, oid, directory, runner):
            runner.run([sys.executable, "-c", "import sys;sys.stderr.write('Disk quota exceeded');sys.exit(1)"])
        self.adapter.during_build = quota
        state = self.run_once()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("quota", state["last_error"])
        builds = self.adapter.builds
        self.assertEqual(self.run_once()["last_result"], "limits_unchanged")
        self.assertEqual(self.adapter.builds, builds)

    def test_interrupted_build_is_not_automatically_repeated(self):
        from lore_mirror.code_index import _budget_fingerprint
        code = normalize_code_config(self.config["code"], data_dir=self.data)
        self.state_path.write_text(json.dumps({"schema_version": 1, "generation": 0, "published": None,
            "status": "building", "attempt_fingerprint": _budget_fingerprint(code)}))
        state = self.run_once()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("interrupted", state["last_error"])
        self.assertEqual(self.adapter.identities, 0)
        self.config["code"]["index"]["timeout_seconds"] = 1801
        self.assertEqual(self.run_once()["status"], "ready")

    def test_command_timeout_and_output_bound(self):
        def timeout(repo, oid, directory, runner):
            runner.deadline = time.monotonic() + .05
            runner.run([sys.executable, "-c", "import time;time.sleep(30)"])
        self.adapter.during_build = timeout
        self.assertIn("deadline", self.run_once()["last_error"])
        def output(repo, oid, directory, runner):
            runner.run([sys.executable, "-c", "print('x'*10000)"], output_bytes=32)
        self.adapter.during_build = output
        self.config["code"]["index"]["timeout_seconds"] = 1801
        self.assertIn("output byte limit", self.run_once()["last_error"])

    def test_caps_rechecked_during_build(self):
        def remove_cap(*args):
            self.cap_mock.stop()
        # A deterministic second guard failure, not the host's actual settings.
        calls = 0
        def cap(_):
            nonlocal calls
            calls += 1
            if calls > 6:
                raise CodeIndexError("memory cap disappeared")
            return CAP
        with mock.patch("lore_mirror.code_index._enforced_memory_limit", side_effect=cap):
            state = self.run_once()
        self.assertIn("cap disappeared", state["last_error"])
        self.assertIsNone(state["published"])

    def test_snapshot_drift_and_catalog_mismatch_fail_closed(self):
        def drift(*args):
            self._publish_code(generation=2)
        self.adapter.during_build = drift
        state = self.run_once()
        self.assertIn("generation changed", state["last_error"])
        self.config["code"]["trees"][0]["refs"].append("refs/heads/other")
        state = self.run_once()
        self.assertIn("catalog provenance", state["last_error"])

    def test_control_filename_and_sourcegraph_ignore_reject_admission(self):
        for name in ("line\nfile.c", ".sourcegraph/ignore"):
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test\n")
            git(self.work, "add", "--", name)
            git(self.work, "commit", "-m", "excluded path")
            self.oid = git(self.work, "rev-parse", "HEAD")
            git(self.repo, "fetch", str(self.work), "main:refs/heads/main")
            self._publish_code()
            state = self.run_once()
            self.assertIn("exact Git fallback required", state["last_error"])
            self.assertEqual(self.adapter.builds, 0)
            git(self.work, "rm", "--", name)
            git(self.work, "commit", "-m", "remove excluded path")

    def test_symlink_artifact_and_generation_parent_rejected(self):
        def unsafe(repo, oid, directory, runner):
            (directory / "bad.zoekt").symlink_to(self.work / "test.c")
        self.adapter.during_build = unsafe
        self.assertIn("unsafe artifact", self.run_once()["last_error"])
        self.adapter.during_build = None
        # Use a fresh root because the earlier failed generation is intentionally
        # retained for operator inspection, never confused with active data.
        other = self.root / "outside"
        other.mkdir()
        generations = self.code_root / "indexes" / "generations"
        generations.rename(generations.with_name("old-generations"))
        generations.symlink_to(other, target_is_directory=True)
        self.assertIn("symlinks", self.run_once()["last_error"])

    def test_atomic_publication_failure_preserves_last_good(self):
        from lore_mirror.code_index import atomic_json
        first = self.run_once()
        self.config["code"]["index"]["symbols"] = True
        def fail(path, value):
            if value.get("generation") == 2:
                raise OSError("simulated publication disk full")
            return atomic_json(path, value)
        with mock.patch("lore_mirror.code_index.atomic_json", side_effect=fail):
            state = self.run_once()
        self.assertEqual(state["published"], first["published"])
        self.assertEqual(json.loads(self.state_path.read_text())["published"], first["published"])

    def test_malformed_nested_code_state_is_bounded_failure(self):
        for state in ({"schema_version": 1, "generation": -1, "trees": {}},
                      {"schema_version": 1, "generation": 1, "trees": []},
                      {"schema_version": 1, "generation": 1, "trees": {"boot": []}},
                      {"schema_version": 1, "generation": 1, "trees": {"boot": {"published_refs": []}}}):
            (self.code_root / "code-state.json").write_text(json.dumps(state))
            result = self.run_once()
            self.assertEqual(result["status"], "blocked")
            self.assertIn("invalid", result["last_error"])
            self.assertNotIn("AttributeError", result["last_error"])
        self.assertEqual(self.adapter.builds, 0)

    def test_alternates_and_symlink_object_directories_rejected(self):
        info = self.repo / "objects" / "info"
        info.mkdir(exist_ok=True)
        alternate = info / "alternates"
        alternate.write_text("/not-authorized\n")
        self.assertIn("coordinator-owned", self.run_once()["last_error"])
        alternate.unlink()
        objects = self.repo / "objects"
        objects.rename(self.repo / "original-objects")
        objects.symlink_to(self.repo / "original-objects", target_is_directory=True)
        self.assertIn("coordinator-owned", self.run_once()["last_error"])
        self.assertEqual(self.adapter.builds, 0)

    def test_requested_symbols_require_verified_symbol_metadata(self):
        self.config["code"]["index"]["symbols"] = True
        with mock.patch.object(self.adapter, "verify", return_value={"engine_commit": ENGINE_COMMIT,
                "repository": "boot", "commit_oid": self.oid, "has_symbols": False}):
            result = self.run_once()
        self.assertIn("verified symbol metadata", result["last_error"])
        self.assertIsNone(result["published"])

    def test_changed_source_admission_policy_forces_rebuild(self):
        first = self.run_once()
        first["published"]["policy"].pop("admission_version")
        first["published"]["policy_fingerprint"] = hashlib.sha256(
            json.dumps(first["published"]["policy"], sort_keys=True).encode()).hexdigest()
        self.state_path.write_text(json.dumps(first))
        result = self.run_once()
        self.assertEqual(result["generation"], 2)
        self.assertEqual(self.adapter.builds, 2)
        self.assertEqual(result["published"]["policy"]["admission_version"], 1)


    def test_deeply_nested_index_state_raises_bounded_error_and_is_preserved(self):
        contents = '{"schema_version":1,"generation":1,"published":' + '[' * 2000 + '0' + ']' * 2000 + '}'
        self.state_path.write_text(contents)
        with self.assertRaisesRegex(CodeIndexError, "checkpoint"):
            self.run_once()
        self.assertEqual(self.state_path.read_text(), contents)
        self.assertEqual(self.adapter.builds, 0)

    def test_index_state_is_revalidated_after_lock_admission(self):
        from lore_mirror.code_index import _read_json
        self.run_once()
        reads = 0
        def changed(path, maximum=4 * 1024**2):
            nonlocal reads
            if path == self.state_path:
                reads += 1
                if reads == 2:
                    return {"schema_version": 1, "generation": -1, "published": None}
            return _read_json(path, maximum)
        with mock.patch("lore_mirror.code_index._read_json", side_effect=changed):
            with self.assertRaisesRegex(CodeIndexError, "checkpoint"):
                self.run_once()
        self.assertEqual(self.adapter.builds, 1)


    def test_default_adapter_checks_build_manifest_and_binaries(self):
        indexer, query, manifest = (self.root / name for name in ("indexer", "query", "manifest"))
        indexer.write_bytes(b"indexer binary")
        query.write_bytes(b"query binary")
        manifest.write_text(json.dumps({"upstream_commit": ENGINE_COMMIT,
            "indexer_sha256": hashlib.sha256(indexer.read_bytes()).hexdigest(),
            "query_bridge_sha256": "0" * 64}))
        runner = mock.Mock()
        runner.options = {"symbols": False}
        with mock.patch("lore_mirror.code_index.INDEXER_BINARY", indexer), \
             mock.patch("lore_mirror.code_index.QUERY_BINARY", query), \
             mock.patch("lore_mirror.code_index.BUILD_MANIFEST", manifest):
            with self.assertRaisesRegex(CodeIndexError, "binary identity"):
                ZoektIndexAdapter().identity(runner)
        runner.run.assert_not_called()


class CgroupAndProcessTests(unittest.TestCase):
    def test_v2_resolves_actual_nested_process_group(self):
        version, path = _cgroup_directory("0::/tenant/job\n", "10 9 0:1 /tenant /cg rw - cgroup2 cgroup rw\n")
        self.assertEqual((version, str(path)), (2, "/cg/job"))
        version, path = _cgroup_directory("0::/\n", "10 9 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
        self.assertEqual(str(path), "/sys/fs/cgroup")

    def test_unmatched_or_escaping_cgroup_is_rejected(self):
        for group in ("0::/elsewhere", "0::/tenant/../outside"):
            with self.assertRaises(CodeIndexError):
                _cgroup_directory(group, "10 9 0:1 /tenant /cg rw - cgroup2 cgroup rw\n")

    def test_v1_memory_controller_resolution(self):
        version, path = _cgroup_directory("5:cpu:/other\n7:memory:/docker/job\n",
            "10 9 0:1 /docker /memory rw - cgroup cgroup rw,memory\n")
        self.assertEqual((version, str(path)), (1, "/memory/job"))

    def test_actual_limit_reader_finite_memory_and_swap_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "job"
            group.mkdir()
            (group / "memory.max").write_text(str(512 * 1024**2))
            (group / "memory.swap.max").write_text("0")
            (group / "memory.oom.group").write_text("1")
            original = Path.read_text
            def read(path, *args, **kwargs):
                if str(path) == "/proc/self/cgroup":
                    return "0::/tenant/job\n"
                if str(path) == "/proc/self/mountinfo":
                    return f"10 9 0:1 /tenant {root} rw - cgroup2 cgroup rw\n"
                return original(path, *args, **kwargs)
            with mock.patch.object(Path, "read_text", read):
                self.assertEqual(_enforced_memory_limit(512 * 1024**2)["path"], str(group))
                for memory, swap in (("max", "0"), (str(1024**3), "0"), (str(512 * 1024**2), "max"),
                                     (str(512 * 1024**2), "1")):
                    (group / "memory.max").write_text(memory)
                    (group / "memory.swap.max").write_text(swap)
                    with self.assertRaises(CodeIndexError):
                        _enforced_memory_limit(512 * 1024**2)

    def test_v2_requires_verified_whole_group_oom_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp)
            (group / "memory.max").write_text(str(CAP["memory_bytes"]))
            (group / "memory.swap.max").write_text("0")
            with mock.patch("lore_mirror.code_index._cgroup_directory", return_value=(2, group)):
                with self.assertRaisesRegex(CodeIndexError, "memory.oom.group=1"):
                    _enforced_memory_limit(CAP["memory_bytes"])
                for value in ("0", "2"):
                    (group / "memory.oom.group").write_text(value)
                    with self.assertRaisesRegex(CodeIndexError, "memory.oom.group=1"):
                        _enforced_memory_limit(CAP["memory_bytes"])
                (group / "memory.oom.group").write_text("1")
                self.assertEqual(_enforced_memory_limit(CAP["memory_bytes"])["oom_group"], 1)

    def test_v2_allows_verified_namespace_init_with_zero_or_missing_oom_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp)
            (group / "memory.max").write_text(str(CAP["memory_bytes"]))
            (group / "memory.swap.max").write_text("0")
            with mock.patch("lore_mirror.code_index._cgroup_directory", return_value=(2, group)), \
                 mock.patch("lore_mirror.code_index._dedicated_pid_namespace_init", return_value="pid:[123]"):
                for value in (None, "0"):
                    if value is not None:
                        (group / "memory.oom.group").write_text(value)
                    cap = _enforced_memory_limit(CAP["memory_bytes"])
                    self.assertEqual(cap["oom_protection"], "pid_namespace_init")
                    self.assertEqual(cap["pid_namespace"], "pid:[123]")
                for memory, swap in (("0", "0"), ("max", "0"), (str(CAP["memory_bytes"] + 1), "0"),
                                     (str(CAP["memory_bytes"]), "1")):
                    (group / "memory.max").write_text(memory)
                    (group / "memory.swap.max").write_text(swap)
                    with self.assertRaises(CodeIndexError):
                        _enforced_memory_limit(CAP["memory_bytes"])

    def test_namespace_init_requires_actual_tini_direct_child_and_same_namespace_cgroup(self):
        import contextlib
        own = b"python\0-m\0lore_mirror\0code-index\0"
        data = {"/proc/1/cmdline": b"/usr/bin/tini\0--\0" + own,
                "/proc/self/cmdline": own,
                "/proc/1/cgroup": "0::/job\n", "/proc/self/cgroup": "0::/job\n"}
        def read(path, *args, **kwargs):
            return data[str(path)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("lore_mirror.code_index.os.getpid", return_value=7))
            ppid = stack.enter_context(mock.patch("lore_mirror.code_index.os.getppid", return_value=1))
            namespace = stack.enter_context(mock.patch("lore_mirror.code_index.os.readlink", return_value="pid:[123]"))
            samefile = stack.enter_context(mock.patch("lore_mirror.code_index.os.path.samefile", return_value=True))
            stack.enter_context(mock.patch.object(Path, "read_bytes", read))
            stack.enter_context(mock.patch.object(Path, "read_text", read))
            self.assertEqual(_dedicated_pid_namespace_init(), "pid:[123]")
            ppid.return_value = 12
            self.assertIsNone(_dedicated_pid_namespace_init())
            ppid.return_value = 1
            samefile.return_value = False
            self.assertIsNone(_dedicated_pid_namespace_init())
            samefile.return_value = True
            namespace.side_effect = ["pid:[123]", "pid:[456]"]
            self.assertIsNone(_dedicated_pid_namespace_init())
            namespace.side_effect = None
            for key, bad in (("/proc/1/cgroup", "0::/other\n"),
                             ("/proc/1/cmdline", b"/usr/bin/tini\0--\0sh\0-c\0python\0"),
                             ("/proc/1/cmdline", b"/sbin/docker-init\0--\0" + own)):
                original = data[key]
                data[key] = bad
                self.assertIsNone(_dedicated_pid_namespace_init())
                data[key] = original
            samefile.side_effect = PermissionError
            self.assertIsNone(_dedicated_pid_namespace_init())

    def test_optional_cgroup_metrics_are_lifetime_labeled_and_missing_is_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cap = dict(CAP, path=tmp)
            self.assertIsNone(_cgroup_metrics(cap)["peak_memory_bytes"])
            (root / "memory.peak").write_text("1234")
            (root / "memory.current").write_text("123")
            (root / "memory.events").write_text("oom 2\noom_kill 1\n")
            (root / "io.stat").write_text("8:0 rbytes=100 wbytes=200 rios=1\n8:1 rbytes=50 wbytes=20\n")
            result = _cgroup_metrics(cap)
            self.assertEqual(result["peak_memory_bytes"], 1234)
            self.assertEqual(result["memory_events"]["oom_kill"], 1)
            self.assertEqual(result["io_read_bytes"], 150)
            self.assertEqual(result["io_write_bytes"], 220)
            self.assertIn("lifetime", result["scope"])


    def test_v1_rejected_even_with_finite_memory_and_no_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp) / "job"
            group.mkdir()
            (group / "memory.limit_in_bytes").write_text(str(CAP["memory_bytes"]))
            original = Path.read_text
            def read(path, *args, **kwargs):
                if str(path) == "/proc/self/cgroup":
                    return "7:memory:/tenant/job\n"
                if str(path) == "/proc/self/mountinfo":
                    return f"10 9 0:1 /tenant {tmp} rw - cgroup cgroup rw,memory\n"
                return original(path, *args, **kwargs)
            with mock.patch.object(Path, "read_text", read):
                with self.assertRaises(CodeIndexError):
                    _enforced_memory_limit(CAP["memory_bytes"])
                (group / "memory.memsw.limit_in_bytes").write_text(str(CAP["memory_bytes"] + 1))
                with self.assertRaises(CodeIndexError):
                    _enforced_memory_limit(CAP["memory_bytes"])
                (group / "memory.memsw.limit_in_bytes").write_text(str(CAP["memory_bytes"]))
                with self.assertRaisesRegex(CodeIndexError, "cgroup v2 memory and swap enforcement"):
                    _enforced_memory_limit(CAP["memory_bytes"])


    def test_child_inherits_lock_after_supervisor_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lockpath, pidpath = root / "index.lock", root / "child.pid"
            config = {"limits": {"command_output_bytes": 1024}, "min_free_fraction": .30,
                "index": {"memory_bytes": CAP["memory_bytes"], "timeout_seconds": 60,
                          "max_index_bytes": 1024, "symbols": False}}
            child_script = f"import os,time;open({str(pidpath)!r},'w').write(str(os.getpid()));time.sleep(30)"
            supervisor_script = ("import fcntl,pathlib;import lore_mirror.code_index as m;"
                f"cap={CAP!r};m._enforced_memory_limit=lambda _:cap;"
                f"lock=open({str(lockpath)!r},'a');fcntl.flock(lock,fcntl.LOCK_EX);"
                f"runner=m.BuildRunner({config!r},pathlib.Path({str(root)!r}),lock.fileno(),cap);"
                f"runner.run([{sys.executable!r},'-c',{child_script!r}])")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            supervisor = subprocess.Popen([sys.executable, "-c", supervisor_script], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while not pidpath.exists() and time.monotonic() < deadline and supervisor.poll() is None:
                    time.sleep(.02)
                self.assertTrue(pidpath.exists(), "supervisor failed before child launch")
                child_pid = int(pidpath.read_text())
                supervisor.kill()
                supervisor.wait(timeout=3)
                with lockpath.open("a") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.killpg(child_pid, signal.SIGKILL)
                deadline = time.monotonic() + 3
                acquired = False
                while time.monotonic() < deadline:
                    with lockpath.open("a") as contender:
                        try:
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                            break
                        except BlockingIOError:
                            time.sleep(.02)
                self.assertTrue(acquired)
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                supervisor.wait()
                supervisor.stderr.close()
                if child_pid:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
