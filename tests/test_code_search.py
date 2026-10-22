"""Offline boundary tests for the optional, exact-snapshot Zoekt adapter.

The bridge fixture tests the Python protocol boundary. The pinned real-engine
bridge and index lifecycle have their own offline integration tests.
"""
from __future__ import annotations

import hashlib
import fcntl
import tempfile
import json
import os
import subprocess
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from tests import test_code_backend as fixtures
from lore_mirror.code_backend import CodeBackend, GitResult, bounded_git_run
from lore_mirror.code_search import CodeSearchBackend, ENGINE_COMMIT
from lore_mirror.errors import MirrorError


class IndexedSearchTests(unittest.TestCase):
    def test_cleanup_timeout_does_not_fall_back_to_git(self):
        failure = MirrorError("code_cleanup_timeout", "Cleanup deadline exceeded", 503)
        with patch.object(self.backend, "_run_bridge", side_effect=failure):
            with self.assertRaises(MirrorError) as caught:
                self.backend.search("newest", "tiny")
        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.status, 503)

    def setUp(self):
        self.fixture = fixtures.CodeReaderTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.code, self.cfg = self.fixture.root, self.fixture.code, self.fixture.cfg
        self.cfg["code"]["index"] = {"enabled": True, "pilot_tree": "tiny", "pilot_ref": "refs/heads/main", "symbols": False, "max_index_bytes": 1048576}
        self.response_file = self.root / "response.json"
        self.bridge = self.root / "lore-zoekt"
        self.bridge.write_text(f"#!{sys.executable}\nimport json,sys\nrequest=json.load(sys.stdin)\nprint(open({str(self.response_file)!r}).read())\n")
        self.bridge.chmod(0o755)
        self.manifest = self.root / "zoekt-build.json"
        self.build = {"upstream_commit": ENGINE_COMMIT, "indexer_sha256": "b" * 64,
                      "query_bridge_sha256": hashlib.sha256(self.bridge.read_bytes()).hexdigest()}
        self.manifest.write_text(json.dumps(self.build))
        self.directory = self.code / "indexes/generations/test-generation"
        self.directory.mkdir(parents=True)
        shard = self.directory / "tiny.zoekt"
        shard.write_bytes(b"fake shard for protocol boundary only")
        self.publication = {"tree": "tiny", "ref": "refs/heads/main", "commit_oid": self.fixture.tip,
                            "code_generation": 1, "catalog_fingerprint": self.fixture.state["trees"]["tiny"]["config_fingerprint"],
                            "engine_commit": ENGINE_COMMIT, "indexer_sha256": self.build["indexer_sha256"],
                            "query_bridge_sha256": self.build["query_bridge_sha256"], "directory": "indexes/generations/test-generation",
                            "shards": [{"name": shard.name, "bytes": shard.stat().st_size, "sha256": hashlib.sha256(shard.read_bytes()).hexdigest()}],
                            "published_at": "2026-10-21T00:00:00Z", "symbols": False, "has_symbols": False, "unsafe_paths": 0,
                            "coverage": {"max_file_bytes": 1048576, "max_trigram_count": 20000, "partial": True,
                                         "exclusions": {"oversized_files": 0, "symlinks": 1, "submodules": 0, "binary_generated_or_non_utf8": "engine policy; not counted"}},
                            "policy": {"admission_version": 1, "max_file_bytes": 1048576, "max_trigram_count": 20000, "symbols": False, "ctags_sha256": None}}
        self.publish()
        self.response = {"engine_commit": ENGINE_COMMIT, "commit": self.fixture.tip, "has_symbols": False,
                         "matches": [{"path": "one.txt", "line": 1, "text": "newest value", "commit": self.fixture.tip, "text_truncated": False}],
                         "truncated": False, "partial_reasons": []}
        self.respond()
        self.backend = CodeBackend(self.cfg)
        self.adapter = CodeSearchBackend(self.cfg, bridge_path=self.bridge, build_manifest_path=self.manifest)
        self.backend.search_backend = self.adapter

    def publish(self, generation=1):
        self.publication["policy_fingerprint"] = hashlib.sha256(json.dumps(self.publication["policy"], sort_keys=True).encode()).hexdigest()
        (self.code / "index-state.json").write_text(json.dumps({"schema_version": 1, "generation": generation, "status": "ready", "published": self.publication}))

    def respond(self):
        self.response_file.write_text(json.dumps(self.response))

    def search(self, **kwargs):
        return self.backend.search(kwargs.pop("query", "newest"), "tiny", paths=kwargs.pop("paths", ["one.txt"]), **kwargs)

    def fallback(self, reason, **kwargs):
        result = self.search(**kwargs)
        self.assertEqual(result["metadata"]["engine"], "git")
        self.assertEqual(result["metadata"]["index_fallback_reason"], reason)
        return result

    def test_exact_literal_uses_verified_index_and_git_blob_identity(self):
        result = self.search()
        self.assertEqual(result["metadata"]["engine"], "zoekt")
        self.assertEqual(result["metadata"]["engine_commit"], ENGINE_COMMIT)
        self.assertEqual(result["metadata"]["code_index_generation"], 1)
        self.assertEqual(result["items"][0]["commit"], self.fixture.tip)
        self.assertEqual(result["items"][0]["blob"], self.fixture.git("rev-parse", "HEAD:one.txt").strip())
        self.assertTrue(result["partial"])
        self.assertIn("index_excluded_source_files", result["metadata"]["partial_reasons"])

    def test_active_zoekt_reader_never_blocks_mail_indexer(self):
        fixtures.assert_mail_writer_proceeds(self, self.fixture.data, self.search)

    def test_typed_json_request_preserves_query_without_engine_syntax(self):
        query = 'repo:evil OR content:"newest"; $(touch nope)'
        with patch.object(self.backend, "_run_bridge", wraps=self.backend._run_bridge) as runner:
            self.search(query=query)
            argv, raw, budget = runner.call_args.args
        self.assertEqual(argv, [str(self.bridge)])
        request = json.loads(raw)
        self.assertEqual(request["pattern"], query)
        self.assertEqual(request["mode"], "literal")
        self.assertEqual(request["paths"], ["one.txt"])
        self.assertEqual(request["commit"], self.fixture.tip)
        self.assertEqual(request["max_bytes"], budget)

    def test_regex_and_casefold_keep_existing_git_semantics(self):
        with patch.object(self.backend, "_run_bridge", side_effect=AssertionError("must not run bridge")):
            self.fallback("regex_dialect_git_only", query="new.*value", mode="regex")
            self.fallback("case_folding_git_only", query="NEWEST", case_sensitive=False)

    def test_snapshot_selection_never_substitutes_another_ref(self):
        result = self.fallback("index_scope_mismatch", query="needle", ref="refs/tags/next-20261020")
        self.assertEqual(result["items"][0]["commit"], self.fixture.old)
        self.publication["commit_oid"] = self.fixture.old
        self.publish()
        self.fallback("index_oid_stale")

    def test_catalog_engine_policy_mismatches_fall_back(self):
        for key, value, reason in (("catalog_fingerprint", "f" * 64, "index_catalog_stale"),
                                   ("engine_commit", "f" * 40, "index_engine_mismatch"),
                                   ("symbols", True, "index_symbols_policy_stale")):
            original = self.publication[key]
            self.publication[key] = value
            self.publish()
            self.fallback(reason)
            self.publication[key] = original
        self.publication["policy"]["max_trigram_count"] = 7
        self.publish()
        self.fallback("index_source_policy_mismatch")

    def test_missing_corrupt_or_disabled_index_remains_exact_git(self):
        (self.code / "index-state.json").unlink()
        self.fallback("index_absent")
        (self.code / "index-state.json").write_text("not json")
        self.fallback("index_state_invalid")
        self.cfg["code"]["index"]["enabled"] = False
        self.fallback("index_disabled")

    def test_missing_or_changed_binary_is_never_executed(self):
        self.bridge.write_text("#!/bin/sh\nexit 99\n")
        with patch.object(self.backend, "_run_bridge", side_effect=AssertionError("unverified executable")):
            self.fallback("index_bridge_identity_mismatch")
        self.bridge.unlink()
        self.fallback("index_bridge_unavailable")

    def test_build_manifest_must_bind_both_executables(self):
        self.build["indexer_sha256"] = "c" * 64
        self.manifest.write_text(json.dumps(self.build))
        self.fallback("index_build_identity_mismatch")
        self.manifest.write_text(json.dumps({"upstream_commit": "0" * 40}))
        self.fallback("index_build_manifest_invalid")

    def test_generation_directory_and_shards_are_literal_regular_files(self):
        original = self.publication["directory"]
        for directory in ("/etc", "indexes/generations/../../elsewhere", "indexes/generations/x/sub"):
            self.publication["directory"] = directory
            self.publish()
            self.fallback("index_directory_invalid")
        self.publication["directory"] = original
        self.publish()
        (self.directory / "unexpected.zoekt").write_text("extra")
        self.fallback("index_shards_invalid")
        (self.directory / "unexpected.zoekt").unlink()
        shard = self.directory / "tiny.zoekt"
        shard.unlink()
        shard.symlink_to(self.response_file)
        self.fallback("index_shards_invalid")

    def test_index_generation_directory_symlink_is_rejected(self):
        alternate = self.code / "indexes/generations/alias"
        alternate.symlink_to(self.directory, target_is_directory=True)
        self.publication["directory"] = "indexes/generations/alias"
        self.publish()
        self.fallback("index_directory_invalid")

    def test_result_identity_and_schema_are_checked(self):
        self.response["commit"] = self.fixture.old
        self.respond()
        self.fallback("index_result_identity_mismatch")
        self.response["commit"] = self.fixture.tip
        for field, value in (("path", "../secret"), ("line", True), ("line", -1), ("commit", self.fixture.old), ("text", "hidden\x00"), ("text_truncated", "false")):
            item = dict(self.response["matches"][0])
            self.response["matches"][0][field] = value
            self.respond()
            self.fallback("index_result_invalid")
            self.response["matches"][0] = item

    def test_result_path_must_match_literal_scope_and_exact_git_tree(self):
        self.response["matches"][0]["path"] = "one.txt.extra"
        self.respond()
        self.fallback("index_result_invalid")
        self.response["matches"][0]["path"] = "missing.txt"
        self.respond()
        self.fallback("index_git_path_mismatch", paths=None)

    def test_duplicated_matches_are_rejected_not_miscounted(self):
        self.response["matches"].append(dict(self.response["matches"][0]))
        self.respond()
        self.fallback("index_result_invalid")

    def test_health_never_claims_serving_without_a_current_publication(self):
        code_state = self.fixture.state
        def state(**fields):
            (self.code / "index-state.json").write_text(json.dumps({"schema_version": 1, "generation": 1, **fields}))
            return self.adapter.health(code_state)
        served = state(status="degraded", published=self.publication, last_error="later build failed")
        self.assertEqual(served["status"], "ready")
        self.assertIn("serving the last matching", served["last_error"])
        absent = state(status="error", last_error="cannot resolve current-process memory cgroup")
        self.assertEqual(absent["status"], "absent")
        self.assertEqual(absent["last_error"], "Last index build failed; exact Git search is used")
        code_state = {"trees": {"tiny": {"published_refs": {"refs/heads/main": self.fixture.old}}}}
        stale = state(status="degraded", published=self.publication, last_error="later build failed")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["last_error"], "Last index build failed; exact Git search is used")

    def test_engine_partial_and_clipping_flags_are_preserved(self):
        self.response["truncated"] = True
        self.response["partial_reasons"] = ["match_limit"]
        self.respond()
        result = self.search()
        self.assertTrue(result["partial"])
        self.assertIn("match_limit", result["metadata"]["partial_reasons"])
        self.assertEqual(result["metadata"]["truncated_selection"], "engine_ranked")
        self.response["truncated"] = False
        self.respond()
        self.fallback("index_result_invalid")

    def test_symlink_hits_are_excluded_using_exact_git_mode(self):
        self.response["matches"][0].update(path="link", text="/etc/passwd")
        self.respond()
        result = self.search(query="passwd", paths=None)
        self.assertEqual(result["metadata"]["engine"], "zoekt")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["metadata"]["excluded_indexed_hits"], 1)
        self.assertIn("indexed_hits_excluded", result["metadata"]["partial_reasons"])

    def test_unsafe_source_paths_force_full_git_fallback(self):
        self.publication["unsafe_paths"] = 1
        self.publish()
        self.fallback("index_unsupported_source_paths")
        self.publication["unsafe_paths"] = 0
        self.publish()
        self.fallback("index_path_encoding_unsupported", query="needle", paths=["odd\tname\n雪.txt"])

    def test_publication_changes_invalidate_code_generation_and_guard(self):
        self.assertEqual(self.backend.generation(), "1:index:1")
        self.publish(2)
        self.assertEqual(self.backend.generation(), "1:index:2")
        with self.assertRaises(MirrorError) as raised:
            with self.backend.read_guard():
                self.publish(3)
        self.assertEqual(raised.exception.code, "stale_code_generation")

    def test_symbol_mode_never_degrades_to_fulltext(self):
        with patch.object(self.backend, "_entries", side_effect=AssertionError("no text fallback")):
            with self.assertRaises(MirrorError) as raised:
                self.search(mode="symbol")
        self.assertEqual(raised.exception.code, "symbols_unavailable")

    def test_optional_symbols_require_verified_ctags_and_engine_metadata(self):
        self.cfg["code"]["index"]["symbols"] = True
        self.publication.update(symbols=True, has_symbols=True, ctags_sha256="c" * 64)
        self.publication["policy"].update(symbols=True, ctags_sha256="c" * 64)
        self.build["ctags_sha256"] = "c" * 64
        self.manifest.write_text(json.dumps(self.build))
        self.response["has_symbols"] = True
        self.publish()
        self.respond()
        result = self.search(mode="symbol")
        self.assertEqual(result["metadata"]["symbol_kind"], "lexical_ctags_not_semantic")
        self.response["has_symbols"] = False
        self.respond()
        with self.assertRaises(MirrorError) as raised:
            self.search(mode="symbol")
        self.assertEqual(raised.exception.code, "symbols_unavailable")

    def test_status_index_health_is_bounded_and_contains_no_paths(self):
        status = self.backend.status()["code_index"]
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["commit"], self.fixture.tip)
        self.assertNotIn(str(self.code), json.dumps(status))
        self.publication["commit_oid"] = self.fixture.old
        self.publish()
        self.assertEqual(self.backend.status()["code_index"]["status"], "stale")
        (self.code / "index-state.json").write_text("not json")
        self.assertEqual(self.backend.status()["code_index"]["status"], "error")

    def test_health_tracks_current_index_policy_and_bounds_timestamp(self):
        self.cfg["code"]["index"]["symbols"] = True
        health = self.backend.status()["code_index"]
        self.assertEqual(health["status"], "stale")
        self.assertFalse(health["has_symbols"])
        self.cfg["code"]["index"]["symbols"] = False
        self.publication["published_at"] = {"untrusted": "x" * 1000}
        self.publish()
        self.assertEqual(self.backend.status()["code_index"]["status"], "error")
        self.fallback("index_state_invalid")


    def test_malformed_nested_index_state_is_structured_fallback(self):
        for published in ([], {"tree": []}, {"tree": "tiny", "ref": None}):
            (self.code / "index-state.json").write_text(json.dumps({"schema_version": 1, "generation": 1, "published": published}))
            self.assertEqual(self.backend.status()["code_index"]["status"], "error")
            self.fallback("index_state_invalid")

    def test_bridge_output_budget_is_explicit_and_no_second_budget(self):
        with patch.object(self.backend, "_run_bridge", return_value=GitResult(b"{", -9, "query_output_limit")):
            self.fallback("index_query_output_limit")
        with self.backend.read_guard():
            self.backend.local.output_remaining = 1
            result = self.adapter.search(tree="tiny", ref="refs/heads/main", commit=self.fixture.tip,
                                         fingerprint=self.publication["catalog_fingerprint"], query="newest", paths=[], mode="literal",
                                         case_sensitive=True, limit=10, max_line_bytes=4096, deadline=self.backend.local.deadline,
                                         max_output_bytes=1, runner=self.backend._run_bridge)
            self.assertEqual(result.fallback_reason, "index_query_budget_unavailable")


class NativeZoektReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.indexer = Path(os.environ.get("LORE_ZOEKT_INDEXER", "/usr/local/bin/zoekt-git-index"))
        cls.bridge = Path(os.environ.get("LORE_ZOEKT_BRIDGE", "/usr/local/bin/lore-zoekt"))
        missing = [str(path) for path in (cls.indexer, cls.bridge)
                   if not path.is_file() or not os.access(path, os.X_OK)]
        if missing:
            reason = "pinned native Zoekt executables unavailable: " + ", ".join(missing)
            if os.environ.get("LORE_NATIVE_E2E_REQUIRED") == "1":
                raise RuntimeError(reason)
            raise unittest.SkipTest(reason)

    def setUp(self):
        self.fixture = IndexedSearchTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        base = f.fixture
        (base.work / "odd\tname\n雪.txt").unlink()
        (base.work / "rank").mkdir()
        for index in range(30):
            (base.work / "rank" / f"a{index:02d}.c").write_text("int rankprobe;\n")
        (base.work / "rank" / "zz.c").write_text("rankprobe rankprobe rankprobe rankprobe\n" * 2)
        base.git("add", ".")
        base.git("commit", "-qm", "admit ordinary indexed paths")
        oid = base.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "--git-dir=" + str(base.repo), "fetch", "--quiet", str(base.work), "main:refs/heads/main"], check=True)
        base.tip = oid
        base.publish(2)
        for child in f.directory.iterdir():
            child.unlink()
        indexer, bridge = self.indexer, self.bridge
        subprocess.run([str(indexer), "-index", str(f.directory), "-branches", oid, "-parallelism", "1",
                        "-submodules=false", "-incremental=false", "-delta=false", "-allow_missing_branches=false",
                        "-require_ctags=false", "-disable_ctags=true", "-max_trigram_count", "20000",
                        "-file_limit", "1048576", "-shard_limit", str(64 * 1024**2), str(base.repo)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=f.backend.env)
        f.build.update(indexer_sha256=hashlib.sha256(indexer.read_bytes()).hexdigest(), query_bridge_sha256=hashlib.sha256(bridge.read_bytes()).hexdigest())
        f.manifest.write_text(json.dumps(f.build))
        f.publication.update(commit_oid=oid, code_generation=2, indexer_sha256=f.build["indexer_sha256"],
                             query_bridge_sha256=f.build["query_bridge_sha256"],
                             shards=[{"name": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                     for path in sorted(f.directory.iterdir())])
        f.publish()
        f.backend.search_backend = CodeSearchBackend(f.cfg, bridge_path=bridge, build_manifest_path=f.manifest)

    def test_real_engine_literal_and_path_parity(self):
        f = self.fixture
        for query, paths in (("newest", ["one.txt"]), ("needle", [":(glob)*"]), ("needle", ["--help"]), ("passwd", None)):
            indexed = f.search(query=query, paths=paths)
            self.assertEqual(indexed["metadata"]["engine"], "zoekt", indexed["metadata"])
            f.cfg["code"]["index"]["enabled"] = False
            exact = f.search(query=query, paths=paths)
            f.cfg["code"]["index"]["enabled"] = True
            shape = lambda result: [(row["path"], row["line"], row["text"], row["blob"]) for row in result["items"]]
            self.assertEqual(shape(indexed), shape(exact))

    def test_real_engine_truncation_fills_the_requested_line_limit(self):
        # One file repeating the pattern once used up the engine's occurrence
        # quota, so a five-line page came back with two lines.
        f = self.fixture
        indexed = f.search(query="rankprobe", paths=["rank"], limit=5)
        self.assertEqual(indexed["metadata"]["engine"], "zoekt", indexed["metadata"])
        self.assertEqual(len(indexed["items"]), 5)
        self.assertTrue(indexed["partial"])
        self.assertEqual(indexed["metadata"]["truncated_selection"], "engine_ranked")

    def test_real_engine_query_syntax_remains_literal_and_historical_tag_exact(self):
        f = self.fixture
        indexed = f.search(query="newest OR repo:.*")
        self.assertEqual(indexed["metadata"]["engine"], "zoekt")
        self.assertEqual(indexed["items"], [])
        exact = f.search(query="needle", ref="refs/tags/next-20261020")
        self.assertEqual(exact["metadata"]["engine"], "git")
        self.assertEqual(exact["items"][0]["commit"], f.fixture.old)



class NativeZoektGateTests(unittest.TestCase):
    def test_required_mode_uses_baked_defaults_without_override_environment(self):
        with patch.dict(os.environ, {"LORE_NATIVE_E2E_REQUIRED": "1"}, clear=True), \
             patch.object(Path, "is_file", return_value=True), patch("os.access", return_value=True):
            NativeZoektReaderTests.setUpClass()
            self.assertEqual(NativeZoektReaderTests.indexer, Path("/usr/local/bin/zoekt-git-index"))
            self.assertEqual(NativeZoektReaderTests.bridge, Path("/usr/local/bin/lore-zoekt"))

    def test_required_mode_missing_tools_fails_instead_of_skip(self):
        with patch.dict(os.environ, {"LORE_NATIVE_E2E_REQUIRED": "1"}, clear=True), \
             patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "native Zoekt executables unavailable"):
                NativeZoektReaderTests.setUpClass()

    def test_optional_mode_missing_tools_visibly_skips(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(unittest.SkipTest):
                NativeZoektReaderTests.setUpClass()


class BridgeInputTests(unittest.TestCase):
    def test_stdin_is_bounded_before_spawn_and_nonblocking(self):
        with patch("lore_mirror.code_runtime.subprocess.Popen") as popen:
            with self.assertRaises(MirrorError):
                bounded_git_run([sys.executable], env={}, timeout=1, max_bytes=1024, memory_bytes=128*1024**2, input_bytes=b"x"*65537)
            popen.assert_not_called()
        data = b"x" * 65536
        result = bounded_git_run([sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                                 env={}, timeout=2, max_bytes=len(data), memory_bytes=128*1024**2, input_bytes=data)
        self.assertEqual(result.output, data)
        self.assertIsNone(result.partial_reason)


if __name__ == "__main__":
    unittest.main()
