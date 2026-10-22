"""Offline exact-Git readers: real bare repositories, no network or checkout."""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from lore_mirror.code_backend import CodeBackend, GitResult, bounded_git_run
from lore_mirror.errors import MirrorError
from lore_mirror.code_config import code_catalog_fingerprint


def assert_mail_writer_proceeds(test, data, query):
    """Hold a real reader child active while a blocking mail-style writer runs."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        started, release = root / "reader-started", root / "reader-release"
        child = ("import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();"
                 "\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)"
                 "\nos.execvpe(sys.argv[3],sys.argv[3:],os.environ)")
        outputs, errors, inherited = [], [], []
        original = bounded_git_run
        def blocked(argv, **kwargs):
            inherited.append(kwargs.get("pass_fds", ()))
            return original([sys.executable, "-c", child, str(started), str(release), *argv], **kwargs)
        def read():
            try:
                outputs.append(query())
            except BaseException as exc:
                errors.append(exc)
        reader = threading.Thread(target=read)
        with patch("lore_mirror.code_backend.bounded_git_run", side_effect=blocked):
            reader.start()
            try:
                deadline = time.monotonic() + 3
                while not started.exists() and reader.is_alive() and time.monotonic() < deadline:
                    time.sleep(.01)
                test.assertTrue(started.exists(), errors)
                writer = ("import fcntl,sys;f=open(sys.argv[1],'rb');"
                          "fcntl.flock(f,fcntl.LOCK_EX);print('acquired')")
                acquired = subprocess.run([sys.executable, "-c", writer, str(data / "index.lock")],
                                          check=True, capture_output=True, timeout=1)
                test.assertEqual(acquired.stdout, b"acquired\n")
                test.assertTrue(reader.is_alive(), "Reader must still be blocked when mail writer finishes")
            finally:
                release.touch()
                reader.join(timeout=5)
        test.assertFalse(reader.is_alive())
        test.assertFalse(errors, errors)
        test.assertTrue(outputs)
        test.assertTrue(inherited)
        test.assertTrue(all(not descriptors for descriptors in inherited), inherited)


class CodeReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Offline Author")
        self.git("config", "user.email", "offline@example.invalid")
        (self.work / "one.txt").write_text("first needle\nsecond line\n")
        (self.work / "odd\tname\n雪.txt").write_text("odd needle\n")
        (self.work / ":(glob)*").write_text("literal magic needle\n")
        (self.work / "--help").write_text("option needle\n")
        (self.work / "binary").write_bytes(b"binary\x00needle\n")
        (self.work / "link").symlink_to("/etc/passwd")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.old = self.git("rev-parse", "HEAD").strip()
        self.git("tag", "-a", "next-20261020", "-m", "historical exact tag")
        (self.work / "one.txt").write_text("newest value\nsecond line\n")
        self.git("add", ".")
        self.git("commit", "-qm", "new tip")
        self.tip = self.git("rev-parse", "HEAD").strip()
        self.data = self.root / "mail"
        self.data.mkdir()
        (self.data / "index.lock").touch()
        self.code = self.root / "code"
        self.repo = self.code / "repositories" / "tiny.git"
        self.repo.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "--bare", "--quiet", str(self.work), str(self.repo)], check=True, capture_output=True)
        self.cfg = {"data_dir": str(self.data), "limits": {"query_output_bytes": 4 * 1024 * 1024},
                    "code": {"enabled": True, "root_dir": str(self.code),
                             "trees": [{"id": "tiny", "name": "Tiny", "default_ref": "refs/heads/main",
                                        "url": "https://git.kernel.org/pub/scm/linux/kernel/git/fixture/tiny.git",
                                        "refs": ["refs/heads/main", "refs/tags/next-20261020"], "tag_patterns": []}], "limits": {}}}
        self.publish()
        self.backend = CodeBackend(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args], stderr=subprocess.PIPE).decode()

    def publish(self, generation=1, refs=None):
        self.state = {"schema_version": 1, "generation": generation, "trees": {"tiny": {"status": "ready", "generation": generation,
                      "published_refs": refs or {"refs/heads/main": self.tip, "refs/tags/next-20261020": self.old},
                      "published_at": "2026-10-21T00:00:00Z", "fetched_at": "2026-10-21T00:00:00Z",
                      "config_fingerprint": code_catalog_fingerprint(self.cfg["code"]["trees"][0])}}}
        (self.code / "code-state.json").write_text(json.dumps(self.state))

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(MirrorError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_exact_annotated_tag_and_latest_are_distinct(self):
        old = self.backend.search("needle", "tiny", "refs/tags/next-20261020", paths=["one.txt"])
        new = self.backend.search("needle", "tiny", paths=["one.txt"])
        self.assertEqual(old["items"][0]["commit"], self.old)
        self.assertEqual(old["items"][0]["line"], 1)
        self.assertEqual(new["items"], [])
        self.assertFalse(new["partial"])

    def test_mutable_ref_move_does_not_change_published_snapshot(self):
        subprocess.run(["git", "--git-dir=" + str(self.repo), "update-ref", "refs/heads/main", self.old], check=True)
        result = self.backend.get_file("tiny", "refs/heads/main", "one.txt")
        self.assertEqual(result["metadata"]["commit"], self.tip)
        self.assertEqual(result["items"][0]["text"], "newest value")
        self.publish(2, {"refs/heads/main": self.old})
        result = self.backend.get_file("tiny", "refs/heads/main", "one.txt")
        self.assertEqual(result["metadata"]["commit"], self.old)

    def test_paths_are_literal_and_nul_parsed(self):
        for path in (":(glob)*", "--help", "odd\tname\n雪.txt"):
            with self.subTest(path=path):
                result = self.backend.search("needle", "tiny", paths=[path])
                self.assertEqual([row["path"] for row in result["items"]], [path])
                self.assertEqual(self.backend.get_file("tiny", "refs/heads/main", path)["metadata"]["path"], path)
        self.error("code_file_not_found", self.backend.get_file, "tiny", "refs/heads/main", "*")

    def test_host_traversal_and_revexpressions_rejected_without_git(self):
        with patch.object(self.backend, "_run") as run:
            for path in ("../outside", "/etc/passwd", "one/../two", "", "a\x00b", "a//b"):
                self.error("invalid_path", self.backend.get_file, "tiny", "refs/heads/main", path)
            for ref in ("--help", "HEAD~1", "refs/heads/main:one.txt", self.tip, "$(touch /tmp/pwn)"):
                self.error("code_ref_not_found", self.backend.get_file, "tiny", ref, "one.txt")
            for oid in ("HEAD", "--help", self.tip + "^", "a" * 39):
                self.error("invalid_commit", self.backend.show, "tiny", oid)
            run.assert_not_called()

    def test_search_exclusions_are_explicit(self):
        result = self.backend.search("needle", "tiny")
        self.assertTrue(result["partial"])
        self.assertEqual(result["metadata"]["exclusions"]["binary_files"], 1)
        self.assertEqual(result["metadata"]["exclusions"]["non_regular_files"], 1)
        self.assertIn("excluded_files", result["metadata"]["partial_reasons"])

    def test_binary_is_not_text_and_symlink_is_not_followed(self):
        self.error("code_binary_file", self.backend.get_file, "tiny", "refs/heads/main", "binary")
        self.assertEqual(self.backend.get_file("tiny", "refs/heads/main", "link")["items"], [{"line": 1, "text": "/etc/passwd"}])

    def test_line_ranges_newline_and_empty_file(self):
        result = self.backend.get_file("tiny", "refs/heads/main", "one.txt", 2, 2)
        self.assertEqual(result["items"], [{"line": 2, "text": "second line"}])
        self.assertTrue(result["metadata"]["final_newline"])
        self.error("invalid_line", self.backend.get_file, "tiny", "refs/heads/main", "one.txt", True)
        self.error("invalid_line", self.backend.get_file, "tiny", "refs/heads/main", "one.txt", 2, 1)

    def test_huge_file_and_line_are_explicit(self):
        self.backend.limits["max_file_bytes"] = 8
        self.error("code_file_too_large", self.backend.get_file, "tiny", "refs/heads/main", "one.txt")
        result = self.backend.search("newest", "tiny", paths=["one.txt"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["metadata"]["exclusions"]["oversize_files"], 1)
        self.backend.limits["max_file_bytes"] = 1024
        self.backend.limits["max_line_bytes"] = 5
        self.error("code_line_too_large", self.backend.get_file, "tiny", "refs/heads/main", "one.txt")
        result = self.backend.search("newest", "tiny", paths=["one.txt"])
        self.assertEqual(result["items"][0]["text"], "newes")
        self.assertTrue(result["items"][0]["text_truncated"])
        self.assertTrue(result["partial"])

    def test_output_budget_returns_partial_not_false_no_matches(self):
        self.backend.limits["query_output_bytes"] = 8
        result = self.backend.search("needle", "tiny")
        self.assertTrue(result["partial"])
        self.assertIn("query_output_limit", result["metadata"]["partial_reasons"])

    def test_large_tree_listing_leaves_grep_its_output_budget(self):
        for index in range(300):
            (self.work / f"zz-pad-{index:04d}.txt").write_text("nothing here\n")
        self.git("add", ".")
        self.git("commit", "-qm", "padding")
        tip = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work),
                        "+refs/heads/main:refs/heads/main"], check=True)
        self.publish(2, {"refs/heads/main": tip})
        listing = subprocess.check_output(["git", "--git-dir=" + str(self.repo), "ls-tree", "-r", "-z", "-l", tip])
        self.backend.limits["query_output_bytes"] = len(listing) * 3 // 4
        result = self.backend.search("needle", "tiny")
        self.assertTrue(result["partial"])
        self.assertIn("odd needle", [item["text"] for item in result["items"]])

    def test_regex_dialect_case_and_literal_defaults(self):
        self.assertEqual(len(self.backend.search("NEWEST", "tiny", paths=["one.txt"], case_sensitive=False)["items"]), 1)
        self.assertEqual(len(self.backend.search("new.*value", "tiny", paths=["one.txt"], mode="regex")["items"]), 1)
        self.assertEqual(self.backend.search("new.*value", "tiny", paths=["one.txt"])["items"], [])
        self.error("unsupported_regex", self.backend.search, "(?=new)", "tiny", mode="regex")
        self.error("symbols_unavailable", self.backend.search, "new", "tiny", mode="symbol")

    def test_log_metadata_is_bounded_exact_history(self):
        result = self.backend.log("tiny", "refs/tags/next-20261020", path="one.txt")
        self.assertEqual([row["commit"] for row in result["items"]], [self.old])
        result = self.backend.log("tiny", limit=1)
        self.assertEqual([row["commit"] for row in result["items"]], [self.tip])
        self.assertTrue(result["partial"])
        self.assertIn("commit_limit", result["metadata"]["partial_reasons"])
        self.error("invalid_date", self.backend.log, "tiny", since="yesterday; touch x")
        self.error("invalid_date", self.backend.log, "tiny", since="2026-02-31")

    def test_log_is_one_nul_framed_subprocess_for_the_scan_window(self):
        with patch.object(self.backend, "_run", wraps=self.backend._run) as run:
            result = self.backend.log("tiny", limit=1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("-z", run.call_args.args[1])
        self.assertIn("--max-count=2", run.call_args.args[1])
        self.assertEqual(result["items"][0]["subject"], "new tip")
        self.assertTrue(result["partial"])
        self.assertEqual(result["items"][0]["author"], self.backend.show("tiny", self.tip)["items"][0]["author"])

    def test_log_streams_without_whole_history_date_order(self):
        # --date-order loads every commit before printing the first one; on a
        # Linux-sized history without a commit-graph that exceeds query memory.
        with patch.object(self.backend, "_run", wraps=self.backend._run) as run:
            result = self.backend.log("tiny", limit=1)
        self.assertNotIn("--date-order", run.call_args.args[1])
        self.assertEqual(result["metadata"]["ordering"], "git_default_reverse_chronological")

    def test_log_multiline_metadata_and_complete_record_prefix(self):
        self.git("commit", "--allow-empty", "-qm", "subject with tab\tvalue\n\nbody\n" + "a" * 40 + "\n雪")
        tip = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work), "main:refs/heads/main"], check=True)
        self.publish(2, {"refs/heads/main": tip})
        result = self.backend.log("tiny")
        self.assertEqual(result["items"][0]["commit"], tip)
        self.assertIn("\n" + "a" * 40 + "\n雪", result["items"][0]["message"])
        self.assertEqual(len(result["items"]), 3)

    def test_unknown_show_oid_is_404_and_ancestry_uses_one_call(self):
        # Distinct unrelated tips must precede the sole reachable tip. Aliases
        # deduplicate to one OID and cannot detect a per-tip merge-base loop.
        root_tree = self.git("rev-parse", self.old + "^{tree}").strip()
        refs = {}
        for index in range(12):
            unrelated = self.git("commit-tree", root_tree, "-m", f"unrelated root {index}").strip()
            self.git("update-ref", f"refs/heads/unrelated-{index}", unrelated)
            refs[f"refs/heads/unrelated-{index}"] = unrelated
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work),
                        "+refs/heads/*:refs/heads/*"], check=True)
        refs["refs/heads/main"] = self.tip
        self.assertEqual(len(set(refs.values())), 13)
        self.publish(2, refs)
        with patch.object(self.backend, "_run", wraps=self.backend._run) as run:
            result = self.backend.show("tiny", self.old)
        self.assertEqual(result["items"][0]["commit"], self.old)
        ancestry = [call for call in run.call_args_list if call.args[1][0] in ("rev-list", "merge-base")]
        self.assertEqual(len(ancestry), 1)
        with self.assertRaises(MirrorError) as caught:
            self.backend.show("tiny", "0" * 40)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "code_commit_not_published")

    def test_show_reports_an_over_budget_ancestry_walk_as_unknown(self):
        original = bounded_git_run
        def walk_over_budget(argv, **kwargs):
            if "--ancestry-path" in argv:
                return GitResult(b"", -9, "query_memory_limit")
            return original(argv, **kwargs)
        self.publish(2, {"refs/heads/main": self.tip})
        with patch("lore_mirror.code_backend.bounded_git_run", side_effect=walk_over_budget):
            self.error("code_reachability_unknown", self.backend.show, "tiny", self.old)

    def test_show_rejects_blob_tree_and_tag_object_ids(self):
        for revision in ("HEAD:one.txt", "HEAD^{tree}", "refs/tags/next-20261020"):
            with self.subTest(revision=revision):
                oid = self.git("rev-parse", revision).strip()
                with self.assertRaises(MirrorError) as caught:
                    self.backend.show("tiny", oid)
                self.assertEqual(caught.exception.status, 404)

    def test_show_reaches_one_of_several_unrelated_tips(self):
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        (self.work / "other").write_text("other branch")
        self.git("add", ".")
        self.git("commit", "-qm", "unrelated root")
        unrelated = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work), "unrelated:refs/heads/unrelated"], check=True)
        self.publish(2, {"refs/heads/unrelated": unrelated, "refs/heads/main": self.tip})
        self.assertEqual(self.backend.show("tiny", self.old)["items"][0]["commit"], self.old)
        self.publish(3, {"refs/heads/unrelated": unrelated})
        self.error("code_commit_not_published", self.backend.show, "tiny", self.old)

    def test_active_git_reader_never_blocks_mail_indexer(self):
        assert_mail_writer_proceeds(self, self.data,
                                   lambda: self.backend.get_file("tiny", "refs/heads/main", "one.txt"))

    def test_show_metadata_and_diff_root_commit(self):
        result = self.backend.show("tiny", self.old)
        self.assertEqual(result["items"][0]["subject"], "initial")
        self.assertEqual(result["items"][0]["parents"], [])
        result = self.backend.show("tiny", self.old, paths=["one.txt"], part="diff")
        self.assertIn("+first needle", "\n".join(row["text"] for row in result["items"]))
        result = self.backend.show("tiny", self.tip, paths=["one.txt"], part="diff")
        self.assertEqual(result["metadata"]["parent"], self.old)
        self.assertIn("+newest value", "\n".join(row["text"] for row in result["items"]))

    def test_show_refuses_unpublished_detached_commit(self):
        self.git("checkout", "--orphan", "unpublished")
        self.git("rm", "-rf", ".")
        (self.work / "secret").write_text("not in published graph")
        self.git("add", ".")
        self.git("commit", "-qm", "unpublished secret")
        hidden = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work), "unpublished:refs/private/hidden"], check=True)
        self.error("code_commit_not_published", self.backend.show, "tiny", hidden)

    def test_guard_is_read_only_and_detects_publication(self):
        with self.backend.read_guard():
            with self.backend.read_guard():
                self.assertEqual(self.backend.generation(), "1")
        with self.assertRaises(MirrorError) as raised:
            with self.backend.read_guard():
                self.publish(2)
        self.assertEqual(raised.exception.code, "stale_code_generation")
        self.assertIsNone(self.backend.local.state)
        with (self.data / "index.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.error("busy", self.backend.search, "x", "tiny")
        (self.data / "index.lock").unlink()
        self.error("code_not_ready", self.backend.search, "x", "tiny")
        self.assertFalse((self.data / "index.lock").exists())

    def test_publication_between_capture_and_validation_is_rejected(self):
        original = self.backend._state
        calls = []
        def captured_then_changed():
            state = original()
            calls.append(state)
            if len(calls) == 1:
                self.publish(2, {"refs/heads/main": self.old})
            return state
        with patch.object(self.backend, "_state", side_effect=captured_then_changed), patch.object(self.backend, "_run") as run:
            self.error("stale_code_generation", self.backend.get_file, "tiny", "refs/heads/main", "one.txt")
            run.assert_not_called()

    def test_disabled_and_corrupt_status_do_not_launch_git(self):
        disabled = CodeBackend({"data_dir": str(self.root / "missing")})
        self.assertFalse(disabled.status()["enabled"])
        self.assertFalse((self.root / "missing").exists())
        (self.code / "code-state.json").write_text("not json")
        with patch.object(self.backend, "_run") as run:
            self.assertEqual(self.backend.status()["status"], "invalid_state")
            self.error("code_state_invalid", self.backend.search, "x", "tiny")
            run.assert_not_called()

    def test_nested_invalid_state_is_structured_not_attribute_error(self):
        for entry in (None, [], {"published_refs": []}, {"published_refs": {"refs/heads/main": "--help"}}, {"status": []}):
            state = {"schema_version": 1, "generation": 1, "trees": {"tiny": entry}}
            (self.code / "code-state.json").write_text(json.dumps(state))
            self.assertEqual(self.backend.status()["status"], "invalid_state")
            self.error("code_state_invalid", self.backend.list_trees)
            self.error("code_state_invalid", self.backend.list_refs, "tiny")

    def test_cleanup_timeout_is_structured_service_unavailable(self):
        with patch("lore_mirror.code_backend.bounded_run",
                   side_effect=subprocess.TimeoutExpired(["git"], 1)):
            with self.assertRaises(MirrorError) as caught:
                self.backend.get_file("tiny", "refs/heads/main", "one.txt")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.as_dict()["error"]["code"], "code_cleanup_timeout")

    def test_candidate_line_cap_and_next_range_are_explicit(self):
        self.backend.limits["scan_limit"] = 1
        result = self.backend.get_file("tiny", "refs/heads/main", "one.txt")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["metadata"]["next_start_line"], 2)
        self.assertIn("line_scan_limit", result["metadata"]["partial_reasons"])
        later = self.backend.get_file("tiny", "refs/heads/main", "one.txt", start_line=2)
        self.assertEqual(later["items"][0]["text"], "second line")
        self.assertFalse(later["partial"])

    def test_total_subprocess_output_not_only_individual_is_bounded(self):
        self.backend.limits["query_output_bytes"] = 200
        result = self.backend.log("tiny")
        self.assertTrue(result["partial"])
        self.assertIn("query_output_limit", result["metadata"]["partial_reasons"])

    def test_python_work_cannot_outlive_whole_query_deadline(self):
        self.backend.limits["query_timeout"] = .01
        with self.assertRaises(MirrorError) as caught:
            with self.backend.read_guard():
                time.sleep(.02)
        self.assertEqual(caught.exception.code, "query_timeout")

    def test_since_filters_without_pruning_clock_skewed_ancestors(self):
        for index, year in enumerate((2026, 1990, 2027)):
            (self.work / "skew.txt").write_text(str(index))
            self.git("add", ".")
            env = dict(os.environ, GIT_AUTHOR_DATE=f"{year}-01-01T00:00:00Z", GIT_COMMITTER_DATE=f"{year}-01-01T00:00:00Z")
            subprocess.run(["git", "-C", str(self.work), "commit", "-qm", f"clock {year}"], env=env, check=True)
        tip = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work), "main:refs/heads/main"], check=True)
        self.publish(2, {"refs/heads/main": tip})
        result = self.backend.log("tiny", since="2020-01-01")
        self.assertIn("clock 2026", [item["subject"] for item in result["items"]])
        self.assertNotIn("clock 1990", [item["subject"] for item in result["items"]])
        self.assertFalse(result["partial"])

    def test_show_can_read_published_tag_when_default_removed(self):
        self.publish(2, {"refs/tags/next-20261020": self.old})
        self.assertEqual(self.backend.show("tiny", self.old)["items"][0]["commit"], self.old)

    def test_removed_tree_does_not_make_configured_tree_ready(self):
        self.state["trees"]["removed"] = self.state["trees"].pop("tiny")
        (self.code / "code-state.json").write_text(json.dumps(self.state))
        result = self.backend.status()
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["trees"][0]["tree"], "tiny")
        self.assertEqual(result["trees"][0]["status"], "not_ready")

    def test_degraded_status_diagnostics_are_byte_bounded(self):
        self.state["trees"]["tiny"].update(status="degraded", last_error="雪" * 2000)
        (self.code / "code-state.json").write_text(json.dumps(self.state))
        result = self.backend.status()
        self.assertEqual(result["status"], "degraded")
        self.assertLessEqual(len(result["trees"][0]["last_error"].encode()), 160)
        self.assertTrue(result["trees"][0]["last_error_truncated"])

    def test_status_reports_skipped_refs_and_blocked_storage_counts(self):
        row = self.state["trees"]["tiny"]
        row.update(retry_blocked=True, blocked_reason="storage", blocked_config_fingerprint="a" * 64, next_check_at=None,
                   skipped_refs={"refs/tags/data": {"object_id": "b" * 40, "object_type": "blob",
                                                    "reason": "peeled target is not a commit"}},
                   measurements={"object_pack_count": 3, "packed_ref_count": 4,
                                 "generation_pin_count": 2, "loose_ref_count": 0})
        (self.code / "code-state.json").write_text(json.dumps(self.state))
        status = self.backend.status()["trees"][0]
        self.assertEqual(status["skipped_ref_count"], 1)
        self.assertEqual(status["skipped_refs"][0]["object_type"], "blob")
        self.assertTrue(status["retry_blocked"])
        self.assertEqual(status["blocked_reason"], "storage")
        self.assertEqual(status["storage_counts"]["object_pack_count"], 3)

    def test_skipped_optional_tag_is_known_but_default_is_still_required(self):
        row = self.state["trees"]["tiny"]
        diagnostic = {"object_id": "b" * 40, "object_type": "blob", "reason": "peeled target is not a commit"}
        del row["published_refs"]["refs/tags/next-20261020"]
        row["skipped_refs"] = {"refs/tags/next-20261020": diagnostic}
        (self.code / "code-state.json").write_text(json.dumps(self.state))
        self.assertTrue(self.backend.status()["ready"])
        del row["published_refs"]["refs/heads/main"]
        row["skipped_refs"]["refs/heads/main"] = diagnostic
        (self.code / "code-state.json").write_text(json.dumps(self.state))
        status = self.backend.status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["trees"][0]["missing_requested_ref_count"], 1)


    def test_catalog_drift_never_relabels_old_repository(self):
        for key, value in (("url", "https://git.kernel.org/pub/scm/linux/kernel/git/fixture/other.git"),
                           ("refs", ["refs/heads/main"]), ("tag_patterns", [{"pattern": "refs/tags/next-*", "keep": 2}])):
            cfg = json.loads(json.dumps(self.cfg))
            cfg["code"]["trees"][0][key] = value
            reader = CodeBackend(cfg)
            self.error("code_catalog_changed", reader.search, "needle", "tiny")
            self.error("code_catalog_changed", reader.get_file, "tiny", "refs/heads/main", "one.txt")
            self.error("code_catalog_changed", reader.show, "tiny", self.tip)
            self.error("code_catalog_changed", reader.list_refs, "tiny")
            self.assertFalse(reader.status()["ready"])
            self.assertFalse(reader.status()["available"])
            self.assertEqual(reader.list_trees()["items"][0]["status"], "catalog_changed")

    def test_status_discovers_configured_ids_before_bootstrap(self):
        (self.code / "code-state.json").unlink()
        (self.data / "index.lock").unlink()
        result = self.backend.status()
        self.assertEqual(result["trees"][0]["tree"], "tiny")
        self.assertEqual(result["trees"][0]["default_ref"], "refs/heads/main")
        self.assertEqual(result["trees"][0]["status"], "not_ready")
        self.assertFalse((self.data / "index.lock").exists())


    def test_maximum_degraded_catalog_status_fits_transport(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["code"]["trees"] = []
        state = {"schema_version": 1, "generation": 1, "trees": {}}
        ref = "refs/heads/" + "a" * 500
        for i in range(32):
            tree = dict(self.cfg["code"]["trees"][0], id=f"tree{i}" + "x" * 57, default_ref=ref, refs=[ref])
            cfg["code"]["trees"].append(tree)
            state["trees"][tree["id"]] = {"status": "degraded", "published_refs": {ref: self.tip},
                                         "last_error": "雪" * 2000, "config_fingerprint": code_catalog_fingerprint(tree)}
        (self.code / "code-state.json").write_text(json.dumps(state))
        result = CodeBackend(cfg).status()
        self.assertEqual(len(result["trees"]), 32)
        self.assertTrue(result["status_details_truncated"])
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), (65536 - 1024) // 3)
        self.assertTrue(result["trees"][0]["default_ref_truncated"])

    def test_retired_state_rows_remain_bounded_by_bytes_not_current_count(self):
        for index in range(33):
            self.state["trees"][f"retired{index}"] = dict(self.state["trees"]["tiny"])
        (self.code / "code-state.json").write_text(json.dumps(self.state))
        self.assertTrue(self.backend.status()["ready"])
        self.assertEqual(len(self.backend.status()["trees"]), 1)


    def test_missing_requested_tree_or_ref_is_not_ready(self):
        cfg = json.loads(json.dumps(self.cfg))
        other = dict(cfg["code"]["trees"][0], id="other")
        cfg["code"]["trees"].append(other)
        reader = CodeBackend(cfg)
        self.assertFalse(reader.status()["ready"])
        self.assertTrue(reader.status()["available"])
        self.assertEqual(reader.status()["status"], "degraded")
        self.publish(2, {"refs/heads/main": self.tip})
        self.assertFalse(self.backend.status()["ready"])
        self.assertTrue(self.backend.status()["available"])

    def test_malformed_commit_cannot_inject_diff_options(self):
        tree_oid = self.git("rev-parse", "HEAD^{tree}").strip()
        marker = self.root / "must-not-exist"
        raw = (f"tree {tree_oid}\nparent --output={marker}\nauthor Offline <offline@example.invalid> 1 +0000\n"
               "committer Offline <offline@example.invalid> 1 +0000\n\nmalformed\n").encode()
        oid = subprocess.check_output(["git", "--git-dir=" + str(self.repo), "hash-object", "-t", "commit", "--literally", "-w", "--stdin"], input=raw).decode().strip()
        self.publish(2, {"refs/heads/main": oid})
        self.error("code_objects_invalid", self.backend.show, "tiny", oid, part="diff")
        self.assertFalse(marker.exists())

    def test_empty_file_and_gitlink_never_follow_filesystem(self):
        (self.work / "empty").touch()
        self.git("add", "empty")
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.old + ",module")
        self.git("commit", "-qm", "empty and gitlink")
        tip = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work), "main:refs/heads/main"], check=True)
        self.publish(2, {"refs/heads/main": tip})
        empty = self.backend.get_file("tiny", "refs/heads/main", "empty")
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["metadata"]["total_lines"], 0)
        self.error("code_not_blob", self.backend.get_file, "tiny", "refs/heads/main", "module")
        result = self.backend.search("needle", "tiny", paths=["module"])
        self.assertEqual(result["metadata"]["exclusions"]["non_regular_files"], 1)


    def test_merge_diff_is_explicit_first_parent(self):
        self.git("checkout", "-qb", "topic")
        (self.work / "topic.txt").write_text("topic change\n")
        self.git("add", ".")
        self.git("commit", "-qm", "topic")
        self.git("checkout", "-q", "main")
        (self.work / "main.txt").write_text("main change\n")
        self.git("add", ".")
        self.git("commit", "-qm", "main work")
        first_parent = self.git("rev-parse", "HEAD").strip()
        self.git("merge", "--no-ff", "-qm", "merge topic", "topic")
        merge = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(self.repo), "fetch", "--quiet", str(self.work), "main:refs/heads/main"], check=True)
        self.publish(2, {"refs/heads/main": merge})
        result = self.backend.show("tiny", merge, part="diff")
        self.assertEqual(result["metadata"]["parent"], first_parent)
        self.assertEqual(result["metadata"]["merge_policy"], "first_parent")
        diff = "\n".join(row["text"] for row in result["items"])
        self.assertIn("+topic change", diff)
        self.assertNotIn("+main change", diff)


    def test_no_external_diff_textconv_or_ambient_git_config(self):
        marker = self.root / "executed"
        executable = self.root / "helper"
        executable.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        executable.chmod(0o755)
        subprocess.run(["git", "--git-dir=" + str(self.repo), "config", "diff.external", str(executable)], check=True)
        subprocess.run(["git", "--git-dir=" + str(self.repo), "config", "core.fsmonitor", str(executable)], check=True)
        with patch.dict(os.environ, {"GIT_EXTERNAL_DIFF": str(executable), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "diff.external", "GIT_CONFIG_VALUE_0": str(executable), "GIT_CONFIG": str(executable)}):
            reader = CodeBackend(self.cfg)
            reader.show("tiny", self.tip, part="diff")
            reader.search("newest", "tiny")
        self.assertFalse(marker.exists())
        self.assertEqual(reader.env["GIT_ALLOW_PROTOCOL"], "")
        self.assertEqual(reader.env["GIT_LITERAL_PATHSPECS"], "1")
        self.assertNotIn("GIT_EXTERNAL_DIFF", reader.env)


class GitBudgetTests(unittest.TestCase):
    def run_bounded(self, script, **kwargs):
        return bounded_git_run([sys.executable, "-c", script], env={"PATH": os.environ.get("PATH", "")}, timeout=kwargs.get("timeout", 5),
                               max_bytes=kwargs.get("max_bytes", 8192), memory_bytes=kwargs.get("memory_bytes", 128 * 1024 * 1024))

    def test_stdout_is_prefix_bounded(self):
        result = self.run_bounded("import os;\nwhile True: os.write(1,b'x'*4096)")
        self.assertEqual(len(result.output), 8192)
        self.assertEqual(result.partial_reason, "query_output_limit")

    def test_stderr_is_bounded(self):
        result = self.run_bounded("import os;\nwhile True: os.write(2,b'x'*4096)")
        self.assertEqual(result.partial_reason, "query_stderr_limit")

    def test_timeout_and_memory_kill_process(self):
        started = time.monotonic()
        result = self.run_bounded("import time;time.sleep(10)", timeout=.05)
        self.assertEqual(result.partial_reason, "query_timeout")
        self.assertLess(time.monotonic() - started, 2)
        result = self.run_bounded("import time;x=bytearray(32*1024*1024);time.sleep(10)", memory_bytes=16*1024*1024)
        self.assertEqual(result.partial_reason, "query_memory_limit")


if __name__ == "__main__":
    unittest.main()
