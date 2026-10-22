"""Phase-neutral validation for code publication, repositories and failures.

Only this module reads code-state.json. Callers translate validation failures
into their own API/coordinator contracts; this module imports no later phase.
"""
from __future__ import annotations

import datetime as dt
import errno
import json
import os
from pathlib import Path
import re
import stat

from .code_config import CODE_STATE_MAX_BYTES
from .code_runtime import code_ref_valid

OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TREE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SAFE_REPO_CONFIG = ("[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
                    "\tbare = true\n\tlogallrefupdates = false\n"
                    "[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
STORAGE_ERRNOS = frozenset((errno.ENOSPC, errno.EDQUOT, errno.EFBIG, errno.EROFS))
STORAGE_DIAGNOSTICS = ("no space left on device", "disk quota exceeded", "disk full",
                       "database or disk is full", "read-only file system", "file too large")
BLOCKED_REASONS = frozenset(("budget", "storage", "non_commit"))


class CodeStateError(ValueError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


class RepositoryOwnershipError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def empty_code_state():
    return {"schema_version": 1, "generation": 0, "trees": {}}


def _timestamp(value):
    # Preserve the existing writer's empty/zero sentinel, while rejecting the
    # malformed dates, booleans and unbounded values rejected by either reader.
    if value is None:
        return
    if isinstance(value, bool):
        raise ValueError("bad timestamp")
    if isinstance(value, (int, float)) and value == 0:
        return
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("bad timestamp")
    if value:
        dt.datetime.fromisoformat(value).timestamp()


def validate_code_state(state, *, max_refs=10000):
    """Validate the strict union of publication reader and writer schemas."""
    try:
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                or state["schema_version"] != 1 or type(state.get("generation")) is not int
                or state["generation"] < 0 or not isinstance(state.get("trees"), dict)):
            raise ValueError("bad state schema")
        for tree, row in state["trees"].items():
            if not isinstance(tree, str) or not TREE_ID.fullmatch(tree) or not isinstance(row, dict):
                raise ValueError("bad tree entry")
            if type(row.get("consecutive_errors", 0)) is not int or not 0 <= row.get("consecutive_errors", 0) <= 1000000:
                raise ValueError("bad consecutive errors")
            if "generation" in row and (type(row["generation"]) is not int or row["generation"] < 0):
                raise ValueError("bad tree generation")
            if "status" in row and (not isinstance(row["status"], str) or len(row["status"]) > 64):
                raise ValueError("bad tree status")
            if row.get("last_error") is not None and not isinstance(row["last_error"], str):
                raise ValueError("bad error text")
            for key in ("published_at", "fetched_at", "checked_at", "next_check_at", "started_at"):
                _timestamp(row.get(key))
            if type(row.get("retry_blocked", False)) is not bool or row.get("blocked_reason") not in BLOCKED_REASONS | {None}:
                raise ValueError("bad blocked retry")
            if row.get("retry_blocked") and (
                    row.get("blocked_reason") not in BLOCKED_REASONS
                    or not isinstance(row.get("blocked_config_fingerprint"), str)
                    or not SHA256.fullmatch(row["blocked_config_fingerprint"])
                    or row.get("next_check_at") is not None):
                raise ValueError("bad blocked retry")
            refs = row.get("published_refs", {})
            if not isinstance(refs, dict) or len(refs) > max_refs:
                raise ValueError("bad published refs")
            for ref, oid in refs.items():
                if not code_ref_valid(ref) or not isinstance(oid, str) or not OID.fullmatch(oid):
                    raise ValueError("bad published identity")
            skipped = row.get("skipped_refs", {})
            if not isinstance(skipped, dict) or len(skipped) > max_refs:
                raise ValueError("bad skipped refs")
            for ref, detail in skipped.items():
                if (not code_ref_valid(ref) or not isinstance(detail, dict)
                        or detail.get("object_type") not in ("tree", "blob")
                        or not isinstance(detail.get("object_id"), str) or not OID.fullmatch(detail["object_id"])
                        or detail.get("reason") != "peeled target is not a commit"):
                    raise ValueError("bad skipped ref diagnostic")
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise CodeStateError("invalid", "schema") from exc
    return state


def load_code_state(path, *, max_refs=10000, missing_ok=False):
    """Read one bounded regular checkpoint, without following its symlink."""
    path = Path(path)
    try:
        if path.is_symlink():
            raise CodeStateError("symlink", "symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CodeStateError("unavailable", "not_regular")
            if info.st_size > CODE_STATE_MAX_BYTES:
                raise CodeStateError("invalid", "oversized")
            raw = source.read(CODE_STATE_MAX_BYTES + 1)
        if len(raw) > CODE_STATE_MAX_BYTES:
            raise CodeStateError("invalid", "oversized")
        state = json.loads(raw)
    except FileNotFoundError as exc:
        if missing_ok:
            return empty_code_state()
        raise CodeStateError("unavailable", "missing") from exc
    except OSError as exc:
        raise CodeStateError("symlink" if exc.errno == errno.ELOOP else "unavailable", "read") from exc
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, CodeStateError):
            raise
        raise CodeStateError("invalid", "json") from exc
    return validate_code_state(state, max_refs=max_refs)


def validate_repo_ownership(repo):
    """Require a coordinator-owned full-history repository's on-disk layout.

The coordinator still proves bare-repository status using bounded Git after
initialization. This checker never modifies or initializes a repository.
"""
    repo = Path(repo)
    if repo.is_symlink():
        raise RepositoryOwnershipError("symlink")
    config = repo / "config"
    try:
        if config.is_symlink():
            raise RepositoryOwnershipError("config")
        expected = SAFE_REPO_CONFIG.encode("utf-8")
        fd = os.open(config, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size != len(expected)
                    or source.read(len(expected) + 1) != expected):
                raise RepositoryOwnershipError("config")
        shallow = repo / "shallow"
        if shallow.exists() or shallow.is_symlink():
            raise RepositoryOwnershipError("shallow")
        alternates = repo / "objects" / "info" / "alternates"
        if alternates.exists() or alternates.is_symlink():
            raise RepositoryOwnershipError("alternates")
        if any(path.is_symlink() for path in (repo / "objects", repo / "refs",
                repo / "objects" / "info", repo / "objects" / "pack")):
            raise RepositoryOwnershipError("layout")
    except (OSError, UnicodeError) as exc:
        raise RepositoryOwnershipError("config") from exc


def storage_error(value):
    """Classify storage failures without importing optional SQLite/index phases."""
    if isinstance(value, OSError) and value.errno in STORAGE_ERRNOS:
        return True
    # SQLite's documented primary FULL code is 13; mask extended error codes.
    sqlite_code = getattr(value, "sqlite_errorcode", None)
    if isinstance(sqlite_code, int) and sqlite_code & 0xff == 13:
        return True
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return any(needle in text.lower() for needle in STORAGE_DIAGNOSTICS)
