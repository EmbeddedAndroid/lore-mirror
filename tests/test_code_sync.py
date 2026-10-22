"""Offline tiny repositories only; production URLs never use a local override."""
import fcntl
import datetime as dt
import gzip
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from lore_mirror.code_config import normalize_code_config
from lore_mirror.sync import tree_bytes
from lore_mirror.code_sync import (CodeSyncError, CodeSyncBlocked, CodeSyncDeadline, GitMirrorAdapter, GitRunner, STAGING,
    _retry_fingerprint, _selected_refs, _stop, _trusted_url, sync_code_once)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


class FixtureAdapter(GitMirrorAdapter):
    """A test-only file transport; no corresponding configuration switch exists."""
    def __init__(self, source):
        self.source = str(source)
        self.probes = 0
        self.fetches = 0
        self.manifest_calls = []
        self.fingerprint = "a" * 40
        self.interrupt = False

    def remote_refs(self, tree, runner):
        self.probes += 1
        return runner.git(["-c", "protocol.file.allow=always", "ls-remote", "--refs", "--",
            self.source, *tree["refs"], *[p["pattern"] for p in tree["tag_patterns"]],
            *[p["pattern"] for p in tree.get("branch_patterns", [])]])

    def fetch(self, tree, refs, runner):
        self.fetches += 1
        runner.git(["-c", "protocol.file.allow=always", "fetch", "--atomic", "--no-tags",
            "--no-recurse-submodules", "--no-write-fetch-head", "--no-auto-maintenance", "--",
            self.source, *[f"+{ref}:{STAGING}{ref[5:]}" for ref in refs]])
        if self.interrupt:
            raise KeyboardInterrupt("simulated stop after fetch")

    def manifest(self, url, headers, limit, timeout):
        self.manifest_calls.append(dict(headers))
        if headers.get("If-None-Match") == self.fingerprint:
            return 304, b"", {"ETag": self.fingerprint}
        content = {"/pub/scm/linux/kernel/git/test/test.git": {"fingerprint": self.fingerprint}}
        return 200, gzip.compress(json.dumps(content).encode()), {"ETag": self.fingerprint}


class CodeSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "upstream"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.name", "Offline Test")
        git(self.source, "config", "user.email", "offline@example.invalid")
        (self.source / "hello.txt").write_text("first\n")
        git(self.source, "add", "hello.txt")
        git(self.source, "commit", "-m", "first")
        self.first = git(self.source, "rev-parse", "HEAD")
        git(self.source, "tag", "-a", "v1.0", "-m", "first release")
        (self.source / "hello.txt").write_text("second\n")
        git(self.source, "commit", "-am", "second")
        self.second = git(self.source, "rev-parse", "HEAD")
        git(self.source, "branch", "next")
        self.data = self.root / "data"
        self.config = {"data_dir": str(self.data), "code": {"enabled": True, "trees": [{
            "id": "boot", "url": "https://git.u-boot-project.org/u-boot/u-boot.git",
            "default_ref": "refs/heads/main", "refs": ["refs/heads/main", "refs/heads/next"],
            "tag_patterns": [{"pattern": "refs/tags/v*", "keep": None}]}]}}
        self.adapter = FixtureAdapter(self.source)
        self.state_path = self.data / "code" / "code-state.json"
        self.repo = self.data / "code" / "repositories" / "boot.git"

    def run_once(self, now=10000, **kwargs):
        return sync_code_once(self.config, now=now, adapter=self.adapter, **kwargs)

    def test_disabled_has_no_files_or_network(self):
        self.config.pop("code")
        self.assertEqual(self.run_once()["status"], "disabled")
        self.assertFalse(self.data.exists())
        self.assertEqual(self.adapter.fetches, 0)

    def test_full_bare_history_peeled_tag_and_no_change(self):
        state = self.run_once()
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "ready", row.get("last_error"))
        self.assertEqual(state["generation"], 1)
        self.assertEqual(row["published_refs"], {"refs/heads/main": self.second,
            "refs/heads/next": self.second, "refs/tags/v1.0": self.first})
        self.assertEqual(git(self.repo, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(git(self.repo, "rev-list", "--count", self.second), "2")
        self.assertFalse((self.repo / "shallow").exists())
        self.assertFalse((self.repo / "FETCH_HEAD").exists())
        self.assertNotIn("remote", (self.repo / "config").read_text())
        again = self.run_once(14000)
        self.assertEqual(again["generation"], 1)
        self.assertEqual(self.adapter.probes, 2)
        self.assertEqual(self.adapter.fetches, 1)
        self.assertEqual(again["trees"]["boot"]["last_result"], "unchanged")
        self.assertEqual(again["trees"]["boot"]["fetched_at"], row["fetched_at"])

    def test_manifest_etag_avoids_ref_negotiation_and_fetch(self):
        self.config["code"]["trees"][0]["url"] = "https://git.kernel.org/pub/scm/linux/kernel/git/test/test.git"
        first = self.run_once()
        self.assertEqual(first["trees"]["boot"]["status"], "ready", first)
        again = self.run_once(14000)
        self.assertEqual(again["generation"], 1)
        self.assertEqual(self.adapter.probes, 1)
        self.assertEqual(self.adapter.fetches, 1)
        self.assertEqual(self.adapter.manifest_calls, [{}, {"If-None-Match": "a" * 40}])
        # A changed whole-repository fingerprint still skips fetching if the
        # configured ref subset did not change.
        self.adapter.fingerprint = "b" * 40
        self.run_once(18000)
        self.assertEqual(self.adapter.probes, 2)
        self.assertEqual(self.adapter.fetches, 1)

    def test_force_update_keeps_old_pins_and_exact_ref_map(self):
        self.run_once()
        git(self.source, "reset", "--hard", self.first)
        state = self.run_once(14000)
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "ready", row)
        self.assertEqual(row["published_refs"]["refs/heads/main"], self.first)
        self.assertEqual(state["generation"], 2)
        self.assertEqual(row["ref_changes"]["moved"], ["refs/heads/main"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/1/heads/main"), self.second)
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/2/heads/main"), self.first)

    def test_deleted_required_ref_preserves_previous_generation(self):
        first = self.run_once()
        git(self.source, "update-ref", "-d", "refs/heads/next")
        state = self.run_once(14000)
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "degraded")
        self.assertIn("refs/heads/next", row["last_error"])
        self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertEqual(state["generation"], 1)
        self.assertEqual(self.adapter.fetches, 1)
        self.run_once(14001)
        self.assertEqual(self.adapter.probes, 2, "backoff must prevent immediate retries")

    def test_missing_branch_initially_is_blocked_without_substitution(self):
        self.config["code"]["trees"][0]["refs"].append("refs/heads/missing")
        row = self.run_once()["trees"]["boot"]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["published_refs"], {})
        self.assertEqual(self.adapter.fetches, 0)

    def test_ref_change_between_probe_and_fetch_not_published(self):
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        original = self.adapter.fetch
        def raced(tree, refs, runner):
            git(self.source, "reset", "--hard", self.second)
            return original(tree, refs, runner)
        with mock.patch.object(self.adapter, "fetch", side_effect=raced):
            state = self.run_once(14000)
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["trees"]["boot"]["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertIn("changed during fetch", state["trees"]["boot"]["last_error"])

    def test_interruption_after_fetch_resumes_without_publishing_partial_map(self):
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        self.adapter.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.run_once(14000)
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["generation"], 1)
        self.assertEqual(saved["trees"]["boot"]["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertEqual(saved["trees"]["boot"]["status"], "fetching")
        self.adapter.interrupt = False
        state = self.run_once(14001)
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["trees"]["boot"]["published_refs"]["refs/heads/main"], self.first)

    def test_atomic_publication_storage_failure_preserves_refs_and_blocks_retry(self):
        from lore_mirror.code_sync import atomic_json
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        def fail_publication(path, value):
            if Path(path) == self.state_path and value["generation"] == 2:
                raise OSError(errno.ENOSPC, "simulated disk full at publication")
            return atomic_json(path, value)
        with mock.patch("lore_mirror.code_sync.atomic_json", side_effect=fail_publication):
            blocked = self.run_once(14000)
        self.assertEqual(blocked["trees"]["boot"]["status"], "blocked")
        self.assertTrue(blocked["trees"]["boot"]["retry_blocked"])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["trees"]["boot"]["published_refs"], first["trees"]["boot"]["published_refs"])
        probes = self.adapter.probes
        self.run_once(999999)
        self.assertEqual(self.adapter.probes, probes)
        self.config["code"]["limits"] = {"repository_bytes": 9 * 1024 ** 3}
        state = self.run_once(1000000)
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["trees"]["boot"]["status"], "ready")

    def test_deeply_nested_checkpoint_is_rejected_and_preserved(self):
        self.state_path.parent.mkdir(parents=True)
        original = "[" * 2000 + "0" + "]" * 2000
        self.state_path.write_text(original)
        with self.assertRaisesRegex(CodeSyncError, "invalid code checkpoint"):
            self.run_once()
        self.assertEqual(self.state_path.read_text(), original)
        self.assertEqual(self.adapter.probes, 0)

    def test_corrupt_checkpoint_is_not_overwritten(self):
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text("not json")
        with self.assertRaisesRegex(CodeSyncError, "checkpoint"):
            self.run_once()
        self.assertEqual(self.state_path.read_text(), "not json")
        self.assertEqual(self.adapter.probes, 0)

    def test_shared_heavy_lock_defers_and_recovers(self):
        self.data.mkdir()
        with (self.data / "index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = self.run_once()
            self.assertEqual(state["status"], "waiting_for_io")
            self.assertEqual(self.adapter.fetches, 0)
        state = self.run_once()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(self.adapter.fetches, 1)

    def test_one_due_tree_per_call_and_yield_before_work(self):
        other = dict(self.config["code"]["trees"][0], id="other",
            url="https://git.u-boot-project.org/u-boot/custodians/u-boot-snapdragon.git")
        self.config["code"]["trees"].append(other)
        waiting = self.run_once(should_yield=lambda: True)
        self.assertEqual(waiting["status"], "queued")
        self.assertEqual(self.adapter.fetches, 0)
        one = self.run_once()
        self.assertEqual(self.adapter.fetches, 1)
        self.assertEqual(one["trees"]["other"]["status"], "queued")
        self.assertEqual(one["status"], "partial")
        two = self.run_once(10001)
        self.assertEqual(self.adapter.fetches, 2)
        self.assertEqual(two["trees"]["other"]["status"], "ready")

    def test_recent_tags_bounded_and_retained_generation_pins(self):
        self.config["code"]["trees"][0]["tag_patterns"] = [{"pattern": "refs/tags/next-*", "keep": 2}]
        for name in ("next-20261019", "next-20261020", "next-20261021"):
            git(self.source, "tag", name, self.first)
        first = self.run_once()
        refs = first["trees"]["boot"]["published_refs"]
        self.assertNotIn("refs/tags/next-20261019", refs)
        self.assertEqual(refs["refs/tags/next-20261020"], self.first)
        git(self.source, "tag", "next-20261022", self.second)
        state = self.run_once(14000)
        self.assertNotIn("refs/tags/next-20261020", state["trees"]["boot"]["published_refs"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/1/tags/next-20261020"), self.first)

    def test_release_branch_patterns_choose_numeric_versions_per_prefix(self):
        tree = {"refs": ["refs/heads/for-next"], "tag_patterns": [],
                "index_refs": ["refs/heads/for-next"],
                "branch_patterns": [{"pattern": "refs/heads/" + prefix + "-for-*", "keep": 1}
                                    for prefix in ("arm64", "drivers", "clk")]}
        names = ["refs/heads/for-next"] + ["refs/heads/" + prefix + "-for-" + version
            for prefix in ("arm64", "drivers", "clk")
            for version in ("7.9", "7.10", "7.10.2", "7.10.12", "99.0-rc1", "99.0-fixes", "next", "7_11")]
        raw = "".join("a" * 40 + "\t" + ref + "\n" for ref in reversed(names)).encode()
        selected = _selected_refs(raw, tree, 100)
        self.assertEqual(set(selected), {"refs/heads/for-next"} | {
            "refs/heads/" + prefix + "-for-7.10.12" for prefix in ("arm64", "drivers", "clk")})
        self.assertEqual(tree["index_refs"], ["refs/heads/for-next"])

    def test_release_branch_pattern_without_numeric_release_fails(self):
        tree = {"refs": ["refs/heads/main"], "tag_patterns": [],
                "branch_patterns": [{"pattern": "refs/heads/drivers-for-*", "keep": 1}]}
        for suffix in (None, "7.10-rc1", "next", "7.10-fixes"):
            raw = "a" * 40 + "\trefs/heads/main\n"
            if suffix:
                raw += "b" * 40 + "\trefs/heads/drivers-for-" + suffix + "\n"
            with self.assertRaisesRegex(CodeSyncError, "release branch pattern missing.*drivers-for"):
                _selected_refs(raw.encode(), tree, 100)

    def test_release_branch_equivalent_numeric_spelling_has_stable_tiebreak(self):
        tree = {"refs": [], "tag_patterns": [], "branch_patterns": [
            {"pattern": "refs/heads/drivers-for-*", "keep": 1}]}
        names = ["refs/heads/drivers-for-07.10", "refs/heads/drivers-for-7.10"]
        for order in (names, names[::-1]):
            raw = "".join("a" * 40 + "\t" + ref + "\n" for ref in order).encode()
            self.assertEqual(list(_selected_refs(raw, tree, 100)), ["refs/heads/drivers-for-7.10"])

    def test_production_ref_probe_includes_only_configured_branch_patterns(self):
        tree = {"url": "https://git.kernel.org/pub/scm/linux/kernel/git/qcom/linux.git",
                "refs": ["refs/heads/for-next"], "tag_patterns": [], "branch_patterns": [
                    {"pattern": "refs/heads/drivers-for-*", "keep": 1}]}
        runner = mock.Mock()
        GitMirrorAdapter().remote_refs(tree, runner)
        runner.git.assert_called_once_with(["ls-remote", "--refs", "--", tree["url"],
            "refs/heads/for-next", "refs/heads/drivers-for-*"])

    def test_release_rollover_mirrors_newest_and_preserves_prior_pins(self):
        tree = self.config["code"]["trees"][0]
        tree["branch_patterns"] = [{"pattern": "refs/heads/drivers-for-*", "keep": 1}]
        tree["index_refs"] = ["refs/heads/main"]
        git(self.source, "branch", "drivers-for-7.9", self.first)
        first = self.run_once()
        self.assertEqual(first["trees"]["boot"]["status"], "ready", first)
        git(self.source, "branch", "drivers-for-7.10", self.second)
        git(self.source, "branch", "drivers-for-99.0-rc1", self.second)
        state = self.run_once(14000)
        self.assertEqual(state["trees"]["boot"]["status"], "ready", state)
        refs = state["trees"]["boot"]["published_refs"]
        self.assertIn("refs/heads/drivers-for-7.10", refs)
        self.assertNotIn("refs/heads/drivers-for-7.9", refs)
        self.assertNotIn("refs/heads/drivers-for-99.0-rc1", refs)
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/1/heads/drivers-for-7.9"), self.first)
        self.assertEqual(tree["index_refs"], ["refs/heads/main"])

    def test_missing_release_pattern_preserves_previous_generation(self):
        first = self.run_once()
        self.config["code"]["trees"][0]["branch_patterns"] = [
            {"pattern": "refs/heads/drivers-for-*", "keep": 1}]
        state = self.run_once(14000)
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "degraded")
        self.assertIn("release branch pattern missing", row["last_error"])
        self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertEqual(state["generation"], first["generation"])
        self.assertEqual(self.adapter.fetches, 1)

    def test_repository_budget_fails_closed(self):
        self.config["code"]["limits"] = {"repository_bytes": 1}
        state = self.run_once()
        self.assertEqual(state["trees"]["boot"]["status"], "blocked")
        self.assertIn("storage budget", state["trees"]["boot"]["last_error"])
        self.assertEqual(state["generation"], 0)
        self.assertTrue(state["trees"]["boot"]["retry_blocked"])
        self.config["code"]["limits"]["repository_bytes"] = 1024 ** 3
        recovered = self.run_once(10001)
        self.assertEqual(recovered["trees"]["boot"]["status"], "ready", recovered)

    def test_killed_fetch_leftovers_do_not_accumulate_into_a_storage_block(self):
        self.run_once()
        pack = self.repo / "objects" / "pack"
        self.config["code"]["limits"] = {"repository_bytes": tree_bytes(self.repo)["apparent"] + 3 * 1024 ** 2}
        for attempt in range(1, 4):
            with (pack / f"tmp_pack_killed{attempt}").open("wb") as leftover:
                leftover.truncate(2 * 1024 ** 2)
            row = self.run_once(10000 + attempt * 4000)["trees"]["boot"]
            self.assertEqual(row["status"], "ready", row)
            self.assertEqual(list(pack.glob("tmp_*")), [])

    def test_partial_initialization_retries_only_with_persisted_ownership(self):
        original = GitRunner.git
        for stage in ("directory_only", "config_only"):
            with self.subTest(stage=stage):
                fixture = CodeSyncTests(methodName="runTest")
                fixture.setUp()
                try:
                    marker = fixture.data / "code" / "state" / "initializing-boot.git.json"
                    binding = fixture.data / "code" / "state" / "upstream-boot.json"
                    def incomplete_init(runner, args, **kwargs):
                        if args[0] == "init":
                            self.assertTrue(marker.is_file())
                            self.assertTrue(binding.is_file())
                            if stage == "directory_only":
                                (runner.repo / "config").unlink()
                            raise CodeSyncBlocked("code repository storage budget exceeded", "storage")
                        return original(runner, args, **kwargs)
                    with mock.patch.object(GitRunner, "git", incomplete_init):
                        blocked = fixture.run_once()
                    self.assertEqual(blocked["trees"]["boot"]["status"], "blocked")
                    self.assertFalse((fixture.repo / "HEAD").exists())
                    self.assertTrue(marker.is_file())
                    original_binding = binding.read_bytes()
                    # Unchanged policy still suppresses retry, even after restart.
                    self.assertEqual(fixture.run_once(999999)["trees"]["boot"]["status"], "blocked")
                    self.assertEqual(fixture.adapter.fetches, 0)
                    fixture.config["code"]["limits"] = {"repository_bytes": 9 * 1024 ** 3}
                    recovered = fixture.run_once(1000000)
                    self.assertEqual(recovered["trees"]["boot"]["status"], "ready", recovered)
                    self.assertEqual(recovered["trees"]["boot"]["published_refs"]["refs/heads/main"], fixture.second)
                    self.assertFalse(marker.exists())
                    self.assertEqual(binding.read_bytes(), original_binding)
                finally:
                    fixture.doCleanups()

    def test_unmarked_partial_repository_is_not_adopted(self):
        self.repo.mkdir(parents=True)
        code = normalize_code_config(self.config["code"], data_dir=self.data)
        runner = GitRunner(self.repo, self.data / "code", code)
        with self.assertRaisesRegex(CodeSyncError, "not coordinator-owned"):
            runner.prepare()
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertFalse((self.data / "code" / "state" / "initializing-boot.git.json").exists())

    def test_untrusted_repo_config_never_used(self):
        self.run_once()
        with (self.repo / "config").open("a") as stream:
            stream.write("[url \"file:///tmp/\"]\n insteadOf = https://\n")
        state = self.run_once(14000)
        self.assertIn("not coordinator-owned", state["trees"]["boot"]["last_error"])
        self.assertEqual(self.adapter.probes, 1)

    def test_command_output_deadline_and_environment_guards(self):
        self.run_once()
        code = normalize_code_config(self.config["code"], data_dir=self.data)
        runner = GitRunner(self.repo, self.data / "code", code)
        runner.limits["command_output_bytes"] = 16
        with self.assertRaisesRegex(CodeSyncError, "output exceeds"):
            runner.git(["for-each-ref"])
        runner.limits["command_output_bytes"] = 1024 * 1024
        runner.deadline = time.monotonic() + .05
        with self.assertRaisesRegex(CodeSyncError, "deadline"):
            runner.git(["-c", "alias.pause=!sleep 2", "pause"])
        runner.deadline = time.monotonic() + 30
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "bad.key",
                                          "GIT_CONFIG_VALUE_0": "bad"}):
            self.assertEqual(runner.git(["rev-parse", "--is-bare-repository"]).strip(), b"true")

    def test_custom_code_root_creates_shared_data_lock_parent(self):
        separate = self.root / "separate-code"
        self.config["code"]["root_dir"] = str(separate)
        state = self.run_once()
        self.assertEqual(state["status"], "ready", state)
        self.assertTrue((self.data / "index.lock").is_file())
        self.assertTrue((separate / "code-state.json").is_file())

    def test_state_byte_admission_preserves_previous_checkpoint(self):
        self.run_once()
        previous = self.state_path.read_bytes()
        with mock.patch("lore_mirror.code_sync.MAX_STATE_BYTES", len(previous) + 32):
            state = self.run_once(14000)
        self.assertEqual(state["trees"]["boot"]["status"], "blocked")
        self.assertIn("checkpoint byte limit", state["trees"]["boot"]["last_error"])
        # A smaller blocked journal can fit even if the fetching journal cannot;
        # no corrupt or oversized JSON is ever advertised.
        self.assertLessEqual(len(self.state_path.read_bytes()), len(previous) + 32)
        self.assertEqual(json.loads(self.state_path.read_text())["generation"], 1)

    def test_orphan_helper_killed_even_when_git_leader_has_exited(self):
        pidfile = self.root / "grandchild.pid"
        script = ("import os,time; p=os.fork(); "
                  "open(" + repr(str(pidfile)) + ", 'w').write(str(p)) if p else None; "
                  "os._exit(0) if p else time.sleep(30)")
        child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait(timeout=3)
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        grandchild = int(pidfile.read_text())
        self.assertGreater(grandchild, 0)
        _stop(child)
        for _ in range(100):
            stat = Path(f"/proc/{grandchild}/stat")
            if not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z "):
                break
            time.sleep(.01)
        else:
            self.fail("helper survived completed parent and cleanup")

    def test_manifest_expansion_limit_and_invalid_304_preserve_last_good(self):
        self.config["code"]["trees"][0]["url"] = "https://git.kernel.org/pub/scm/linux/kernel/git/test/test.git"
        first = self.run_once()
        with mock.patch.object(self.adapter, "manifest", return_value=(200, gzip.compress(b" " * 4097), {})):
            self.config["code"]["limits"] = {"manifest_bytes": 1024, "manifest_expanded_bytes": 4096}
            state = self.run_once(14000)
        self.assertIn("expanded byte limit", state["trees"]["boot"]["last_error"])
        self.assertEqual(state["generation"], first["generation"])
        for cache in (self.data / "code" / "state").glob("manifest-*.json"):
            cache.unlink()
        self.config["code"]["limits"]["manifest_expanded_bytes"] = 8192
        with mock.patch.object(self.adapter, "manifest", return_value=(304, b"", {})):
            state = self.run_once(18000)
        self.assertIn("304 without", state["trees"]["boot"]["last_error"])

    def test_lock_is_released_between_tree_units(self):
        other = dict(self.config["code"]["trees"][0], id="other",
            url="https://git.u-boot-project.org/u-boot/custodians/u-boot-snapdragon.git")
        self.config["code"]["trees"].append(other)
        checks = []
        def should_yield():
            with (self.data / "index.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checks.append(True)
            return False
        self.run_once(should_yield=should_yield)
        state = self.run_once(should_yield=should_yield)
        self.assertEqual(state["status"], "ready")
        self.assertEqual({row["status"] for row in state["trees"].values()}, {"ready"})
        self.assertEqual(len(checks), 2)

    def test_sampled_rss_and_disk_growth_guards(self):
        self.run_once()
        code = normalize_code_config(self.config["code"], data_dir=self.data)
        runner = GitRunner(self.repo, self.data / "code", code)
        runner.limits["max_fetch_bytes"] = 16
        (self.repo / "test-growth").write_bytes(b"x" * 17)
        with self.assertRaisesRegex(CodeSyncError, "added-disk"):
            runner.git(["rev-parse", "--is-bare-repository"])
        (self.repo / "test-growth").unlink()
        with mock.patch("lore_mirror.code_sync._aggregate_rss", return_value=code["limits"]["fetch_memory_bytes"] + 1):
            with self.assertRaisesRegex(CodeSyncError, "RSS limit"):
                runner.git(["-c", "alias.pause=!sleep 2", "pause"])


    def test_candidate_admission_failure_keeps_pins_map_and_backoff(self):
        from lore_mirror.code_sync import _check_state_size
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        def reject_candidate(value, *, reserve=0):
            if reserve:
                raise CodeSyncBlocked("code checkpoint byte limit exceeded; reduce catalog scope")
            return _check_state_size(value)
        with mock.patch("lore_mirror.code_sync._check_state_size", side_effect=reject_candidate):
            state = self.run_once(14000)
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["trees"]["boot"]["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertIn("checkpoint byte limit", state["trees"]["boot"]["last_error"])
        self.assertEqual(git(self.repo, "for-each-ref", "refs/lore/generations/2/"), "")
        probes = self.adapter.probes
        self.run_once(14001)
        self.assertEqual(self.adapter.probes, probes)

    def test_configuration_change_is_due_immediately_and_never_false_ready(self):
        self.run_once()
        self.config["code"]["trees"][0]["refs"].append("refs/heads/absent")
        state = self.run_once(10001)
        self.assertEqual(state["status"], "degraded")
        self.assertIn("missing", state["trees"]["boot"]["last_error"])
        self.assertEqual(self.adapter.probes, 2)
        self.run_once(10002)
        self.assertEqual(self.adapter.probes, 2, "changed config must still respect failed-job backoff")

    def test_changed_upstream_requires_new_identity(self):
        first = self.run_once()
        self.config["code"]["trees"][0]["url"] = "https://git.u-boot-project.org/u-boot/other.git"
        state = self.run_once(10001)
        self.assertEqual(state["status"], "degraded")
        self.assertIn("upstream changed", state["trees"]["boot"]["last_error"])
        self.assertEqual(state["trees"]["boot"]["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertEqual(self.adapter.probes, 1)

    def test_retired_rows_removed_but_provenance_and_git_pins_survive(self):
        self.run_once()
        original = dict(self.config["code"]["trees"][0])
        self.config["code"]["trees"] = [dict(original, id="other")]
        state = self.run_once(14000)
        self.assertEqual(set(state["trees"]), {"other"})
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/1/heads/main"), self.second)
        self.config["code"]["trees"] = [dict(original, url="https://git.u-boot-project.org/u-boot/other.git")]
        state = self.run_once(18000)
        self.assertEqual(state["status"], "degraded")
        self.assertIn("binding changed", state["trees"]["boot"]["last_error"])
        self.assertEqual(self.adapter.probes, 2)


    def test_linux_tree_tag_and_blob_tag_are_skipped_and_reported(self):
        tree_oid = git(self.source, "rev-parse", self.first + "^{tree}")
        blob_oid = git(self.source, "rev-parse", self.first + ":hello.txt")
        # Linux's historical v2.6.11 names a tree, not a commit. Cover both an
        # annotated tree tag and a lightweight blob tag without external I/O.
        git(self.source, "tag", "-a", "v2.6.11", tree_oid, "-m", "historical tree")
        git(self.source, "tag", "vblob", blob_oid)
        state = self.run_once()
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "ready", row)
        self.assertEqual(row["published_refs"]["refs/tags/v1.0"], self.first)
        self.assertNotIn("refs/tags/v2.6.11", row["published_refs"])
        self.assertEqual(row["skipped_refs"]["refs/tags/v2.6.11"]["object_type"], "tree")
        self.assertEqual(row["skipped_refs"]["refs/tags/v2.6.11"]["object_id"], tree_oid)
        self.assertEqual(row["skipped_refs"]["refs/tags/vblob"]["object_type"], "blob")
        self.assertIn("refs/tags/v2.6.11", row["source_refs"])
        self.assertEqual(git(self.repo, "for-each-ref", "refs/lore/generations/1/tags/v2.6.11"), "")
        again = self.run_once(14000)
        self.assertEqual(again["generation"], 1)
        self.assertEqual(self.adapter.fetches, 1, "unchanged non-commit tags must not cause refetch loops")
        self.assertEqual(again["trees"]["boot"]["skipped_refs"], row["skipped_refs"])

    def test_noncommit_default_preserves_previous_snapshot_and_blocks_until_config_change(self):
        git(self.source, "tag", "vdefault", self.first)
        config_tree = self.config["code"]["trees"][0]
        config_tree["default_ref"] = "refs/tags/vdefault"
        config_tree["refs"].append("refs/tags/vdefault")
        first = self.run_once()
        tree_oid = git(self.source, "rev-parse", self.second + "^{tree}")
        git(self.source, "tag", "-f", "vdefault", tree_oid)
        state = self.run_once(14000)
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blocked_reason"], "non_commit")
        self.assertIsNone(row["next_check_at"])
        self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertEqual(git(self.repo, "show", self.first + ":hello.txt"), "first")
        self.assertEqual(row["skipped_refs"]["refs/tags/vdefault"]["object_type"], "tree")
        attempts = self.adapter.fetches
        self.run_once(999999)
        self.assertEqual(self.adapter.fetches, attempts)
        config_tree["default_ref"] = "refs/heads/main"
        changed = self.run_once(1000000)
        self.assertEqual(changed["trees"]["boot"]["status"], "ready")
        self.assertNotIn("refs/tags/vdefault", changed["trees"]["boot"]["published_refs"])

    def test_tag_keep_version_sort_orders_numeric_versions_and_release_candidates(self):
        tree = normalize_code_config(self.config["code"], data_dir=self.data)["trees"][0]
        tags = ["v6.9", "v6.10-rc2", "v6.10-rc10", "v6.10", "v6.11-rc1"]
        raw = "".join(self.first + "\t" + ref + "\n" for ref in tree["refs"])
        raw += "".join(self.first + "\trefs/tags/" + tag + "\n" for tag in reversed(tags))
        for keep in range(1, len(tags) + 1):
            tree["tag_patterns"] = [{"pattern": "refs/tags/v*", "keep": keep}]
            selected = _selected_refs(raw.encode(), tree, 100)
            self.assertEqual({ref[10:] for ref in selected if ref.startswith("refs/tags/")}, set(tags[-keep:]))

    def test_published_pins_are_packed_and_storage_counts_survive_restarts(self):
        first = self.run_once()
        metrics = first["trees"]["boot"]["measurements"]
        self.assertTrue((self.repo / "packed-refs").is_file())
        self.assertEqual(metrics["generation_pin_count"], 3)
        self.assertEqual(metrics["packed_ref_count"], 3)
        self.assertEqual(metrics["loose_ref_count"], 0)
        self.assertGreaterEqual(metrics["object_pack_count"], 1)
        git(self.source, "reset", "--hard", self.first)
        second = self.run_once(14000)
        self.assertEqual(second["trees"]["boot"]["measurements"]["generation_pin_count"], 6)
        self.assertEqual(second["trees"]["boot"]["measurements"]["loose_ref_count"], 0)
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/1/heads/main"), self.second)
        restarted = self.run_once(18000)
        self.assertEqual(restarted["trees"]["boot"]["measurements"]["generation_pin_count"], 6)

    def test_budget_block_is_durable_and_relevant_limit_change_retries_immediately(self):
        self.run_once()
        self.config["code"]["limits"] = {"repository_bytes": 1}
        state = self.run_once(14000)
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "blocked")
        self.assertTrue(row["retry_blocked"])
        blocked_signature = row["blocked_config_fingerprint"]
        calls = (self.adapter.probes, self.adapter.fetches)
        self.run_once(999999)
        self.assertEqual((self.adapter.probes, self.adapter.fetches), calls)
        # A query-only change does not repeatedly unblock a mirror budget error.
        self.config["code"]["limits"]["query_timeout"] = 16
        self.run_once(1000000)
        self.assertEqual((self.adapter.probes, self.adapter.fetches), calls)
        self.config["code"]["limits"]["repository_bytes"] = 1024 ** 3
        recovered = self.run_once(1000001)["trees"]["boot"]
        self.assertEqual(recovered["status"], "ready", recovered)
        self.assertFalse(recovered["retry_blocked"])
        self.assertIsNone(recovered["blocked_config_fingerprint"])
        self.assertTrue(blocked_signature)
        self.assertEqual(self.adapter.probes, calls[0] + 1)

    def test_fetch_storage_error_blocks_without_automatic_retry(self):
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        with mock.patch.object(self.adapter, "fetch", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            state = self.run_once(14000)
        row = state["trees"]["boot"]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blocked_reason"], "storage")
        self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertIsNone(row["next_check_at"])
        probes = self.adapter.probes
        self.run_once(999999)
        self.assertEqual(self.adapter.probes, probes)

    def test_fetch_deadlines_remain_transient_with_day_capped_backoff(self):
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)

        def deadline(tree, refs, runner):
            # Exercise actual runner deadline classification, not merely a
            # generic injected exception that could hide permanent blocking.
            runner.deadline = time.monotonic() - 1
            runner.git(["fetch", "--help"])

        now = 14000
        delays = [60, 120, 240, 480, 960, 1920, 3840, 7680, 15360,
                  30720, 61440, 86400, 86400, 86400]
        with mock.patch.object(self.adapter, "fetch", side_effect=deadline) as fetch:
            for count, delay in enumerate(delays, 1):
                with self.subTest(attempt=count, expected_delay=delay):
                    failed = self.run_once(now)
                    row = failed["trees"]["boot"]
                    self.assertEqual(fetch.call_count, count)
                    self.assertEqual(row["status"], "degraded", row)
                    self.assertFalse(row["retry_blocked"])
                    self.assertIsNone(row["blocked_reason"])
                    self.assertIsNone(row["blocked_config_fingerprint"])
                    self.assertEqual(row["consecutive_errors"], count)
                    self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
                    self.assertEqual(failed["generation"], first["generation"])
                    checked = dt.datetime.fromisoformat(row["checked_at"]).timestamp()
                    due = dt.datetime.fromisoformat(row["next_check_at"]).timestamp()
                    self.assertAlmostEqual(due - checked, delay, places=5)
                    # Each invocation reloads the durable checkpoint. A wake
                    # before due must not spend another full fetch deadline.
                    with mock.patch.object(self.adapter, "remote_refs") as probe:
                        self.run_once(due - 1)
                    probe.assert_not_called()
                    self.assertEqual(fetch.call_count, count)
                    now = due
        recovered = self.run_once(now)["trees"]["boot"]
        self.assertEqual(recovered["status"], "ready", recovered)
        self.assertEqual(recovered["consecutive_errors"], 0)
        self.assertEqual(recovered["published_refs"]["refs/heads/main"], self.first)

    def test_subprocess_deadline_recovers_and_success_resets_backoff(self):
        self.run_once()
        git(self.source, "reset", "--hard", self.first)

        def deadline(tree, refs, runner):
            runner.deadline = time.monotonic() + .05
            runner.git(["-c", "alias.pause=!sleep 2", "pause"])

        with mock.patch.object(self.adapter, "fetch", side_effect=deadline):
            failed = self.run_once(14000)["trees"]["boot"]
        self.assertFalse(failed["retry_blocked"], failed)
        self.assertEqual(failed["consecutive_errors"], 1)
        recovered = self.run_once(14100)["trees"]["boot"]
        self.assertEqual(recovered["status"], "ready", recovered)
        self.assertEqual(recovered["consecutive_errors"], 0)
        git(self.source, "reset", "--hard", self.second)
        with mock.patch.object(self.adapter, "fetch", side_effect=deadline):
            failed_again = self.run_once(18000)["trees"]["boot"]
        self.assertFalse(failed_again["retry_blocked"], failed_again)
        self.assertEqual(failed_again["consecutive_errors"], 1)
        checked = dt.datetime.fromisoformat(failed_again["checked_at"]).timestamp()
        due = dt.datetime.fromisoformat(failed_again["next_check_at"]).timestamp()
        self.assertAlmostEqual(due - checked, 60, places=5)

    def test_hard_budget_after_deadline_still_blocks_on_first_failure(self):
        self.run_once()
        git(self.source, "reset", "--hard", self.first)
        with mock.patch.object(self.adapter, "fetch", side_effect=CodeSyncDeadline("code job deadline exceeded")):
            self.run_once(14000)
        with mock.patch.object(self.adapter, "fetch", side_effect=CodeSyncBlocked("code job aggregate RSS limit exceeded")):
            blocked = self.run_once(14100)["trees"]["boot"]
        self.assertTrue(blocked["retry_blocked"], blocked)
        self.assertEqual(blocked["blocked_reason"], "budget")
        self.assertIsNone(blocked["next_check_at"])

    def test_mixed_transient_failures_remain_paced_without_durable_block(self):
        self.run_once()
        git(self.source, "reset", "--hard", self.first)
        failures = [CodeSyncDeadline("code job deadline exceeded"),
                    CodeSyncError("temporary connection failure"),
                    CodeSyncDeadline("code job deadline exceeded")]
        with mock.patch.object(self.adapter, "fetch", side_effect=failures):
            self.run_once(14000)
            transient = self.run_once(14100)["trees"]["boot"]
            self.assertFalse(transient["retry_blocked"])
            self.assertEqual(transient["consecutive_errors"], 2)
            repeated = self.run_once(14400)["trees"]["boot"]
        self.assertFalse(repeated["retry_blocked"])
        self.assertIsNone(repeated["blocked_reason"])
        self.assertEqual(repeated["consecutive_errors"], 3)
        checked = dt.datetime.fromisoformat(repeated["checked_at"]).timestamp()
        due = dt.datetime.fromisoformat(repeated["next_check_at"]).timestamp()
        self.assertAlmostEqual(due - checked, 240, places=5)

    def test_repeated_manifest_deadlines_stay_transient_without_probing(self):
        self.config["code"]["trees"][0]["url"] = "https://git.kernel.org/pub/scm/linux/kernel/git/test/test.git"
        first = self.run_once()
        with mock.patch.object(self.adapter, "manifest", side_effect=CodeSyncDeadline("code manifest deadline exceeded")):
            with mock.patch.object(self.adapter, "remote_refs") as probe:
                for count, now in enumerate((14000, 14100, 14400), 1):
                    failed = self.run_once(now)
                    row = failed["trees"]["boot"]
                    self.assertFalse(row["retry_blocked"], row)
                    self.assertEqual(row["consecutive_errors"], count)
                    self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
                    self.assertEqual(failed["generation"], first["generation"])
        probe.assert_not_called()

    def test_publication_verification_deadline_does_not_probe_upstream(self):
        self.run_once()
        with mock.patch.object(GitRunner, "commit_map", side_effect=CodeSyncDeadline("code job deadline exceeded")):
            with mock.patch.object(self.adapter, "remote_refs") as probe:
                failed = self.run_once(14000)["trees"]["boot"]
        probe.assert_not_called()
        self.assertFalse(failed["retry_blocked"])
        self.assertEqual(failed["consecutive_errors"], 1)

    def test_invalid_advertised_ref_is_rejected_through_sync_before_fetch(self):
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        # These pass the old traversal-only ref check. Keep valid configured
        # refs present so missing-ref handling cannot mask the syntax defect.
        for index, ref in enumerate(("refs/tags/v1.2.lock/child", "refs/tags/v1/.hidden", "refs/tags/v1.")):
            with self.subTest(ref=ref):
                advertised = (self.first + "\trefs/heads/main\n" + self.second + "\trefs/heads/next\n"
                              + self.first + "\t" + ref + "\n").encode()
                with mock.patch.object(self.adapter, "remote_refs", return_value=advertised):
                    with mock.patch.object(self.adapter, "fetch") as fetch:
                        state = self.run_once(14000 + index * 4000)
                row = state["trees"]["boot"]
                self.assertEqual(row["status"], "degraded", row)
                self.assertEqual(row["last_error"], "invalid upstream ref advertisement")
                fetch.assert_not_called()
                self.assertEqual(state["generation"], first["generation"])
                self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
                self.assertEqual(git(self.repo, "for-each-ref", STAGING), "")

    def test_interrupted_journal_recovers_on_yield_only_pass_without_refetch(self):
        for published in (False, True):
            with self.subTest(has_last_good=published):
                if published:
                    self.run_once(20000)
                    git(self.source, "reset", "--hard", self.first)
                now = 24000 if published else 10000
                with mock.patch.object(self.adapter, "fetch", side_effect=KeyboardInterrupt("fetch interrupted")):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_once(now)
                interrupted = json.loads(self.state_path.read_text())
                self.assertEqual(interrupted["trees"]["boot"]["status"], "fetching")
                self.assertIn("pending_generation", interrupted["trees"]["boot"])
                # No per-tree success/failure path can mask missing startup
                # recovery: the scheduler yields before any repository work.
                with mock.patch.object(self.adapter, "fetch") as fetch:
                    with mock.patch.object(self.adapter, "remote_refs") as probe:
                        recovered = self.run_once(now + 1, should_yield=lambda: True)
                fetch.assert_not_called()
                probe.assert_not_called()
                row = recovered["trees"]["boot"]
                self.assertEqual(row["status"], "degraded" if published else "queued")
                self.assertEqual(recovered["status"], "degraded" if published else "queued")
                self.assertEqual(row["last_error"], "previous code job interrupted")
                self.assertIsNone(row["next_check_at"])
                self.assertNotIn("pending_generation", row)
                self.assertEqual(recovered["generation"], interrupted["generation"])
                self.assertEqual(row["published_refs"], interrupted["trees"]["boot"]["published_refs"])
                self.assertEqual(json.loads(self.state_path.read_text()), recovered)

    def test_interrupted_staging_is_reconciled_without_refetch_on_unchanged_map(self):
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        fetch_complete = self.adapter.fetch

        def interrupted(tree, refs, runner):
            runner.git(["update-ref", STAGING + "heads/interrupted-only", self.first])
            fetch_complete(tree, refs, runner)
            runner.git(["update-ref", "refs/heads/operator-keep", self.first])
            runner.git(["update-ref", "refs/lore/staging-other/keep", self.first])
            raise KeyboardInterrupt("interrupted before publication")

        with mock.patch.object(self.adapter, "fetch", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_once(14000)
        self.assertEqual(json.loads(self.state_path.read_text())["trees"]["boot"]["status"], "fetching")
        self.assertEqual(git(self.repo, "rev-parse", STAGING + "heads/interrupted-only"), self.first)
        # The upstream returns to the published map before recovery. Fetching
        # would mask missing recovery by clearing staging in its normal path.
        git(self.source, "reset", "--hard", self.second)
        with mock.patch.object(self.adapter, "fetch") as fetch:
            with mock.patch.object(self.adapter, "remote_refs", wraps=self.adapter.remote_refs) as probe:
                recovered = self.run_once(14001)
        fetch.assert_not_called()
        probe.assert_called_once()
        row = recovered["trees"]["boot"]
        self.assertEqual(row["status"], "ready", row)
        self.assertEqual(row["last_result"], "unchanged")
        self.assertEqual(recovered["generation"], first["generation"])
        self.assertEqual(row["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertEqual(row["fetched_at"], first["trees"]["boot"]["fetched_at"])
        self.assertNotIn("pending_generation", row)
        self.assertEqual(git(self.repo, "for-each-ref", STAGING), "")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/operator-keep"), self.first)
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/staging-other/keep"), self.first)
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/1/heads/main"), self.second)
        self.assertEqual(git(self.repo, "show", self.second + ":hello.txt"), "second")
        self.assertEqual(json.loads(self.state_path.read_text()), recovered)

    def test_interrupted_partial_fetch_retains_snapshot_and_retries_clean_staging(self):
        first = self.run_once()
        git(self.source, "reset", "--hard", self.first)
        partial = self.repo / "objects" / "pack" / "tmp_pack_interrupted"
        def interrupted(tree, refs, runner):
            # Model an interruption with incomplete object data and private ref
            # work already present, not just a stop after a completed fetch.
            partial.write_bytes(b"PACK" + b"incomplete fixture" * 64)
            runner.git(["update-ref", STAGING + "heads/unpublished", self.first])
            raise KeyboardInterrupt("simulated interruption during fetch")
        with mock.patch.object(self.adapter, "fetch", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_once(14000)
        interrupted_state = json.loads(self.state_path.read_text())
        self.assertEqual(interrupted_state["generation"], first["generation"])
        self.assertEqual(interrupted_state["trees"]["boot"]["published_refs"], first["trees"]["boot"]["published_refs"])
        self.assertEqual(git(self.repo, "show", self.second + ":hello.txt"), "second")
        self.assertEqual(git(self.repo, "rev-parse", "refs/lore/generations/1/heads/main"), self.second)
        recovered = self.run_once(14001)
        self.assertEqual(recovered["trees"]["boot"]["status"], "ready", recovered)
        self.assertEqual(recovered["trees"]["boot"]["published_refs"]["refs/heads/main"], self.first)
        self.assertEqual(git(self.repo, "for-each-ref", STAGING), "")
        self.assertFalse(partial.exists(), "the next unit drops temporary packs left by the interruption")

    def test_bad_urls_and_malformed_remote_refs_rejected(self):
        for url in ("file:///tmp/repo", "http://git.kernel.org/repo.git", "https://evil.invalid/repo.git",
                    "https://git.kernel.org@evil.invalid/x", "https://git.kernel.org:444/x"):
            with self.assertRaises(CodeSyncError):
                _trusted_url(url)
        tree = normalize_code_config(self.config["code"], data_dir=self.data)["trees"][0]
        for raw in (b"bad\n", ("a" * 40 + "\trefs/heads/../../evil\n").encode()):
            with self.assertRaises(CodeSyncError):
                _selected_refs(raw, tree, 100)


if __name__ == "__main__":
    unittest.main()
