"""Shared policy stays strict and phase-neutral across code consumers."""
from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lore_mirror.code_common import (OID, SHA256, SAFE_REPO_CONFIG, CodeStateError,
    RepositoryOwnershipError, empty_code_state, load_code_state, storage_error,
    validate_code_state, validate_repo_ownership)
from lore_mirror.code_engine import ENGINE_COMMIT, EngineIdentityError, verify_engine_identity


class SharedCodeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "code-state.json"
        self.state = {"schema_version": 1, "generation": 2, "trees": {"tiny": {
            "generation": 2, "status": "ready", "consecutive_errors": 0,
            "published_at": "2026-10-21T00:00:00Z",
            "published_refs": {"refs/heads/main": "a" * 40, "refs/tags/v1": "b" * 64}}}}

    def write(self, state=None):
        self.path.write_text(json.dumps(self.state if state is None else state))

    def test_valid_publication_and_optional_retry_state(self):
        row = self.state["trees"]["tiny"]
        row.update(retry_blocked=True, blocked_reason="storage", blocked_config_fingerprint="c" * 64,
                   next_check_at=None,
                   skipped_refs={"refs/tags/data": {"object_type": "tree", "object_id": "e" * 40,
                                                   "reason": "peeled target is not a commit"}})
        self.write()
        self.assertEqual(load_code_state(self.path), self.state)
        for value in (None, "", 0, 0.0, "2026-10-21T00:00:00+00:00"):
            row["started_at"] = value
            self.assertIs(validate_code_state(self.state), self.state)

    def test_missing_is_empty_only_when_requested_and_returns_detached_state(self):
        with self.assertRaises(CodeStateError) as caught:
            load_code_state(self.path)
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("unavailable", "missing"))
        first = load_code_state(self.path, missing_ok=True)
        first["trees"]["changed"] = {}
        self.assertEqual(load_code_state(self.path, missing_ok=True), empty_code_state())

    def test_disabled_reader_never_reads_disk(self):
        from lore_mirror.code_backend import CodeBackend
        backend = CodeBackend({"data_dir": str(self.root), "code": {"enabled": False}})
        with patch("lore_mirror.code_backend.load_code_state", side_effect=AssertionError("disabled disk read")):
            self.assertEqual(backend._state(), empty_code_state())

    def test_symlink_directory_fifo_and_oversized_checkpoint_fail_closed(self):
        target = self.root / "target.json"
        target.write_text(json.dumps(self.state))
        for existing in (True, False):
            with self.subTest(existing=existing):
                if not existing:
                    target.unlink()
                self.path.symlink_to(target)
                with self.assertRaises(CodeStateError) as caught:
                    load_code_state(self.path, missing_ok=True)
                self.assertEqual(caught.exception.reason, "symlink")
                self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(CodeStateError):
            load_code_state(self.path)
        self.path.rmdir()
        os.mkfifo(self.path)
        with self.assertRaises(CodeStateError):
            load_code_state(self.path)
        self.path.unlink()
        self.write()
        with patch("lore_mirror.code_common.CODE_STATE_MAX_BYTES", 8):
            with self.assertRaises(CodeStateError) as caught:
                load_code_state(self.path)
            self.assertEqual(caught.exception.detail, "oversized")

    def test_json_errors_and_deep_nesting_are_normalized_without_mutation(self):
        for raw in (b"{", b"\xff", b"[" * 2000 + b"0" + b"]" * 2000):
            self.path.write_bytes(raw)
            with self.assertRaises(CodeStateError) as caught:
                load_code_state(self.path)
            self.assertIn(caught.exception.detail, ("json", "schema"))
            self.assertEqual(self.path.read_bytes(), raw)

    def test_union_rejects_every_reader_and_writer_corruption_class(self):
        row_changes = [
            ("generation", True), ("generation", -1), ("status", []), ("status", "x" * 65),
            ("last_error", {}), ("consecutive_errors", True), ("consecutive_errors", -1),
            ("consecutive_errors", 1000001), ("published_refs", []),
            ("published_refs", {"HEAD": "a" * 40}), ("published_refs", {"refs/heads/main": "a" * 40 + "\n"}),
            ("published_refs", {"refs/heads/main": True}), ("retry_blocked", 1),
            ("retry_blocked", True), ("blocked_reason", "unknown"), ("blocked_reason", []),
            ("blocked_reason", "deadline"), ("skipped_refs", []),
            ("skipped_refs", {"refs/tags/data": {"object_type": "blob", "object_id": "a" * 40,
                                                   "reason": "different untrusted diagnostic"}}),
        ]
        for key in ("published_at", "fetched_at", "checked_at", "next_check_at", "started_at"):
            for value in (True, 1, float("nan"), float("inf"), "x" * 129, "not a date", []):
                row_changes.append((key, value))
        for key, value in row_changes:
            with self.subTest(key=key, value=value):
                candidate = copy.deepcopy(self.state)
                candidate["trees"]["tiny"][key] = value
                with self.assertRaises(CodeStateError):
                    validate_code_state(candidate)
        for key, value in (("schema_version", True), ("schema_version", 2), ("generation", True),
                           ("generation", -1), ("trees", []), ("trees", {"BAD/TREE": {}}),
                           ("trees", {"tiny": []})):
            candidate = self.state | {key: value}
            with self.subTest(key=key, value=value), self.assertRaises(CodeStateError):
                validate_code_state(candidate)
        with self.assertRaises(CodeStateError):
            validate_code_state(self.state, max_refs=1)

    def test_retry_block_requires_exact_fingerprint_and_no_scheduled_retry(self):
        for fingerprint, next_check in ((None, None), ("c" * 63, None), ("C" * 64, None),
                                        ("c" * 64, "2026-10-21T00:00:00Z")):
            row = self.state["trees"]["tiny"]
            row.update(retry_blocked=True, blocked_reason="storage",
                       blocked_config_fingerprint=fingerprint, next_check_at=next_check)
            with self.assertRaises(CodeStateError):
                validate_code_state(self.state)



class SharedOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "tiny.git"
        self.repo.mkdir()
        (self.repo / "config").write_text(SAFE_REPO_CONFIG)
        (self.repo / "objects/info").mkdir(parents=True)

    def test_owned_layout_is_accepted_without_mutation(self):
        before = (self.repo / "config").read_bytes()
        validate_repo_ownership(self.repo)
        self.assertEqual((self.repo / "config").read_bytes(), before)

    def test_unowned_config_and_empty_alternates_fail_closed(self):
        (self.repo / "config").write_text(SAFE_REPO_CONFIG + "\n")
        with self.assertRaises(RepositoryOwnershipError) as caught:
            validate_repo_ownership(self.repo)
        self.assertEqual(caught.exception.reason, "config")
        (self.repo / "config").write_text(SAFE_REPO_CONFIG)
        for relative, reason in (("shallow", "shallow"), ("objects/info/alternates", "alternates")):
            path = self.repo / relative
            path.touch()
            with self.assertRaises(RepositoryOwnershipError) as caught:
                validate_repo_ownership(self.repo)
            self.assertEqual(caught.exception.reason, reason)
            path.unlink()

    def test_nonregular_config_never_blocks_or_counts_as_owned(self):
        config = self.repo / "config"
        config.unlink()
        os.mkfifo(config)
        with self.assertRaises(RepositoryOwnershipError):
            validate_repo_ownership(self.repo)
        config.unlink()
        config.mkdir()
        with self.assertRaises(RepositoryOwnershipError):
            validate_repo_ownership(self.repo)

    def test_symlink_sensitive_paths_and_dangling_markers_are_rejected(self):
        for relative in ("config", "objects", "refs", "objects/info", "objects/pack", "shallow", "objects/info/alternates"):
            path = self.repo / relative
            saved = self.root / "saved"
            existed = path.exists()
            if existed:
                path.rename(saved)
            path.symlink_to(self.root / "missing")
            try:
                with self.subTest(relative=relative), self.assertRaises(RepositoryOwnershipError):
                    validate_repo_ownership(self.repo)
            finally:
                path.unlink()
                if existed:
                    saved.rename(path)
        link = self.root / "alias.git"
        link.symlink_to(self.repo)
        with self.assertRaises(RepositoryOwnershipError) as caught:
            validate_repo_ownership(link)
        self.assertEqual(caught.exception.reason, "symlink")


class SharedIdentityAndStorageTests(unittest.TestCase):

    def test_engine_identity_binds_all_enabled_executables(self):
        identity = {"engine_commit": ENGINE_COMMIT, "indexer_sha256": "a" * 64,
                    "query_bridge_sha256": "b" * 64, "ctags_sha256": "c" * 64}
        manifest = {"upstream_commit": ENGINE_COMMIT, **identity}
        self.assertIs(verify_engine_identity(manifest, identity, symbols=True), identity)
        for key, reason in (("upstream_commit", "engine"), ("indexer_sha256", "binary"),
                            ("query_bridge_sha256", "binary"), ("ctags_sha256", "symbols")):
            with self.subTest(key=key), self.assertRaises(EngineIdentityError) as caught:
                verify_engine_identity(manifest | {key: "x"}, identity, symbols=True)
            self.assertEqual(caught.exception.reason, reason)
        malformed = identity | {"query_bridge_sha256": "not a hash"}
        with self.assertRaises(EngineIdentityError):
            verify_engine_identity(manifest | malformed, malformed)
        verify_engine_identity({key: value for key, value in manifest.items() if key != "ctags_sha256"}, identity)

    def test_storage_errno_git_text_and_sqlite_codes_share_one_policy(self):
        for number in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG, errno.EROFS):
            self.assertTrue(storage_error(OSError(number, "fixture")))
        for message in ("No space left on device", "disk quota exceeded", "disk full",
                        "read-only file system", "file too large", "database or disk is full"):
            self.assertTrue(storage_error(RuntimeError(message)))
            self.assertTrue(storage_error(message.encode()))
        failure = sqlite3.OperationalError("opaque native message")
        failure.sqlite_errorcode = sqlite3.SQLITE_FULL | (3 << 8)
        self.assertTrue(storage_error(failure))
        self.assertFalse(storage_error(OSError(errno.EIO, "input/output error")))
        self.assertFalse(storage_error(sqlite3.OperationalError("database is locked")))

    def test_mirroring_does_not_import_later_code_phases(self):
        code = "import sys; import lore_mirror.code_sync; assert not set(('lore_mirror.code_engine', 'lore_mirror.code_index', 'lore_mirror.code_search', 'lore_mirror.code_links')) & set(sys.modules)"
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
