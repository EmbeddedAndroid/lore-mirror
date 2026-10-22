"""Opt-in, single-writer, bounded bare-Git mirroring. Never called by read APIs.

The atomic code-state.json is the publication boundary. Private fetch refs and
immutable generation pins may exist before it, but are never reader authority.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import email.utils
import fcntl
import fnmatch
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .sync import atomic_json, storage_available, tree_bytes, _aggregate_rss
from .code_config import CODE_STATE_MAX_BYTES, code_catalog_fingerprint
from .code_runtime import CodeRunError, bounded_run, code_ref_valid, stop_process_group
from .code_common import (OID, SAFE_REPO_CONFIG, CodeStateError, RepositoryOwnershipError,
    load_code_state, storage_error, validate_repo_ownership)

STAGING = "refs/lore/staging/"
MAX_STATE_BYTES = CODE_STATE_MAX_BYTES


class CodeSyncError(RuntimeError):
    pass


class CodeSyncDeadline(CodeSyncError):
    """Transient upstream/unit slowness, retried with backoff capped at 24 hours."""


class CodeSyncBlocked(CodeSyncError):
    """A resource/policy failure that needs a changed configuration to retry."""
    def __init__(self, message, reason="budget"):
        super().__init__(message)
        self.reason = reason


def _blocked_reason(exc):
    if isinstance(exc, CodeSyncBlocked):
        return exc.reason
    # Git does not provide structured storage-error codes. Its stderr uses the
    # fixed C.UTF-8 locale; retain the original bounded diagnostic for operators.
    if storage_error(exc):
        return "storage"
    return None


def _retry_fingerprint(tree, code):
    """Bind an operator-unblocked attempt to mirror config and relevant limits."""
    fields = {"tree": tree, "max_refs": code["max_refs"],
        "min_free_fraction": code["min_free_fraction"], "checkpoint_bytes": MAX_STATE_BYTES,
        "limits": {key: code["limits"][key] for key in (
            "fetch_timeout_seconds", "fetch_memory_bytes", "repository_bytes", "max_fetch_bytes",
            "command_output_bytes", "manifest_bytes", "manifest_expanded_bytes")}}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _stamp(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()


def _epoch(value):
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        raise CodeSyncError("invalid code checkpoint timestamp") from None


def _trusted_url(url):
    """Reuse the strict configuration URL policy inside the network adapter."""
    from .code_config import validate_code_url
    from .errors import MirrorError
    try:
        parsed = urllib.parse.urlsplit(url)
        return validate_code_url(url, manifest=parsed.path == "/manifest.js.gz")
    except (MirrorError, ValueError, TypeError):
        raise CodeSyncError("code upstream must be a configured trusted HTTPS URL") from None


_ref_ok = code_ref_valid


def _safe_environment(home):
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"}


_stop = stop_process_group


class GitRunner:
    """Bound subprocess output, total unit time, sampled RSS and disk growth.

    Disk/RSS guards are sampled, not exact wire-byte or allocator limits. Use
    container limits too. Git's whole child process group is terminated on error.
    """
    def __init__(self, repo, root, code, *, lock_fds=()):
        self.repo, self.root, self.code = Path(repo), Path(root), code
        self.lock_fds = tuple(lock_fds)
        self.limits = code["limits"]
        self.deadline = time.monotonic() + self.limits["fetch_timeout_seconds"]
        self.baseline = tree_bytes(self.repo)["apparent"] if self.repo.exists() else 0
        self.peak_rss = 0
        self.peak_bytes = self.baseline
        self.home = self.root / "state" / "home"
        self.home.mkdir(parents=True, exist_ok=True)

    def _guard(self, child=None):
        if time.monotonic() >= self.deadline:
            raise CodeSyncDeadline("code job deadline exceeded")
        if child:
            self.peak_rss = max(self.peak_rss, _aggregate_rss(child.pid))
            if self.peak_rss > self.limits["fetch_memory_bytes"]:
                raise CodeSyncBlocked("code job aggregate RSS limit exceeded")
        size = tree_bytes(self.repo)["apparent"] if self.repo.exists() else 0
        self.peak_bytes = max(self.peak_bytes, size)
        if size > self.limits["repository_bytes"]:
            raise CodeSyncBlocked("code repository storage budget exceeded", "storage")
        if size - self.baseline > self.limits["max_fetch_bytes"]:
            raise CodeSyncBlocked("code added-disk byte budget exceeded")
        if storage_available(self.root, self.code["min_free_fraction"]) < 256 * 1024**2:
            raise CodeSyncBlocked("code storage guard: preserving free-space headroom", "storage")

    def git(self, args, *, input_bytes=None, bare=True):
        self._guard()
        settings = ("core.hooksPath=/dev/null", "core.fsmonitor=false", "core.attributesFile=/dev/null",
            "credential.helper=", "credential.interactive=false", "protocol.allow=never",
            "protocol.https.allow=always", "http.followRedirects=false", "http.sslVerify=true",
            "gc.auto=0", "maintenance.auto=false", "core.fsync=all", "core.fsyncMethod=fsync", "fetch.recurseSubmodules=false",
            "fetch.fsckObjects=true", "transfer.fsckObjects=true", "fetch.unpackLimit=1",
            "http.userAgent=lore-mirror-code/0.1 (bounded bare mirror)")
        argv = ["git"] + [v for setting in settings for v in ("-c", setting)]
        if bare:
            argv += ["--git-dir", str(self.repo)]
        argv += list(args)
        cap = int(self.limits["command_output_bytes"])
        try:
            result = bounded_run(argv, env=_safe_environment(self.home), deadline=self.deadline,
                max_bytes=cap, combined_output_bytes=cap, stderr_bytes=cap,
                input_bytes=input_bytes, max_input_bytes=cap, guard=self._guard, guard_interval=.5,
                memory_bytes=self.limits["fetch_memory_bytes"], pass_fds=self.lock_fds)
        except CodeRunError as exc:
            raise CodeSyncBlocked("code command input exceeds byte limit") from exc
        if result.partial_reason:
            if result.partial_reason == "timeout":
                raise CodeSyncDeadline("code job deadline exceeded")
            diagnostic = {"memory_limit": "code job aggregate RSS limit exceeded"}.get(
                    result.partial_reason, "code command output exceeds byte limit")
            raise CodeSyncBlocked(diagnostic)
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace")[-1500:].strip()
            raise CodeSyncError(f"git command failed ({result.returncode}): {detail}")
        return result.output

    def prepare(self):
        if self.repo.is_symlink():
            raise CodeSyncError("code repository must not be a symlink")
        marker = self.root / "state" / ("initializing-" + self.repo.name + ".json")
        expected = {"schema_version": 1, "repository": str(self.repo)}
        initializing = marker.exists()
        if initializing:
            try:
                if (marker.is_symlink() or marker.stat().st_size > 4096
                        or json.loads(marker.read_text()) != expected):
                    raise ValueError
            except (ValueError, TypeError, OSError):
                raise CodeSyncError("invalid code initialization marker; operator recovery required") from None
        elif not self.repo.exists():
            # Persist ownership before mkdir/init. A killed or budget-stopped
            # init may leave only a directory or config, not a usable bare repo.
            atomic_json(marker, expected)
            initializing = True
        config = self.repo / "config"
        if initializing:
            self.repo.mkdir(parents=True, exist_ok=True)
            if config.is_symlink() or config.exists() and not config.is_file():
                raise CodeSyncError("code repository configuration is not coordinator-owned")
            def write_config():
                with config.open("w") as out:
                    out.write(SAFE_REPO_CONFIG)
                    out.flush()
                    os.fsync(out.fileno())
            # Normalize only our marked incomplete initialization, never an
            # arbitrary existing repository. Git init is idempotent and does
            # not remove existing objects, refs or uncertain lock files.
            write_config()
            try:
                self.git(["init", "--bare", "--template=", str(self.repo)], bare=False)
            finally:
                if config.is_file() and not config.is_symlink():
                    write_config()
        try:
            validate_repo_ownership(self.repo)
        except RepositoryOwnershipError as exc:
            messages = {"symlink": "code repository must not be a symlink",
                "config": "code repository configuration is not coordinator-owned",
                "shallow": "shallow repositories are not valid full-history mirrors",
                "alternates": "code object alternates are not supported",
                "layout": "code repository layout is not coordinator-owned"}
            raise CodeSyncError(messages[exc.reason]) from None
        if self.git(["rev-parse", "--is-bare-repository"]).strip() != b"true":
            raise CodeSyncError("code repository is not bare")
        # A fetch killed at its deadline leaves temporary packs that Git never
        # resumes. Every helper inherits the writer lock this unit now holds,
        # so none of them is still writing.
        for temporary in (self.repo / "objects" / "pack").glob("tmp_*"):
            temporary.unlink(missing_ok=True)
        self.baseline = self.peak_bytes = tree_bytes(self.repo)["apparent"]
        if initializing:
            marker.unlink()
            directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def refs(self, prefix):
        data = self.git(["for-each-ref", "--format=%(refname) %(objectname)", prefix])
        result = {}
        for line in data.decode("utf-8", "strict").splitlines():
            ref, oid = line.split(" ", 1)
            if not ref.startswith(prefix) or not OID.fullmatch(oid):
                raise CodeSyncError("invalid local ref inventory")
            result[ref] = oid
        return result

    def remove_staging(self):
        refs = self.refs(STAGING)
        if refs:
            self.git(["update-ref", "--stdin"], input_bytes=("start\n" + "".join(
                f"delete {ref}\n" for ref in refs) + "prepare\ncommit\n").encode())

    def peeled_objects(self, names):
        """Peel annotated tags recursively, then report the actual object type."""
        values = list(names)
        if not values:
            return {}
        data = self.git(["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input_bytes="".join(name + "^{}\n" for name in values).encode())
        lines = data.decode("ascii", "strict").splitlines()
        if len(lines) != len(values):
            raise CodeSyncError("incomplete published object validation")
        result = {}
        for name, line in zip(values, lines):
            oid, kind = line.split(" ", 1)
            if kind not in ("commit", "tree", "blob") or not OID.fullmatch(oid):
                raise CodeSyncError("selected ref has no locally available object")
            result[name] = (oid, kind)
        return result

    def commit_map(self, names):
        result = {}
        for name, (oid, kind) in self.peeled_objects(names).items():
            if kind != "commit":
                raise CodeSyncBlocked("selected ref has no locally available commit", "non_commit")
            result[name] = oid
        return result

    def storage_counts(self):
        """Count ref storage without materializing every historical pin in RAM."""
        loose = set()
        refdir = self.repo / "refs"
        if refdir.exists():
            loose = {"refs/" + str(path.relative_to(refdir)) for path in refdir.rglob("*")
                if path.is_file() and not path.name.endswith(".lock")}
        pins = sum(ref.startswith("refs/lore/generations/") for ref in loose)
        packed = 0
        path = self.repo / "packed-refs"
        if path.exists():
            with path.open() as stream:
                for line in stream:
                    if line.startswith(("#", "^")) or not line.strip():
                        continue
                    _, ref = line.rstrip("\n").split(" ", 1)
                    packed += 1
                    if ref not in loose and ref.startswith("refs/lore/generations/"):
                        pins += 1
        return {"object_pack_count": sum(1 for _ in (self.repo / "objects" / "pack").glob("*.pack")),
                "packed_ref_count": packed, "generation_pin_count": pins, "loose_ref_count": len(loose)}


class GitMirrorAdapter:
    """Production network adapter. Tests inject a separate offline adapter."""
    def remote_refs(self, tree, runner):
        url = _trusted_url(tree["url"])
        patterns = (tree["refs"] + [p["pattern"] for p in tree["tag_patterns"]]
                    + [p["pattern"] for p in tree.get("branch_patterns", [])])
        return runner.git(["ls-remote", "--refs", "--", url, *patterns])

    def fetch(self, tree, refs, runner):
        url = _trusted_url(tree["url"])
        specs = [f"+{ref}:{STAGING}{ref[5:]}" for ref in refs]
        runner.git(["fetch", "--atomic", "--no-tags", "--no-recurse-submodules",
            "--no-write-fetch-head", "--no-auto-maintenance", "--", url, *specs])

    def manifest(self, url, headers, limit, timeout):
        _trusted_url(url)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                raise CodeSyncError("code manifest redirects are disabled; configure its canonical URL")
        request = urllib.request.Request(url, headers={
            "User-Agent": "lore-mirror-code/0.1 (bounded bare mirror)", **headers})
        deadline = time.monotonic() + timeout
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=min(timeout, 30)) as response:
                chunks, total = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise CodeSyncDeadline("code manifest deadline exceeded")
                    chunk = response.read1(min(65536, limit + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > limit:
                        raise CodeSyncBlocked("code manifest compressed byte limit exceeded")
                return 200, b"".join(chunks), dict(response.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, b"", dict(exc.headers)
            raise


def _manifest_fingerprints(code, tree, adapter):
    url = _trusted_url(tree["manifest_url"])
    keys = {t["manifest_key"] for t in code["trees"]
            if t["fetch_mode"] == "grokmirror_manifest" and t["manifest_url"] == url}
    path = Path(code["root_dir"]) / "state" / ("manifest-" + hashlib.sha256(url.encode()).hexdigest() + ".json")
    cached = {}
    if path.exists():
        try:
            if path.stat().st_size > code["limits"]["manifest_expanded_bytes"]:
                raise ValueError
            cached = json.loads(path.read_text())
            if cached.get("url") != url or not isinstance(cached.get("entries"), dict):
                raise ValueError
        except (ValueError, OSError, TypeError):
            cached = {}
    headers = {}
    if keys.issubset(cached.get("entries", {})):
        for name, key in (("If-None-Match", "etag"), ("If-Modified-Since", "last_modified")):
            value = cached.get(key)
            if isinstance(value, str) and len(value) <= 1024 and "\r" not in value and "\n" not in value:
                headers[name] = value
    status, raw, received = adapter.manifest(url, headers, code["limits"]["manifest_bytes"],
        min(60, code["limits"]["fetch_timeout_seconds"]))
    if status == 304:
        if not headers or not keys.issubset(cached.get("entries", {})):
            raise CodeSyncError("manifest returned 304 without a usable local cache")
        entries = cached["entries"]
    elif status == 200:
        if len(raw) > code["limits"]["manifest_bytes"]:
            raise CodeSyncBlocked("code manifest compressed byte limit exceeded")
        if raw[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                plain = stream.read(code["limits"]["manifest_expanded_bytes"] + 1)
        else:
            plain = raw
        if len(plain) > code["limits"]["manifest_expanded_bytes"]:
            raise CodeSyncBlocked("code manifest expanded byte limit exceeded")
        document = json.loads(plain)
        if not isinstance(document, dict) or len(document) > 100000:
            raise CodeSyncError("invalid grokmirror manifest")
        entries = {}
        for key in keys:
            row = document.get(key)
            fingerprint = row.get("fingerprint") if isinstance(row, dict) else None
            if fingerprint is not None and (not isinstance(fingerprint, str) or not OID.fullmatch(fingerprint)):
                raise CodeSyncError(f"invalid configured manifest fingerprint: {key}")
            # Missing sibling trees must not prevent this tree from syncing.
            entries[key] = fingerprint
        lower = {k.lower(): v for k, v in received.items()}
        cached = {"url": url, "entries": entries, "etag": lower.get("etag"),
                  "last_modified": lower.get("last-modified")}
        atomic_json(path, cached)
    else:
        raise CodeSyncError(f"unexpected manifest response: {status}")
    if not all(value is None or isinstance(value, str) and OID.fullmatch(value) for value in entries.values()):
        raise CodeSyncError("invalid cached manifest fingerprint")
    return entries


def _tag_version_key(ref):
    """Natural numeric ordering; a release's -rcN sorts before its final tag."""
    match = re.fullmatch(r"(.*)-rc([0-9]+)", ref)
    base, rc = (match.group(1), int(match.group(2))) if match else (ref, None)
    natural = tuple((1, int(part)) if part.isdigit() else (0, part)
                    for part in re.split(r"([0-9]+)", base) if part)
    # Ref name is a deterministic tie-break for equivalent numeric spellings.
    return natural, rc is None, rc or 0, ref


def _selected_refs(raw, tree, maximum):
    available = {}
    for line in raw.decode("utf-8", "strict").splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not OID.fullmatch(parts[0]) or not _ref_ok(parts[1]):
            raise CodeSyncError("invalid upstream ref advertisement")
        oid, ref = parts
        if ref in available and available[ref] != oid:
            raise CodeSyncError("conflicting upstream ref advertisement")
        available[ref] = oid
        if len(available) > maximum:
            raise CodeSyncBlocked("upstream ref inventory exceeds max_refs")
    missing = set(tree["refs"]) - set(available)
    if missing:
        raise CodeSyncError("configured upstream refs missing: " + ", ".join(sorted(missing)))
    selected = {ref: available[ref] for ref in tree["refs"]}
    for selector in tree.get("branch_patterns", []):
        # Configuration admits one trailing wildcard after a literal heads
        # prefix. Interpret only its suffix as a dotted numeric release, never
        # lexicographically (7.10 is newer than 7.9), and exclude -rc/fixes.
        prefix = selector["pattern"][:-1]
        candidates = []
        for ref in available:
            if ref.startswith(prefix):
                suffix = ref[len(prefix):]
                if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
                    candidates.append((tuple(int(part) for part in suffix.split(".")), ref))
        if not candidates:
            raise CodeSyncError("configured upstream release branch pattern missing: " + selector["pattern"])
        # Equal numeric versions with different spellings have a stable name
        # tiebreak. Selection does not expand the independently set index_refs.
        newest = max(candidates)[1]
        selected[newest] = available[newest]
    for selector in tree["tag_patterns"]:
        matches = sorted((ref for ref in available if ref.startswith("refs/tags/")
                          and fnmatch.fnmatchcase(ref, selector["pattern"])), key=_tag_version_key, reverse=True)
        if selector["keep"] is not None:
            matches = matches[:selector["keep"]]
        selected.update((ref, available[ref]) for ref in matches)
    if not selected or len(selected) > maximum:
        raise CodeSyncBlocked("selected ref set is empty or exceeds max_refs")
    return dict(sorted(selected.items()))


def _read_state(path, max_refs):
    try:
        return load_code_state(path, max_refs=max_refs, missing_ok=True)
    except CodeStateError:
        raise CodeSyncError("invalid code checkpoint; preserved for operator recovery") from None


def _check_state_size(state, *, reserve=0):
    # Keep space for final status/measurement fields when checking a candidate.
    if len(json.dumps(state, indent=2, sort_keys=True).encode("utf-8")) + 1 + reserve > MAX_STATE_BYTES:
        raise CodeSyncBlocked("code checkpoint byte limit exceeded; reduce catalog scope")


def _save_state(path, state):
    # Must match the read backend's admission cap. Never publish a manifest its
    # readers refuse to load, even when many long refs were selected.
    _check_state_size(state)
    atomic_json(path, state)


def _verify_published(runner, row):
    refs = row.get("published_refs", {})
    if refs:
        # Exact OIDs only, never mutable advertised refs.
        values = runner.commit_map(dict.fromkeys(refs.values()))
        if any(values.get(oid) != oid for oid in refs.values()):
            raise CodeSyncError("published code objects are unavailable")


def _publish(state, row, selected, runner, tree, now, signature, fingerprint):
    staged = runner.refs(STAGING)
    if staged != {STAGING + ref[5:]: oid for ref, oid in selected.items()}:
        raise CodeSyncError("upstream refs changed during fetch; publication deferred")
    objects = runner.peeled_objects(staged)
    published, skipped = {}, {}
    for ref in selected:
        oid, kind = objects[STAGING + ref[5:]]
        if kind == "commit":
            published[ref] = oid
        else:
            skipped[ref] = {"object_id": oid, "object_type": kind, "reason": "peeled target is not a commit"}
    # A tree/blob tag such as Linux v2.6.11 is not a mirror-wide error. Keep it
    # in the advertised map for unchanged checks, but never advertise it as code.
    row["skipped_refs"] = skipped
    if not published or tree["default_ref"] not in published:
        raise CodeSyncBlocked("selected default ref has no commit target; non-commit refs skipped", "non_commit")
    generation = state["generation"] + 1
    old = row.get("published_refs", {})
    published_row = dict(row)
    published_row.update(published_refs=published, source_refs=selected, skipped_refs=skipped, generation=generation,
        upstream_url=tree["url"],
        previous_generation=row.get("generation"), published_at=_stamp(now), fetched_at=_stamp(now),
        config_fingerprint=signature, manifest_fingerprint=fingerprint,
        ref_changes={"moved": sorted(ref for ref in old.keys() & published.keys() if old[ref] != published[ref]),
                     "removed": sorted(old.keys() - published.keys()),
                     "added": sorted(published.keys() - old.keys())})
    published_row.pop("pending_generation", None)
    candidate = dict(state, generation=generation, trees=dict(state["trees"]))
    candidate["trees"][tree["id"]] = published_row
    _check_state_size(candidate, reserve=4096)
    pins = "".join(f"update refs/lore/generations/{generation}/{ref[5:]} {oid}\n"
                   for ref, oid in selected.items() if ref in published)
    runner.git(["update-ref", "--stdin"], input_bytes=("start\n" + pins + "prepare\ncommit\n").encode())
    # Compact refs after pin publication, before the sole JSON boundary. This
    # is ref packing, not object repacking, garbage collection or pin expiry.
    runner.remove_staging()
    runner.git(["pack-refs", "--all", "--prune"])
    # core.fsync=all requests durable objects/refs before the fsynced JSON.
    row.clear()
    row.update(published_row)
    state["generation"] = generation
    # Caller publishes final ready status in the same atomic write.


def _backoff(exc, failures, now):
    # Slow upstreams remain retryable, but repeated full-deadline work must
    # eventually be paced to once per day. Preserve other transient limits.
    ceiling = 86400 if isinstance(exc, CodeSyncDeadline) else 3600
    delay = min(ceiling, 60 * 2 ** min(failures - 1, 11))
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 503):
        value = exc.headers.get("Retry-After", "")
        try:
            delay = max(delay, int(value))
        except (TypeError, ValueError):
            try:
                delay = max(delay, email.utils.parsedate_to_datetime(value).timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(86400, max(1, delay))


def _record_failure(row, exc, tree, code, finished):
    failures = row.get("consecutive_errors", 0) + 1
    reason = _blocked_reason(exc)
    row.update(status="blocked" if reason or not row.get("published_refs") else "degraded",
        checked_at=_stamp(finished), last_error=str(exc)[:2000], consecutive_errors=failures,
        retry_blocked=bool(reason), blocked_reason=reason,
        blocked_config_fingerprint=_retry_fingerprint(tree, code) if reason else None,
        next_check_at=None if reason else _stamp(finished + _backoff(exc, failures, finished)), last_result="failed")
    row.pop("pending_generation", None)


def _bind_upstream(root, tree, row, repo):
    """Persist identity independently of removable catalog/status rows."""
    path = root / "state" / ("upstream-" + tree["id"] + ".json")
    expected = {"schema_version": 1, "id": tree["id"], "url": tree["url"]}
    if path.exists():
        try:
            if path.stat().st_size > 4096 or json.loads(path.read_text()) != expected:
                raise ValueError
        except (ValueError, OSError, TypeError):
            raise CodeSyncError("tree upstream binding changed; use a new tree ID or operator migration") from None
    else:
        if repo.exists() and row.get("upstream_url") != tree["url"]:
            raise CodeSyncError("existing repository has no upstream binding; operator recovery required")
        atomic_json(path, expected)


def sync_code_once(config, *, now=None, adapter=None, should_yield=None):
    """Run at most one due tree by default, between mail coordinator units.

    ``config`` is the full mail/API configuration. ``adapter`` is Python-only
    dependency injection for offline fixture tests, never a config/API option.
    Return last-good publication plus health; errors never replace its ref map.
    Callers catch CodeSyncError for a corrupt checkpoint or concurrent writer.
    """
    from .code_config import normalize_code_config
    code = normalize_code_config(config.get("code"), data_dir=config["data_dir"])
    if not code["enabled"]:
        return {"schema_version": 1, "generation": 0, "status": "disabled", "trees": {}}
    now = time.time() if now is None else float(now)
    root = Path(code["root_dir"])
    Path(config["data_dir"]).mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "code-state.json"
    adapter = adapter or GitMirrorAdapter()
    with (root / "code-sync.lock").open("a") as writer:
        try:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CodeSyncError("another code coordinator is active") from None
        state = _read_state(state_path, code["max_refs"])
        state["enabled"] = True
        state["status"] = "syncing"
        # Retired catalog rows are not reader-visible. Their repositories and
        # immutable per-ID upstream bindings/pins remain on disk, untouched.
        active_ids = {tree["id"] for tree in code["trees"]}
        state["trees"] = {ident: row for ident, row in state["trees"].items() if ident in active_ids}
        # Recover journal status without claiming an interrupted fetch was ready.
        for tree in code["trees"]:
            row = state["trees"].setdefault(tree["id"], {"published_refs": {}, "status": "queued"})
            if row.get("status") == "fetching":
                row.update(status="degraded" if row.get("published_refs") else "queued",
                           last_error="previous code job interrupted", next_check_at=None)
                row.pop("pending_generation", None)
            desired = code_catalog_fingerprint(tree)
            retry_signature = _retry_fingerprint(tree, code)
            if row.get("retry_blocked"):
                if row.get("blocked_config_fingerprint") == retry_signature:
                    row.update(status="blocked", desired_config_fingerprint=desired)
                    continue
                row.update(retry_blocked=False, blocked_reason=None, blocked_config_fingerprint=None,
                           next_check_at=None, status="degraded" if row.get("published_refs") else "queued")
            if row.get("published_refs") and row.get("config_fingerprint") != desired:
                if row.get("desired_config_fingerprint") != desired:
                    row.update(next_check_at=None, last_error="configuration changed; publication pending")
                row["status"] = "degraded"
            row["desired_config_fingerprint"] = desired
        due = [t for t in code["trees"] if not state["trees"][t["id"]].get("retry_blocked")
               and _epoch(state["trees"][t["id"]].get("next_check_at")) <= now]
        due.sort(key=lambda t: (_epoch(state["trees"][t["id"]].get("next_check_at")), t["id"]))
        manifests = {}
        completed = 0
        for tree in due:
            if completed >= 1 or (should_yield and should_yield()):
                break
            row = state["trees"][tree["id"]]
            # Use the SAME inode/path as existing mail indexing. No lock is held
            # across trees; another reader/indexer causes a nonblocking yield.
            with (Path(config["data_dir"]) / "index.lock").open("a") as heavy:
                try:
                    fcntl.flock(heavy, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    state["status"] = "waiting_for_io"
                    break
                started = time.monotonic()
                runner = None
                previous_row, previous_generation = dict(row), state["generation"]
                # The saved journal always preserves published_refs.
                row.update(status="fetching", started_at=_stamp(now), pending_generation=state["generation"] + 1)
                try:
                    _save_state(state_path, state)
                    if row.get("published_refs") and row.get("upstream_url") != tree["url"]:
                        raise CodeSyncError("tree upstream changed or provenance missing; use a new tree ID or operator migration")
                    repo = root / "repositories" / (tree["id"] + ".git")
                    _bind_upstream(root, tree, row, repo)
                    runner = GitRunner(repo, root, code, lock_fds=(writer.fileno(), heavy.fileno()))
                    runner.prepare()
                    # Reconcile abandoned private refs even when the upstream
                    # returns to the last-good map and no fetch is necessary.
                    # This remains inside the writer/heavy locks and unit limits.
                    runner.remove_staging()
                    publication_valid = True
                    try:
                        _verify_published(runner, row)
                    except (CodeSyncBlocked, CodeSyncDeadline):
                        raise
                    except CodeSyncError:
                        # Refetch can repair missing objects. Never accept a
                        # matching fingerprint as proof local objects survived.
                        publication_valid = False
                    signature = code_catalog_fingerprint(tree)
                    fingerprint = None
                    if tree["fetch_mode"] == "grokmirror_manifest":
                        url = tree["manifest_url"]
                        if url not in manifests:
                            manifests[url] = _manifest_fingerprints(code, tree, adapter)
                        fingerprint = manifests[url].get(tree["manifest_key"])
                        if fingerprint is None:
                            raise CodeSyncError("configured repository absent from grokmirror manifest")
                    unchanged = (publication_valid and bool(row.get("published_refs")) and row.get("config_fingerprint") == signature
                        and fingerprint is not None and fingerprint == row.get("manifest_fingerprint"))
                    if not unchanged:
                        selected = _selected_refs(adapter.remote_refs(tree, runner), tree, code["max_refs"])
                        unchanged = (publication_valid and bool(row.get("published_refs")) and selected == row.get("source_refs")
                                     and signature == row.get("config_fingerprint"))
                        if not unchanged:
                            adapter.fetch(tree, selected, runner)
                            if (runner.repo / "shallow").exists():
                                raise CodeSyncError("upstream produced a shallow mirror")
                            _publish(state, row, selected, runner, tree,
                                     now + time.monotonic() - started, signature, fingerprint)
                    finished = now + time.monotonic() - started
                    # Deterministic per-tree jitter spreads upstream checks; pace
                    # from completion so an outage cannot create catch-up bursts.
                    jitter = int(hashlib.sha256(tree["id"].encode()).hexdigest()[:4], 16) / 65535 * .1
                    row.update(status="ready", checked_at=_stamp(finished), last_error=None, consecutive_errors=0,
                        retry_blocked=False, blocked_reason=None, blocked_config_fingerprint=None,
                        next_check_at=_stamp(finished + tree["interval_seconds"] * (1 + jitter)), manifest_fingerprint=fingerprint,
                        last_result="unchanged" if unchanged else "published")
                    row.pop("pending_generation", None)
                except Exception as exc:
                    _record_failure(row, exc, tree, code, now + time.monotonic() - started)
                finally:
                    row["measurements"] = {"seconds": round(time.monotonic() - started, 3),
                        "peak_aggregate_rss_bytes_sampled": runner.peak_rss if runner else 0,
                        "peak_repository_bytes_sampled": runner.peak_bytes if runner else 0,
                        "added_disk_bytes_sampled": max(0, runner.peak_bytes - runner.baseline) if runner else 0}
                    if runner:
                        with contextlib.suppress(OSError, ValueError):
                            row["measurements"].update(runner.storage_counts())
                    # The previous exact map stays authoritative if publication
                    # cannot be persisted. Record a durable block when storage
                    # still admits the smaller last-good failure checkpoint.
                    try:
                        _save_state(state_path, state)
                    except Exception as exc:
                        if not _blocked_reason(exc):
                            raise
                        measurements = row["measurements"]
                        row.clear()
                        row.update(previous_row)
                        state["generation"] = previous_generation
                        _record_failure(row, exc, tree, code, now + time.monotonic() - started)
                        row["measurements"] = measurements
                        _save_state(state_path, state)
                    finally:
                        fcntl.flock(heavy, fcntl.LOCK_UN)
                completed += 1
        if state.get("status") != "waiting_for_io":
            rows = [state["trees"][tree["id"]] for tree in code["trees"]]
            if any(row.get("status") in ("degraded", "blocked") for row in rows):
                state["status"] = "degraded"
            elif any(row.get("status") != "ready" or not row.get("published_refs") for row in rows):
                state["status"] = "partial" if any(row.get("published_refs") for row in rows) else "queued"
            else:
                state["status"] = "ready"
        state["last_cycle_at"] = _stamp(now)
        _save_state(state_path, state)
        return state
