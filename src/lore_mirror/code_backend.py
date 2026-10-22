"""Bounded, read-only Git queries against the published code catalog.

No working tree, remote, mutable Git ref, textconv, or external diff is used.
Search covers regular text blobs at one exact published commit, not history.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .errors import MirrorError
from .code_config import DEFAULT_CODE_LIMITS, code_catalog_fingerprint
from .code_common import OID, CodeStateError, empty_code_state, load_code_state
from .code_runtime import CodeRunError, bounded_run
from .code_search import CodeSearchBackend

DEFAULTS = DEFAULT_CODE_LIMITS


@dataclass
class GitResult:
    output: bytes
    returncode: int
    partial_reason: str | None = None
    stderr: bytes = b""


def bounded_git_run(argv, *, env, timeout, max_bytes, memory_bytes, input_bytes=b"", pass_fds=(),
                    accepted_returncodes=(0, 1)):
    """Translate the shared bounded runner into the reader's partial contract."""
    try:
        result = bounded_run(argv, env=env, timeout=timeout, max_bytes=max_bytes,
            memory_bytes=memory_bytes, input_bytes=input_bytes, pass_fds=pass_fds)
    except CodeRunError as exc:
        raise MirrorError(exc.reason, "Local reader input exceeds 64 KiB", 413) from exc
    except subprocess.TimeoutExpired as exc:
        raise MirrorError("code_cleanup_timeout", "Local reader process cleanup exceeded its deadline", 503) from exc
    except OSError as exc:
        raise MirrorError("code_backend_unavailable", "Cannot start local Git reader", 503) from exc
    reason = "query_" + result.partial_reason if result.partial_reason else None
    if not reason and result.returncode not in accepted_returncodes:
        raise MirrorError("code_backend_error", "Local Git read failed; the published objects may be missing or invalid", 503)
    return GitResult(result.output, result.returncode, reason, result.stderr)


class CodeBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        self.code = cfg.get("code", {})
        self.enabled = bool(self.code.get("enabled", False))
        self.root = Path(self.code.get("root_dir", Path(cfg["data_dir"]) / "code"))
        self.state_path = self.root / "code-state.json"
        self.trees = {tree["id"]: tree for tree in self.code.get("trees", [])}
        self.limits = DEFAULTS | self.code.get("limits", {})
        # Never exceed the mail service's configured output/session constraints.
        self.limits["query_output_bytes"] = min(self.limits["query_output_bytes"], cfg.get("limits", {}).get("query_output_bytes", 16 * 1024 * 1024))
        self.local = threading.local()
        self.search_backend = CodeSearchBackend(cfg)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "",
                    "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_LITERAL_PATHSPECS": "1", "GIT_PAGER": "cat", "GIT_CONFIG_COUNT": "0"}

    def _state(self):
        if not self.enabled:
            return empty_code_state()
        try:
            return load_code_state(self.state_path, max_refs=self.code.get("max_refs", 10000), missing_ok=True)
        except CodeStateError as exc:
            raise MirrorError("code_state_invalid", "Cannot validate the published code checkpoint", 503) from exc

    def generation(self):
        code_generation = str(self._state()["generation"])
        if self.code.get("index", {}).get("enabled", False):
            return code_generation + ":index:" + self.search_backend.generation()
        return code_generation

    @contextmanager
    def read_guard(self):
        if not self.enabled:
            raise MirrorError("code_disabled", "Code tools are disabled", 503)
        if getattr(self.local, "state", None) is not None:
            yield
            return
        # Only the coordinator creates this lock. API mounts may be read-only.
        try:
            lock = (Path(self.cfg["data_dir"]) / "index.lock").open("rb")
        except FileNotFoundError as exc:
            raise MirrorError("code_not_ready", "Code coordinator has not initialized local storage", 503) from exc
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MirrorError("busy", "Local heavy-I/O work is active; retry later", 429) from exc
            # Admission is only a probe. Never delay a mail indexer for a code
            # read, or pass a coordinator lock to a Git/Zoekt child. Immutable
            # object identities and before/after checkpoints protect readers.
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()
        self.local.deadline = time.monotonic() + self.limits["query_timeout"]
        self.local.output_remaining = int(self.limits["query_output_bytes"])
        try:
            self.local.state = self._state()
            self.local.index_snapshot = self.search_backend.snapshot()
            generation = str(self.local.state["generation"])
            if self.code.get("index", {}).get("enabled", False):
                generation += ":index:" + self.local.index_snapshot[1]
            self.local.generation = generation
            # Derive the identity from the captured state, not a second state
            # read: publication between these reads must invalidate the query.
            if self.generation() != generation:
                raise MirrorError("stale_code_generation", "Code publication changed before the query; retry", 409)
            yield
            if time.monotonic() > self.local.deadline:
                raise MirrorError("query_timeout", "Code query exceeded its complete request deadline", 504)
            if self.generation() != generation:
                raise MirrorError("stale_code_generation", "Code publication changed during the query; retry", 409)
        finally:
            self.local.state = None
            self.local.index_snapshot = None
            self.local.generation = None
            self.local.deadline = None
            self.local.output_remaining = None

    def _run(self, tree, args, *, max_bytes=None, allow_partial=False, allow_one=False,
             accepted_returncodes=(0, 1), input_bytes=b""):
        repo = self.root / "repositories" / (tree + ".git")
        if not repo.is_dir():
            raise MirrorError("code_objects_missing", "Published repository is missing locally", 503)
        argv = ["git", "--no-pager", "--no-optional-locks", "--git-dir=" + str(repo),
                "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "core.attributesFile=/dev/null", "-c", "protocol.allow=never",
                "-c", "credential.helper=", "-c", "diff.external=", "-c", "color.ui=false",
                "-c", "core.quotePath=false", "-c", "grep.threads=1", *args]
        remaining = (getattr(self.local, "deadline", None) or (time.monotonic() + self.limits["query_timeout"])) - time.monotonic()
        output_remaining = getattr(self.local, "output_remaining", None)
        if output_remaining is None:
            output_remaining = int(self.limits["query_output_bytes"])
        if remaining <= 0:
            result = GitResult(b"", -1, "query_timeout")
        elif output_remaining <= 0:
            result = GitResult(b"", -1, "query_output_limit")
        else:
            result = bounded_git_run(argv, env=self.env, timeout=remaining,
                                     max_bytes=min(max_bytes or self.limits["query_output_bytes"], output_remaining),
                                     memory_bytes=self.limits["query_memory_bytes"],
                                     accepted_returncodes=accepted_returncodes, input_bytes=input_bytes)
            self.local.output_remaining = output_remaining - len(result.output)
        if result.partial_reason and not allow_partial:
            raise MirrorError(result.partial_reason, "Exact Git read exceeded its resource budget; narrow the request", 413 if "limit" in result.partial_reason else 504)
        if not result.partial_reason and result.returncode == 1 and not allow_one:
            raise MirrorError("code_objects_missing", "Requested published object is unavailable locally", 503)
        return result

    def _run_bridge(self, argv, input_bytes, max_bytes):
        remaining = self.local.deadline - time.monotonic()
        budget = min(max_bytes, self.local.output_remaining)
        if remaining <= 0 or budget < 1024:
            raise MirrorError("query_output_limit", "No remaining bounded index-query budget", 413)
        result = bounded_git_run(argv, env=self.env, timeout=remaining, max_bytes=budget,
                                 memory_bytes=self.limits["query_memory_bytes"], input_bytes=input_bytes)
        self.local.output_remaining -= len(result.output)
        return result

    def _indexed_search(self, tree, oid, metadata, query, paths, mode, case_sensitive, limit):
        result = self.search_backend.search(
            tree=tree, ref=metadata["requested_ref"], commit=oid, fingerprint=metadata["catalog_fingerprint"],
            query=query, paths=paths, mode=mode, case_sensitive=case_sensitive, limit=limit,
            max_line_bytes=self.limits["max_line_bytes"], deadline=self.local.deadline,
            max_output_bytes=self.local.output_remaining, runner=self._run_bridge,
            snapshot=self.local.index_snapshot)
        if result.items is None:
            if mode == "symbol":
                raise MirrorError("symbols_unavailable", "Lexical symbols require a matching, verified symbol-enabled index (" + result.fallback_reason + ")", 503)
            return None, result.fallback_reason
        # Zoekt indexes symlink target strings. Recover exact Git identities and
        # enforce our regular-file/size policy before returning any indexed hit.
        entries = {}
        for batch in self._batches(dict.fromkeys(item["path"] for item in result.items)):
            rows, partial = self._entries(tree, oid, batch)
            if partial:
                if mode == "symbol":
                    raise MirrorError("symbols_unavailable", "Cannot verify indexed symbol paths within the exact-Git budget", 503)
                return None, "index_git_validation_limit"
            entries.update({entry["path"]: entry for entry in rows if "path" in entry})
        kept, excluded = [], 0
        for item in result.items:
            entry = entries.get(item["path"])
            if entry is None:
                if mode == "symbol":
                    raise MirrorError("symbols_unavailable", "Indexed symbol path does not exist at its published commit", 503)
                return None, "index_git_path_mismatch"
            if entry["type"] != "blob" or entry["mode"] not in ("100644", "100755") or entry["size"] > self.limits["max_file_bytes"]:
                excluded += 1
                continue
            item["blob"] = entry["blob"]
            kept.append(item)
        reasons = list(result.reasons)
        if excluded:
            reasons.append("indexed_hits_excluded")
        metadata.update(result.metadata)
        metadata.update({"query": query, "paths": paths, "mode": mode, "case_sensitive": case_sensitive,
                         "excluded_indexed_hits": excluded,
                         "policies": {"binary": "exclude", "max_file_bytes": self.limits["max_file_bytes"],
                                      "max_line_bytes": self.limits["max_line_bytes"], "symlinks_and_gitlinks": "exclude"}})
        return self._result(kept, metadata, reasons), None

    def _catalog_current(self, tree, entry):
        return entry.get("config_fingerprint") == code_catalog_fingerprint(self.trees[tree])

    def _published_entry(self, tree):
        entry = self.local.state["trees"].get(tree, {})
        if entry.get("published_refs") and not self._catalog_current(tree, entry):
            raise MirrorError("code_catalog_changed", "Tree configuration differs from its published snapshot; wait for coordinator republication", 409)
        return entry

    def _resolve(self, tree, ref=None):
        if not isinstance(tree, str) or tree not in self.trees:
            raise MirrorError("unknown_tree", "Unknown configured code tree", 404)
        requested = self.trees[tree].get("default_ref") if ref is None else ref
        if not isinstance(requested, str) or len(requested.encode("utf-8")) > 1024:
            raise MirrorError("invalid_ref", "Use an exact published ref name")
        state = self.local.state
        entry = self._published_entry(tree)
        refs = entry.get("published_refs", {})
        if not isinstance(refs, dict):
            raise MirrorError("code_state_invalid", "Invalid published ref map", 503)
        oid = refs.get(requested)
        if oid is None:
            raise MirrorError("code_ref_not_found", "Exact ref is not in the local published snapshot; no alternate ref was used", 404)
        if not isinstance(oid, str) or not OID.fullmatch(oid):
            raise MirrorError("code_state_invalid", "Invalid published commit identity", 503)
        metadata = {"tree": tree, "requested_ref": requested, "commit": oid,
                    "code_generation": str(state["generation"]), "tree_generation": entry.get("generation"),
                    "fetched_at": entry.get("fetched_at"), "published_at": entry.get("published_at"),
                    "indexed_at": None, "engine": "git", "partial_reasons": [],
                    "catalog_fingerprint": entry.get("config_fingerprint")}
        return oid, metadata

    @staticmethod
    def _path(path):
        if not isinstance(path, str):
            raise MirrorError("invalid_path", "Path must be a literal repository-relative UTF-8 path")
        try:
            raw = path.encode("utf-8")
        except UnicodeError as exc:
            raise MirrorError("invalid_path", "Path must use valid UTF-8") from exc
        if not raw or len(raw) > 4096 or "\x00" in path or path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")):
            raise MirrorError("invalid_path", "Path must be a bounded literal repository-relative path without dot components")
        return path

    def _paths(self, paths):
        if paths is None:
            return []
        if not isinstance(paths, list) or not paths or len(paths) > 64:
            raise MirrorError("invalid_paths", "paths must contain 1..64 literal paths")
        return list(dict.fromkeys(self._path(path) for path in paths))

    def _limit(self, limit):
        if limit is None:
            return int(self.limits["scan_limit"])
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.limits["scan_limit"]:
            raise MirrorError("invalid_limit", "Candidate limit exceeds the code scan budget")
        return limit

    @staticmethod
    def _result(items, metadata, reasons=()):
        metadata["partial_reasons"] = sorted(set(metadata.get("partial_reasons", []) + list(filter(None, reasons))))
        return {"items": items, "partial": bool(metadata["partial_reasons"]), "metadata": metadata}

    def _entries(self, tree, oid, paths):
        # The listing covers the whole tree but only scan_limit entries are
        # used; capping it at half the request budget leaves grep its share.
        result = self._run(tree, ["ls-tree", "-r", "-z", "-l", "--full-tree", oid, "--", *paths], allow_partial=True,
                           max_bytes=self.limits["query_output_bytes"] // 2)
        entries = []
        reason = result.partial_reason
        for record in result.output.split(b"\x00")[:-1]:
            if len(entries) >= self.limits["scan_limit"]:
                reason = reason or "file_scan_limit"
                break
            try:
                header, name = record.split(b"\t", 1)
                mode, kind, blob, size = header.split()
                path = name.decode("utf-8")
                entries.append({"path": path, "mode": mode.decode(), "type": kind.decode(), "blob": blob.decode(),
                                "size": int(size) if size != b"-" else None})
            except UnicodeError:
                entries.append({"excluded": "non_utf8_path"})
            except (ValueError, TypeError) as exc:
                raise MirrorError("code_backend_error", "Malformed local Git tree output", 503) from exc
        return entries, reason

    def _blob(self, tree, oid, path):
        entries, partial = self._entries(tree, oid, [path])
        if partial:
            raise MirrorError(partial, "Cannot identify file within the Git output budget", 413)
        matches = [entry for entry in entries if entry.get("path") == path]
        if not matches:
            raise MirrorError("code_file_not_found", "Literal file does not exist at the requested snapshot", 404)
        entry = matches[0]
        if entry["type"] != "blob":
            raise MirrorError("code_not_blob", "Path is a submodule or non-file object; it is never followed")
        if entry["size"] > self.limits["max_file_bytes"]:
            raise MirrorError("code_file_too_large", "File exceeds the explicit code file-byte limit; no text was omitted silently", 413)
        data = self._run(tree, ["cat-file", "blob", entry["blob"]], max_bytes=self.limits["max_file_bytes"]).output
        return entry, data

    def get_file(self, tree, ref, path, start_line=1, end_line=None):
        path = self._path(path)
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise MirrorError("invalid_line", "start_line must be a positive integer")
        if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line):
            raise MirrorError("invalid_line", "end_line must be an integer at or after start_line")
        with self.read_guard():
            oid, metadata = self._resolve(tree, ref)
            entry, data = self._blob(tree, oid, path)
            if b"\x00" in data:
                raise MirrorError("code_binary_file", "Binary blob is not returned as text")
            try:
                data.decode("utf-8")
            except UnicodeError as exc:
                raise MirrorError("code_encoding_unsupported", "Blob is not valid UTF-8 text") from exc
            total_lines = data.count(b"\n") + bool(data and not data.endswith(b"\n"))
            stop = min(end_line or total_lines, total_lines)
            items, offset, number = [], 0, 1
            reasons = []
            while offset < len(data) and number <= stop:
                if number % 256 == 0 and time.monotonic() >= self.local.deadline:
                    raise MirrorError("query_timeout", "Code line scan exceeded its complete request deadline", 504)
                newline = data.find(b"\n", offset)
                end = newline if newline >= 0 else len(data)
                if number >= start_line:
                    if len(items) >= self.limits["scan_limit"]:
                        reasons.append("line_scan_limit")
                        break
                    if end - offset > self.limits["max_line_bytes"]:
                        raise MirrorError("code_line_too_large", "A selected line exceeds the line-byte limit; request a different range", 413)
                    items.append({"line": number, "text": data[offset:end].decode("utf-8")})
                offset, number = end + 1, number + 1
            metadata.update({"path": path, "blob": entry["blob"], "mode": entry["mode"], "encoding": "utf-8", "file_bytes": len(data),
                             "total_lines": total_lines, "start_line": start_line,
                             "end_line": items[-1]["line"] if items else None, "requested_end_line": end_line,
                             "next_start_line": number if reasons else None,
                             "final_newline": data.endswith(b"\n"), "symlink_policy": "return_link_blob_never_follow"})
            return self._result(items, metadata, reasons)

    @staticmethod
    def _batches(paths):
        batch, size = [], 0
        for path in paths:
            if batch and (len(batch) >= 128 or size + len(path.encode()) > 16384):
                yield batch
                batch, size = [], 0
            batch.append(path)
            size += len(path.encode()) + 1
        if batch:
            yield batch

    def search(self, query, tree, ref=None, paths=None, mode="literal", case_sensitive=True, limit=None):
        if not isinstance(query, str) or not query or len(query.encode("utf-8")) > 4096 or "\x00" in query or "\n" in query:
            raise MirrorError("invalid_query", "Query must be 1..4096 UTF-8 bytes without NUL or newline")
        if mode not in ("literal", "regex", "symbol"):
            raise MirrorError("unsupported_search_mode", "Search supports literal, POSIX ERE, or optional lexical symbols")
        if mode == "regex" and ("(?" in query or re.search(r"\\[dDsSwWbBAZz1-9]", query)):
            raise MirrorError("unsupported_regex", "Regex dialect is POSIX ERE; PCRE extensions and backreferences are not supported")
        if not isinstance(case_sensitive, bool):
            raise MirrorError("invalid_arguments", "case_sensitive must be boolean")
        paths, limit = self._paths(paths), self._limit(limit)
        with self.read_guard():
            oid, metadata = self._resolve(tree, ref)
            indexed, fallback = self._indexed_search(tree, oid, metadata, query, paths, mode, case_sensitive, limit)
            if indexed is not None:
                return indexed
            metadata["index_fallback_reason"] = fallback
            entries, reason = self._entries(tree, oid, paths)
            reasons, eligible, exclusions = [reason], {}, {"binary_files": 0, "oversize_files": 0, "non_regular_files": 0, "non_utf8_paths": 0}
            for entry in entries:
                if entry.get("excluded"):
                    exclusions["non_utf8_paths"] += 1
                elif entry["type"] != "blob" or entry["mode"] not in ("100644", "100755"):
                    exclusions["non_regular_files"] += 1
                elif entry["size"] > self.limits["max_file_bytes"]:
                    exclusions["oversize_files"] += 1
                else:
                    eligible[entry["path"]] = entry
            if len(eligible) > self.limits["scan_limit"]:
                eligible = dict(list(eligible.items())[:int(self.limits["scan_limit"])])
                reasons.append("file_scan_limit")
            items = []
            prefix = (oid + ":").encode()
            for batch in self._batches(eligible):
                # Empty-pattern -I identifies text files, so binary exclusions
                # are counted rather than silently hidden behind no matches.
                texts = self._run(tree, ["grep", "--no-textconv", "--no-recurse-submodules", "--threads=1", "-I", "-l", "-z", "-e", "", oid, "--", *batch], allow_partial=True, allow_one=True)
                reasons.append(texts.partial_reason)
                names = {raw[len(prefix):].decode("utf-8") for raw in texts.output.split(b"\x00")[:-1] if raw.startswith(prefix)}
                if not texts.partial_reason:
                    exclusions["binary_files"] += sum(1 for path in batch if path not in names and eligible[path]["size"] > 0)
                args = ["grep", "--no-textconv", "--no-recurse-submodules", "--threads=1", "--full-name", "--no-color", "-I", "-n", "-z", "-F" if mode == "literal" else "-E"]
                if not case_sensitive:
                    args.append("-i")
                result = self._run(tree, [*args, "-e", query, oid, "--", *batch], allow_partial=True, allow_one=True)
                reasons.append(result.partial_reason)
                offset = 0
                while offset < len(result.output):
                    first = result.output.find(b"\x00", offset)
                    second = result.output.find(b"\x00", first + 1) if first >= 0 else -1
                    end = result.output.find(b"\n", second + 1) if second >= 0 else -1
                    if min(first, second, end) < 0:
                        break
                    name, number, raw = result.output[offset:first], result.output[first + 1:second], result.output[second + 1:end]
                    offset = end + 1
                    if not name.startswith(prefix) or not number.isdigit():
                        raise MirrorError("code_backend_error", "Malformed local Git search output", 503)
                    path = name[len(prefix):].decode("utf-8")
                    if path not in eligible:
                        raise MirrorError("code_backend_error", "Git search returned a file outside its literal scope", 503)
                    clipped = len(raw) > self.limits["max_line_bytes"]
                    text = raw[:int(self.limits["max_line_bytes"])].decode("utf-8", "replace")
                    items.append({"tree": tree, "requested_ref": metadata["requested_ref"], "commit": oid,
                                  "path": path, "blob": eligible[path]["blob"], "line": int(number), "text": text,
                                  "text_truncated": clipped, "encoding": "utf-8-with-replacement"})
                    if clipped:
                        reasons.append("match_line_truncated")
                    if len(items) >= limit:
                        reasons.append("match_limit")
                        break
                if result.partial_reason or texts.partial_reason or len(items) >= limit:
                    break
            if any(exclusions.values()):
                reasons.append("excluded_files")
            metadata.update({"query": query, "paths": paths, "mode": mode, "regex_dialect": "POSIX ERE" if mode == "regex" else None,
                             "case_sensitive": case_sensitive, "exclusions": exclusions,
                             "policies": {"binary": "exclude", "max_file_bytes": self.limits["max_file_bytes"], "max_line_bytes": self.limits["max_line_bytes"], "symlinks_and_gitlinks": "exclude"},
                             "coverage": "one_exact_commit_regular_text_blobs_only", "candidate_files": len(eligible)})
            return self._result(items, metadata, reasons)

    def _commit(self, tree, oid):
        # Batch framing verifies the exact object's type as well as its size;
        # `cat-file commit <tag>` would otherwise peel and mislabel the result.
        result = self._run(tree, ["cat-file", "--batch"], input_bytes=(oid + "\n").encode("ascii"),
                           max_bytes=self.limits["max_file_bytes"] + 128, allow_partial=True)
        line, separator, body = result.output.partition(b"\n")
        identity = line.split()
        if identity == [oid.encode("ascii"), b"missing"]:
            raise MirrorError("code_commit_not_published", "Commit is unknown locally", 404)
        if len(identity) == 3 and identity[1] != b"commit":
            raise MirrorError("code_commit_not_published", "Requested object is not a commit", 404)
        if len(identity) == 3 and identity[2].isdigit() and int(identity[2]) > self.limits["max_file_bytes"]:
            raise MirrorError("code_commit_too_large", "Commit metadata exceeds its byte budget", 413)
        if result.partial_reason:
            raise MirrorError(result.partial_reason, "Commit read exceeded its resource budget", 413 if "limit" in result.partial_reason else 504)
        if (not separator or len(identity) != 3 or identity[0] != oid.encode("ascii")
                or not identity[2].isdigit() or len(body) != int(identity[2]) + 1 or not body.endswith(b"\n")):
            raise MirrorError("code_objects_invalid", "Malformed local commit object framing", 503)
        raw = body[:-1]
        header, _, message = raw.partition(b"\n\n")
        fields = {}
        parents = []
        for line in header.splitlines():
            if line.startswith(b"parent "):
                try:
                    parent = line[7:].decode("ascii")
                except UnicodeError as exc:
                    raise MirrorError("code_objects_invalid", "Commit contains an invalid parent identity", 503) from exc
                if not OID.fullmatch(parent):
                    raise MirrorError("code_objects_invalid", "Commit contains an invalid parent identity", 503)
                parents.append(parent)
            elif line.startswith((b"author ", b"committer ", b"tree ")):
                key, value = line.split(b" ", 1)
                fields[key.decode()] = value.decode("utf-8", "replace")
        if not OID.fullmatch(fields.get("tree", "")):
            raise MirrorError("code_objects_invalid", "Commit contains an invalid tree identity", 503)
        text = message[:4096].decode("utf-8", "replace")
        return {"commit": oid, "parents": parents, "tree_oid": fields.get("tree"),
                "author": fields.get("author", "")[:1024], "committer": fields.get("committer", "")[:1024],
                "subject": text.split("\n", 1)[0][:1024], "message": text,
                "metadata_truncated": len(message) > 4096 or len(fields.get("author", "")) > 1024 or len(fields.get("committer", "")) > 1024}

    @staticmethod
    def _date(value):
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))?", value):
            raise MirrorError("invalid_date", "Dates must be YYYY-MM-DD or RFC3339 with an explicit timezone")
        try:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MirrorError("invalid_date", "Invalid calendar date") from exc
        return value

    def log(self, tree, ref=None, path=None, since=None, until=None, limit=None):
        paths = [self._path(path)] if path is not None else []
        limit, since, until = self._limit(limit), self._date(since), self._date(until)
        with self.read_guard():
            oid, metadata = self._resolve(tree, ref)
            # One subprocess supplies the whole bounded window. NUL separates
            # all six fields and records, so tabs/newlines in messages cannot
            # become fake commits. Never launch cat-file twice per history row.
            fmt = "%H%x00%P%x00%T%x00%an <%ae> %at %ai%x00%cn <%ce> %ct %ci%x00%B"
            args = ["log", "-z", "--no-show-signature", "--encoding=none", "--format=" + fmt,
                    "--no-renames", "--max-count=" + str(limit + 1)]
            if since:
                args.append("--since-as-filter=" + since)
            if until:
                args.append("--until=" + until)
            result = self._run(tree, [*args, oid, "--", *paths], allow_partial=True)
            fields = result.output.split(b"\x00")[:-1]
            reasons = [result.partial_reason, "commit_limit" if len(fields) // 6 > limit else None]
            if (len(fields) % 6 or (result.output and not result.output.endswith(b"\x00"))) and not result.partial_reason:
                raise MirrorError("code_backend_error", "Malformed local Git log output", 503)
            items = []
            for offset in range(0, min(len(fields) // 6, limit) * 6, 6):
                commit, parents, tree_oid, author, committer, message = fields[offset:offset + 6]
                if not all(OID.fullmatch(value.decode("ascii", "replace"))
                           for value in [commit, tree_oid, *parents.split()]):
                    raise MirrorError("code_backend_error", "Malformed local Git log identity", 503)
                # %ai/%ci end with the original numeric timezone. Remove the
                # intervening human date to preserve raw-commit header shape.
                author = author.rsplit(b" ", 3)[0] + b" " + author.rsplit(b" ", 1)[-1]
                committer = committer.rsplit(b" ", 3)[0] + b" " + committer.rsplit(b" ", 1)[-1]
                clipped = len(message) > 4096 or len(author) > 1024 or len(committer) > 1024
                text = message[:4096].decode("utf-8", "replace")
                items.append({"commit": commit.decode("ascii"), "parents": parents.decode("ascii").split(),
                              "tree_oid": tree_oid.decode("ascii"), "author": author[:1024].decode("utf-8", "replace"),
                              "committer": committer[:1024].decode("utf-8", "replace"), "subject": text.split("\n", 1)[0][:1024],
                              "message": text, "metadata_truncated": clipped})
                if clipped:
                    reasons.append("metadata_truncated")
            metadata.update({"path": path, "since": since, "until": until, "ordering": "git_default_reverse_chronological",
                             "path_history": "literal_path_no_rename_follow", "history_scope": "ancestors_of_exact_published_commit"})
            return self._result(items, metadata, reasons)

    def show(self, tree, commit, paths=None, part="metadata", limit=None):
        if not isinstance(commit, str) or not OID.fullmatch(commit):
            raise MirrorError("invalid_commit", "Use a full lowercase commit OID, not a Git revision expression")
        if part not in ("metadata", "diff"):
            raise MirrorError("invalid_part", "part must be metadata or diff")
        paths, limit = self._paths(paths), self._limit(limit)
        with self.read_guard():
            if not isinstance(tree, str) or tree not in self.trees:
                raise MirrorError("unknown_tree", "Unknown configured code tree", 404)
            refs = self._published_entry(tree).get("published_refs", {})
            if not refs:
                raise MirrorError("code_ref_not_found", "Tree has no locally published refs", 404)
            _, metadata = self._resolve(tree, next(iter(refs)))
            tips = list(dict.fromkeys(refs.values()))
            if not all(isinstance(oid, str) and OID.fullmatch(oid) for oid in tips):
                raise MirrorError("code_state_invalid", "Invalid published commit map", 503)
            reachable = commit in tips
            if not reachable:
                # Only ancestors of the exact published tips are eligible.
                # A single ancestry-path walk handles every tip, without
                # consulting mutable refs or spawning one Git per branch.
                try:
                    checked = self._run(tree, ["rev-list", "--ancestry-path", "--max-count=1", *tips,
                                               "^" + commit, "--"], max_bytes=128,
                                        accepted_returncodes=(0, 1, 128))
                except MirrorError as exc:
                    # The walk spans tip..commit, so an old commit in a large
                    # history can exceed the query budget; nothing to narrow.
                    if exc.code in ("query_memory_limit", "query_timeout"):
                        raise MirrorError("code_reachability_unknown", "Commit is too far below the published tips to verify within the query budget", 503) from None
                    raise
                if checked.returncode == 128:
                    missing = (b"fatal: bad object " + commit.encode("ascii"),
                               b"fatal: bad revision '^" + commit.encode("ascii") + b"'",
                               b"fatal: --ancestry-path given but there are no bottom commits")
                    if any(checked.stderr.strip() == line for line in missing):
                        raise MirrorError("code_commit_not_published", "Commit is unknown or not reachable from a published ref", 404)
                    raise MirrorError("code_objects_missing", "Published commit graph is unavailable locally", 503)
                reachable = bool(checked.output.strip())
            if not reachable:
                raise MirrorError("code_commit_not_published", "Commit is not reachable from a published ref", 404)
            item = self._commit(tree, commit)
            metadata.update({"commit": commit, "requested_ref": None, "part": part,
                             "reachability": "ancestor_of_published_ref", "merge_policy": "first_parent", "parent": item["parents"][0] if item["parents"] else None})
            if part == "metadata":
                return self._result([item], metadata, ["metadata_truncated"] if item["metadata_truncated"] else [])
            args = ["diff-tree", "--no-commit-id", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "-p", "--unified=3"]
            if item["parents"]:
                args.extend([item["parents"][0], commit])
            else:
                args.extend(["--root", commit])
            result = self._run(tree, [*args, "--", *paths], allow_partial=True)
            lines = result.output.split(b"\n")
            if lines and lines[-1] == b"":
                lines.pop()
            reasons = [result.partial_reason, "diff_line_limit" if len(lines) > limit else None]
            rows = []
            for number, raw in enumerate(lines[:limit], 1):
                clipped = len(raw) > self.limits["max_line_bytes"]
                rows.append({"line": number, "text": raw[:int(self.limits["max_line_bytes"])].decode("utf-8", "replace"), "text_truncated": clipped})
                if clipped:
                    reasons.append("diff_line_truncated")
            metadata.update({"paths": paths, "binary_policy": "git_binary_change_summary_no_payload", "encoding": "utf-8-with-replacement"})
            return self._result(rows, metadata, reasons)

    def list_trees(self):
        with self.read_guard():
            state = self.local.state
            rows = [{"tree": tree["id"], "name": tree.get("name", tree["id"]), "default_ref": tree.get("default_ref"),
                     "status": ("catalog_changed" if state["trees"].get(tree["id"], {}).get("published_refs") and not self._catalog_current(tree["id"], state["trees"][tree["id"]]) else state["trees"].get(tree["id"], {}).get("status", "not_ready")),
                     "catalog_current": self._catalog_current(tree["id"], state["trees"].get(tree["id"], {})),
                     "published_ref_count": len(state["trees"].get(tree["id"], {}).get("published_refs", {}))}
                    for tree in self.trees.values()]
            return self._result(rows, {"code_generation": str(state["generation"])})

    def list_refs(self, tree):
        with self.read_guard():
            if not isinstance(tree, str) or tree not in self.trees:
                raise MirrorError("unknown_tree", "Unknown configured code tree", 404)
            state = self.local.state
            entry = self._published_entry(tree)
            rows = [{"tree": tree, "ref": ref, "commit": oid, "indexed": False} for ref, oid in sorted(entry.get("published_refs", {}).items())]
            if any(not isinstance(row["commit"], str) or not OID.fullmatch(row["commit"]) for row in rows):
                raise MirrorError("code_state_invalid", "Invalid published commit map", 503)
            return self._result(rows, {"code_generation": str(state["generation"]), "fetched_at": entry.get("fetched_at"), "published_at": entry.get("published_at")})

    def status(self):
        try:
            state = self._state()
        except MirrorError as exc:
            return {"enabled": self.enabled, "ready": False, "available": False, "status": "invalid_state",
                    "error": exc.as_dict()["error"], "engine": "git",
                    "trees": [{"tree": key, "default_ref": tree.get("default_ref"), "status": "invalid_state"}
                              for key, tree in self.trees.items()]}
        rows = []
        for key, tree in self.trees.items():
            entry = state["trees"].get(key, {})
            refs = entry.get("published_refs", {})
            skipped = entry.get("skipped_refs", {})
            current = self._catalog_current(key, entry)
            error = (entry.get("last_error") or "").encode("utf-8")
            rows.append({"tree": key, "default_ref": tree.get("default_ref"),
                         "status": "catalog_changed" if refs and not current else entry.get("status", "not_ready"),
                         "catalog_current": current, "published_ref_count": len(refs),
                         "missing_requested_ref_count": len(set(tree.get("refs", [tree.get("default_ref")])) - set(refs)
                                                            - (set(skipped) - {tree.get("default_ref")})),
                         "checked_at": str(entry["checked_at"])[:40] if entry.get("checked_at") is not None else None,
                         "fetched_at": str(entry["fetched_at"])[:40] if entry.get("fetched_at") is not None else None,
                         "published_at": str(entry["published_at"])[:40] if entry.get("published_at") is not None else None,
                         "last_error": error[:160].decode("utf-8", "ignore") if error else None,
                         "last_error_truncated": len(error) > 160})
            row = rows[-1]
            if skipped:
                row.update(skipped_ref_count=len(skipped), skipped_ref_details_truncated=len(skipped) > 3,
                           skipped_refs=[{"ref": ref.encode("utf-8")[:128].decode("utf-8", "ignore"),
                                          "ref_truncated": len(ref.encode("utf-8")) > 128,
                                          "object_type": detail["object_type"], "reason": detail["reason"][:80]}
                                         for ref, detail in list(sorted(skipped.items()))[:3]])
            if "retry_blocked" in entry:
                row.update(retry_blocked=entry["retry_blocked"], blocked_reason=entry.get("blocked_reason"),
                           next_check_at=str(entry["next_check_at"])[:40] if entry.get("next_check_at") is not None else None)
            measurements = entry.get("measurements")
            if isinstance(measurements, dict):
                counts = {name: measurements[name] for name in
                          ("object_pack_count", "packed_ref_count", "generation_pin_count", "loose_ref_count")
                          if type(measurements.get(name)) is int and 0 <= measurements[name] <= 2**63 - 1}
                if counts:
                    row["storage_counts"] = counts
        available = self.enabled and any(row["published_ref_count"] and row["catalog_current"] for row in rows)
        ready = self.enabled and bool(rows) and all(row["status"] == "ready" and row["catalog_current"]
                                                  and row["missing_requested_ref_count"] == 0 for row in rows)
        changed_catalog = any(row["status"] == "catalog_changed" for row in rows)
        state_status = "disabled" if not self.enabled else "ready" if ready else "degraded" if available or changed_catalog else "not_ready"
        result = {"enabled": self.enabled, "ready": ready, "available": available, "status": state_status,
                "code_generation": str(state["generation"]), "engine": "git", "configured_trees": len(self.trees), "trees": rows,
                "code_index": self.search_backend.health(state),
                "capabilities": {"search": ["literal", "POSIX ERE"], "symbol": "optional_lexical_ctags" if self.code.get("index", {}).get("symbols", False) else False,
                                 "blame": False, "network": False, "memory_bound": "sampled_git_rss_plus_deployment_hard_limit",
                                 "index_engine": "zoekt" if self.code.get("index", {}).get("enabled", False) else None}}
        # Keep degraded diagnostics useful even at the maximum catalog size.
        # Discovery is paginated elsewhere; status must fit the usual envelope.
        budget = max(256, max(512, (self.cfg.get("limits", {}).get("response_bytes", 65536) - 1024) // 3) - 512)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > budget:
            result["status_details_truncated"] = True
            for row in rows:
                if row.pop("skipped_refs", None):
                    row["skipped_ref_details_truncated"] = True
                for key in ("checked_at", "fetched_at", "published_at"):
                    row.pop(key, None)
                if row["last_error"]:
                    raw = row["last_error"].encode("utf-8")
                    row["last_error"] = raw[:80].decode("utf-8", "ignore")
                    row["last_error_truncated"] |= len(raw) > 80
                ref = row.get("default_ref") or ""
                row["default_ref"] = ref[:128]
                row["default_ref_truncated"] = len(ref) > 128
        return result
