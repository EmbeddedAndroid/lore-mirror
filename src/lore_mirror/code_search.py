"""Optional pinned Zoekt bridge; exact Git remains the fail-closed fallback.

This adapter never evaluates engine query syntax. It supplies one JSON request
for the bridge's typed query AST, against one immutable published snapshot.
Public regex and case-folding retain the existing Git dialect and semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .code_config import CODE_STATE_MAX_BYTES, code_catalog_fingerprint
from .code_common import OID, SHA256 as HEX256
from .code_engine import (ENGINE_COMMIT, QUERY_BINARY as BRIDGE_PATH,
    BUILD_MANIFEST as BUILD_MANIFEST_PATH, MAX_BINARY_BYTES, MAX_TRIGRAM_COUNT,
    EngineIdentityError, verify_engine_identity)
from .errors import MirrorError

PARTIAL_REASONS = {"query_timeout", "engine_search_limit", "match_limit", "path_limit", "line_limit", "non_utf8_content", "output_limit"}


class IndexUnavailable(Exception):
    def __init__(self, reason):
        self.reason = reason


@dataclass
class IndexedResult:
    items: list | None
    metadata: dict
    reasons: list
    fallback_reason: str | None = None


class CodeSearchBackend:
    def __init__(self, cfg, *, bridge_path=BRIDGE_PATH, build_manifest_path=BUILD_MANIFEST_PATH):
        # Path overrides are for offline fixture injection, not accepted config.
        self.cfg = cfg
        self.code = cfg.get("code", {})
        self.index = self.code.get("index", {})
        self.root = Path(self.code.get("root_dir", Path(cfg["data_dir"]) / "code"))
        self.state_path = self.root / "index-state.json"
        self.bridge = Path(bridge_path)
        self.build_manifest = Path(build_manifest_path)
        self._verified_binary = None

    def _state(self):
        if not self.index.get("enabled", False):
            raise IndexUnavailable("index_disabled")
        try:
            with self.state_path.open("rb") as stream:
                raw = stream.read(CODE_STATE_MAX_BYTES + 1)
            if len(raw) > CODE_STATE_MAX_BYTES:
                raise ValueError()
            state = json.loads(raw)
            if (not isinstance(state, dict) or state.get("schema_version") != 1
                    or type(state.get("generation")) is not int or state["generation"] < 0
                    or (state.get("published") is not None and not isinstance(state["published"], dict))):
                raise ValueError()
            publication = state.get("published")
            if publication:
                for key, maximum in (("tree", 64), ("ref", 1024), ("catalog_fingerprint", 64), ("engine_commit", 40), ("directory", 160)):
                    value = publication.get(key)
                    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= maximum:
                        raise ValueError()
                if not isinstance(publication.get("commit_oid"), str) or not OID.fullmatch(publication["commit_oid"]):
                    raise ValueError()
                timestamp = publication.get("published_at")
                if timestamp is not None and (not isinstance(timestamp, str) or len(timestamp.encode("utf-8")) > 128):
                    raise ValueError()
            return state
        except FileNotFoundError:
            raise IndexUnavailable("index_absent") from None
        except (OSError, ValueError, RecursionError):
            raise IndexUnavailable("index_state_invalid") from None

    def generation(self):
        return self.snapshot()[1]

    def snapshot(self):
        """Capture one detached publication for an entire lock-free read."""
        try:
            state = self._state()
            return state, str(state["generation"])
        except IndexUnavailable as exc:
            return None, exc.reason

    def _publication(self, tree, ref, commit, fingerprint, snapshot=None):
        state, identity = self.snapshot() if snapshot is None else snapshot
        if state is None:
            raise IndexUnavailable(identity)
        row = state.get("published")
        if not row:
            raise IndexUnavailable("index_not_published")
        self._compatible(row, tree, ref, commit, fingerprint)
        return state, row

    def _compatible(self, row, tree, ref, commit, fingerprint):
        if (tree != self.index.get("pilot_tree") or ref != self.index.get("pilot_ref")
                or row.get("tree") != tree or row.get("ref") != ref):
            raise IndexUnavailable("index_scope_mismatch")
        if row.get("commit_oid") != commit:
            raise IndexUnavailable("index_oid_stale")
        if row.get("catalog_fingerprint") != fingerprint:
            raise IndexUnavailable("index_catalog_stale")
        if row.get("engine_commit") != ENGINE_COMMIT:
            raise IndexUnavailable("index_engine_mismatch")
        if row.get("symbols", False) != self.index.get("symbols", False):
            raise IndexUnavailable("index_symbols_policy_stale")
        coverage = row.get("coverage")
        if (not isinstance(coverage, dict) or type(coverage.get("partial")) is not bool
                or coverage.get("max_file_bytes") != self.code.get("limits", {}).get("max_file_bytes", 1024 * 1024)
                or coverage.get("max_trigram_count") != MAX_TRIGRAM_COUNT
                or not isinstance(coverage.get("exclusions"), dict)):
            raise IndexUnavailable("index_source_policy_mismatch")
        if type(row.get("unsafe_paths")) is not int or row["unsafe_paths"] < 0:
            raise IndexUnavailable("index_source_policy_unknown")
        if row["unsafe_paths"]:
            raise IndexUnavailable("index_unsupported_source_paths")
        policy = row.get("policy")
        expected_policy = {"admission_version": 1, "max_file_bytes": coverage["max_file_bytes"], "max_trigram_count": MAX_TRIGRAM_COUNT,
                           "symbols": self.index.get("symbols", False), "ctags_sha256": row.get("ctags_sha256")}
        if policy != expected_policy or row.get("policy_fingerprint") != hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest():
            raise IndexUnavailable("index_source_policy_mismatch")

    def _binary(self, publication, deadline):
        try:
            if self.build_manifest.is_symlink():
                raise IndexUnavailable("index_build_manifest_invalid")
            with self.build_manifest.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise IndexUnavailable("index_build_manifest_invalid")
            manifest = json.loads(raw)
            try:
                verify_engine_identity(manifest, publication, symbols=self.index.get("symbols", False))
            except EngineIdentityError as exc:
                reason = {"engine": "index_build_manifest_invalid", "binary": "index_build_identity_mismatch",
                          "symbols": "index_symbol_build_identity_mismatch"}[exc.reason]
                raise IndexUnavailable(reason) from None
            info = self.bridge.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BINARY_BYTES or not os.access(self.bridge, os.X_OK):
                raise IndexUnavailable("index_bridge_unavailable")
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, publication["query_bridge_sha256"])
            if identity != self._verified_binary:
                digest, size = hashlib.sha256(), 0
                with self.bridge.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        if time.monotonic() >= deadline:
                            raise IndexUnavailable("index_validation_timeout")
                        size += len(chunk)
                        if size > MAX_BINARY_BYTES:
                            raise IndexUnavailable("index_bridge_unavailable")
                        digest.update(chunk)
                after = self.bridge.lstat()
                after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, publication["query_bridge_sha256"])
                if after_identity != identity or digest.hexdigest() != publication["query_bridge_sha256"]:
                    raise IndexUnavailable("index_bridge_identity_mismatch")
                self._verified_binary = identity
        except IndexUnavailable:
            raise
        except (OSError, ValueError, RecursionError):
            raise IndexUnavailable("index_bridge_unavailable") from None

    def _directory(self, publication):
        name = publication.get("directory")
        if not isinstance(name, str) or not re.fullmatch(r"indexes/generations/[A-Za-z0-9_-]{1,80}", name):
            raise IndexUnavailable("index_directory_invalid")
        directory = self.root
        try:
            for component in name.split("/"):
                directory = directory / component
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    raise IndexUnavailable("index_directory_invalid")
            shards = publication.get("shards")
            if not isinstance(shards, list) or not 1 <= len(shards) <= 4096:
                raise IndexUnavailable("index_shards_invalid")
            expected, total_bytes = set(), 0
            for shard in shards:
                if not isinstance(shard, dict):
                    raise IndexUnavailable("index_shards_invalid")
                leaf, size, digest = shard.get("name"), shard.get("bytes"), shard.get("sha256")
                if (not isinstance(leaf, str) or len(leaf.encode("utf-8")) > 255 or not leaf.endswith(".zoekt")
                        or "/" in leaf or "\\" in leaf or "\x00" in leaf or leaf in expected
                        or type(size) is not int or size < 1 or not isinstance(digest, str) or not HEX256.fullmatch(digest)):
                    raise IndexUnavailable("index_shards_invalid")
                info = (directory / leaf).lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                    raise IndexUnavailable("index_shards_invalid")
                expected.add(leaf)
                total_bytes += size
            if total_bytes > self.index.get("max_index_bytes", 2 * 1024**3):
                raise IndexUnavailable("index_size_limit")
            actual = set()
            for child in directory.iterdir():
                actual.add(child.name)
                if len(actual) > 4096:
                    raise IndexUnavailable("index_shards_invalid")
            if actual != expected:
                raise IndexUnavailable("index_shards_invalid")
            # Content hashes are verified before atomic publication. Published
            # directories are immutable and query mounts are read-only. No
            # reader holds index.lock; publication drift is checked separately.
            return directory
        except IndexUnavailable:
            raise
        except (OSError, UnicodeError):
            raise IndexUnavailable("index_directory_unavailable") from None

    @staticmethod
    def _path(value):
        if not isinstance(value, str):
            return False
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            return False
        return (0 < len(encoded) <= 4096 and "\x00" not in value and not value.startswith("/")
                and all(part not in ("", ".", "..") for part in value.split("/")))

    def search(self, *, tree, ref, commit, fingerprint, query, paths, mode, case_sensitive,
               limit, max_line_bytes, deadline, max_output_bytes, runner, snapshot=None):
        if not self.index.get("enabled", False):
            return IndexedResult(None, {}, [], "index_disabled")
        if mode == "regex":
            return IndexedResult(None, {}, [], "regex_dialect_git_only")
        if mode == "literal" and not case_sensitive:
            return IndexedResult(None, {}, [], "case_folding_git_only")
        if mode == "symbol" and not self.index.get("symbols", False):
            return IndexedResult(None, {}, [], "symbols_not_enabled")
        if any("\n" in path or "\r" in path for path in paths):
            return IndexedResult(None, {}, [], "index_path_encoding_unsupported")
        try:
            state, publication = self._publication(tree, ref, commit, fingerprint, snapshot)
            if mode == "symbol" and not publication.get("has_symbols", False):
                raise IndexUnavailable("symbols_not_indexed")
            directory = self._directory(publication)
            self._binary(publication, deadline)
            available_ms = int((deadline - time.monotonic()) * 1000)
            if available_ms < 1 or max_output_bytes < 1024:
                raise IndexUnavailable("index_query_budget_unavailable")
            request = {"index_dir": str(directory), "commit": commit, "pattern": query, "mode": mode, "paths": paths,
                       "case_sensitive": case_sensitive, "max_matches": min(limit, 10000), "max_line_bytes": max_line_bytes,
                       "max_bytes": min(max_output_bytes, 1024 * 1024), "timeout_ms": min(available_ms, 30000)}
            raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(raw) > 65536:
                raise IndexUnavailable("index_request_limit")
            result = runner([str(self.bridge)], raw, request["max_bytes"])
            if result.partial_reason:
                raise IndexUnavailable("index_" + result.partial_reason)
            response = json.loads(result.output)
            if not isinstance(response, dict) or response.get("error") or result.returncode != 0:
                raise IndexUnavailable("index_query_error")
            if response.get("engine_commit") != ENGINE_COMMIT or response.get("commit") != commit:
                raise IndexUnavailable("index_result_identity_mismatch")
            if type(response.get("has_symbols")) is not bool or (mode == "symbol" and not response["has_symbols"]):
                raise IndexUnavailable("symbols_not_indexed" if mode == "symbol" else "index_result_invalid")
            items, truncated, reasons = response.get("matches"), response.get("truncated"), response.get("partial_reasons")
            if (not isinstance(items, list) or len(items) > limit or type(truncated) is not bool
                    or not isinstance(reasons, list) or len(reasons) > len(PARTIAL_REASONS)
                    or any(not isinstance(reason, str) or reason not in PARTIAL_REASONS for reason in reasons)
                    or truncated != bool(reasons)):
                raise IndexUnavailable("index_result_invalid")
            normalized, seen = [], set()
            for item in items:
                if not isinstance(item, dict):
                    raise IndexUnavailable("index_result_invalid")
                path, line, text, clipped = item.get("path"), item.get("line"), item.get("text"), item.get("text_truncated")
                if (not self._path(path) or item.get("commit") != commit or type(line) is not int or not 1 <= line <= 2**31 - 1
                        or not isinstance(text, str) or "\x00" in text or "\n" in text or type(clipped) is not bool
                        or len(text.encode("utf-8")) > max_line_bytes or (path, line) in seen
                        or (paths and not any(path == scope or path.startswith(scope + "/") for scope in paths))):
                    raise IndexUnavailable("index_result_invalid")
                if clipped and not truncated:
                    raise IndexUnavailable("index_result_invalid")
                seen.add((path, line))
                normalized.append({"tree": tree, "requested_ref": ref, "commit": commit, "path": path,
                                   "line": line, "text": text, "text_truncated": clipped, "encoding": "utf-8"})
            normalized.sort(key=lambda item: (item["path"].encode("utf-8"), item["line"]))
            metadata = {"engine": "zoekt", "engine_commit": ENGINE_COMMIT, "code_index_generation": state["generation"],
                        "indexed_at": publication.get("published_at", "")[:40] if publication.get("published_at") else None, "coverage": "one_exact_indexed_commit_regular_text_blobs_only",
                        "regex_dialect": None, "index_content_verification": "publication_hashes_read_only_generation",
                        "symbol_kind": "lexical_ctags_not_semantic" if mode == "symbol" else None}
            if "match_limit" in reasons:
                metadata["truncated_selection"] = "engine_ranked"
            if publication["coverage"]["partial"]:
                reasons.append("index_excluded_source_files")
            metadata["index_exclusions"] = {key: value for key, value in publication["coverage"]["exclusions"].items()
                                            if key in ("oversized_files", "submodules", "symlinks", "binary_generated_or_non_utf8")
                                            and ((type(value) is int and 0 <= value <= 2**63 - 1)
                                                 or (isinstance(value, str) and len(value) <= 160))}
            return IndexedResult(normalized, metadata, reasons)
        except IndexUnavailable as exc:
            return IndexedResult(None, {}, [], exc.reason)
        except MirrorError as exc:
            if exc.code == "code_cleanup_timeout":
                raise
            return IndexedResult(None, {}, [], "index_query_error")
        except (OSError, ValueError, UnicodeError, RecursionError):
            return IndexedResult(None, {}, [], "index_query_error")

    def health(self, code_state):
        if not self.index.get("enabled", False):
            return {"enabled": False, "status": "disabled"}
        try:
            state = self._state()
            publication = state.get("published") or {}
            tree, ref, commit = publication.get("tree"), publication.get("ref"), publication.get("commit_oid")
            trees = {item["id"]: item for item in self.code.get("trees", [])}
            current = (tree in trees and isinstance(ref, str) and isinstance(commit, str) and OID.fullmatch(commit)
                       and code_state.get("trees", {}).get(tree, {}).get("published_refs", {}).get(ref) == commit
                       and publication.get("catalog_fingerprint") == code_catalog_fingerprint(trees[tree])
                       and publication.get("engine_commit") == ENGINE_COMMIT
                       and tree == self.index.get("pilot_tree") and ref == self.index.get("pilot_ref"))
            reason = None
            if current:
                try:
                    self._compatible(publication, tree, ref, commit, code_catalog_fingerprint(trees[tree]))
                except IndexUnavailable as exc:
                    current, reason = False, exc.reason
            return {"enabled": True, "status": "absent" if not publication else "ready" if current else "stale", "reason": reason,
                    "generation": state["generation"], "tree": tree if isinstance(tree, str) and len(tree) <= 64 else None,
                    "ref": ref[:128] if isinstance(ref, str) else None, "ref_truncated": isinstance(ref, str) and len(ref) > 128,
                    "commit": commit if isinstance(commit, str) and OID.fullmatch(commit) else None,
                    "indexed_at": str(publication.get("published_at"))[:40] if publication.get("published_at") else None,
                    "has_symbols": bool(current and self.index.get("symbols", False) and publication.get("has_symbols", False)),
                    "last_error": None if not state.get("last_error") else
                        "Index build is degraded; serving the last matching published generation" if publication and current else
                        "Last index build failed; exact Git search is used"}
        except IndexUnavailable as exc:
            return {"enabled": True, "status": "absent" if exc.reason == "index_absent" else "error", "reason": exc.reason}
