"""Single-pilot, hard-cgroup-gated immutable Zoekt builds. No API entry point."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .code_config import CODE_STATE_MAX_BYTES, code_catalog_fingerprint, normalize_code_config
from .code_sync import _safe_environment
from .code_common import (OID, SHA256, CodeStateError, RepositoryOwnershipError,
    load_code_state, validate_repo_ownership, storage_error)
from .code_engine import (ENGINE_COMMIT, INDEXER_BINARY, QUERY_BINARY, CTAGS_BINARY,
    MAX_TRIGRAM_COUNT, BUILD_MANIFEST, MAX_BINARY_BYTES, EngineIdentityError, verify_engine_identity)
from .code_runtime import CodeRunError, bounded_run
from .sync import atomic_json, storage_available, tree_bytes

MAX_SHARDS = 256
MAX_SOURCE_METADATA = 64 * 1024**2
NAMESPACE_INIT = Path("/usr/bin/tini")


class CodeIndexError(RuntimeError):
    pass


class CodeIndexBlocked(CodeIndexError):
    """A deterministic resource failure requiring changed limits before retry."""


def _budget_fingerprint(code):
    limits = {key: code["limits"][key] for key in ("command_output_bytes", "max_file_bytes")}
    options = code["index"]
    tree = next((tree for tree in code["trees"] if tree["id"] == options["pilot_tree"]), None)
    scope = code_catalog_fingerprint(tree) if tree else None
    return hashlib.sha256(json.dumps([limits, options, scope, code["min_free_fraction"]], sort_keys=True).encode()).hexdigest()


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _unescape_mount(value):
    return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m.group(1), 8)), value)


def _cgroup_directory(cgroup_text, mountinfo_text):
    """Resolve the current process's controller, including namespace mount roots."""
    groups = []
    for line in cgroup_text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3 or not fields[2].startswith("/"):
            continue
        if fields[0] == "0" and fields[1] == "":
            groups.append((2, fields[2]))
        elif "memory" in fields[1].split(","):
            groups.append((1, fields[2]))
    candidates = []
    for line in mountinfo_text.splitlines():
        before, sep, after = line.partition(" - ")
        if not sep:
            continue
        left, right = before.split(), after.split()
        if len(left) < 6 or len(right) < 3:
            continue
        root, mount = _unescape_mount(left[3]), _unescape_mount(left[4])
        if not root.startswith("/") or not mount.startswith("/"):
            continue
        for version, group in groups:
            matches = right[0] == "cgroup2" if version == 2 else (
                right[0] == "cgroup" and "memory" in right[2].split(","))
            if not matches or not (root == "/" or group == root or group.startswith(root.rstrip("/") + "/")):
                continue
            suffix = group[len(root):].lstrip("/") if root != "/" else group.lstrip("/")
            if any(part == ".." for part in Path(suffix).parts):
                continue
            candidates.append((len(root), version, Path(mount) / suffix))
    if not candidates:
        raise CodeIndexError("cannot resolve current-process memory cgroup; dedicated capped index container required")
    _, version, directory = max(candidates, key=lambda item: (item[0], item[1]))
    return version, directory


def _read_limit(path):
    value = Path(path).read_text().strip()
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise CodeIndexError("memory/swap cgroup limit is unlimited or unknown")
    return int(value)


def _dedicated_pid_namespace_init():
    """Verify the image's Tini is our namespace init and we are its direct child.

    Tini exits when its direct child exits, and Linux kills all remaining tasks
    when namespace PID 1 exits. Checking the actual executable, child argv,
    namespace and controller membership rejects host/shared PID namespaces,
    shell wrappers, docker exec and merely setting a container environment flag.
    Nothing here changes namespaces, cgroups or host security configuration.
    """
    try:
        if os.getpid() == 1 or os.getppid() != 1:
            return None
        namespace = os.readlink("/proc/self/ns/pid")
        if namespace != os.readlink("/proc/1/ns/pid"):
            return None
        if not os.path.samefile("/proc/1/exe", NAMESPACE_INIT):
            return None
        if Path("/proc/1/cgroup").read_text() != Path("/proc/self/cgroup").read_text():
            return None
        init_argv = Path("/proc/1/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        own_argv = Path("/proc/self/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        if init_argv[:2] != [os.fsencode(NAMESPACE_INIT), b"--"] or init_argv[2:] != own_argv:
            return None
        return namespace
    except OSError:
        return None


def _enforced_memory_limit(budget):
    """Require a hard v2 cap and verified whole-workload teardown; read only."""
    try:
        version, directory = _cgroup_directory(Path("/proc/self/cgroup").read_text(),
                                               Path("/proc/self/mountinfo").read_text())
        if version != 2:
            raise CodeIndexError("index build requires cgroup v2 memory and swap enforcement")
        memory = _read_limit(directory / "memory.max")
        swap = _read_limit(directory / "memory.swap.max")
        if not 0 < memory <= budget or swap != 0:
            raise CodeIndexError("index build requires a finite hard memory cap within its budget and no swap")
        oom_path = directory / "memory.oom.group"
        oom_group = _read_limit(oom_path) if oom_path.exists() else None
        namespace = None
        protection = "cgroup_oom_group"
        if oom_group != 1:
            namespace = _dedicated_pid_namespace_init() if oom_group in (None, 0) else None
            if namespace is None:
                raise CodeIndexError("index build requires memory.oom.group=1 or dedicated PID namespace with verified image init as PID 1")
            protection = "pid_namespace_init"
        return {"version": version, "path": str(directory), "memory_bytes": memory,
                "swap_bytes": swap, "oom_group": oom_group,
                "oom_protection": protection, "pid_namespace": namespace}
    except OSError as exc:
        raise CodeIndexError("memory/swap cgroup enforcement unavailable; dedicated capped index container required") from exc


def _cgroup_metrics(cap):
    """Read-only optional counters; never infer a process-only peak from these."""
    base = Path(cap["path"])
    def number(name):
        try:
            return _read_limit(base / name)
        except (CodeIndexError, OSError):
            return None
    peak = number("memory.peak")
    current = number("memory.current")
    events, read_bytes, write_bytes = None, None, None
    if cap["version"] == 2:
        try:
            with (base / "memory.events").open() as source:
                raw = source.read(65537)
            if len(raw) <= 65536:
                events = {key: int(value) for line in raw.splitlines()
                          for key, value in [line.split()] if value.isdigit()}
        except (OSError, ValueError):
            events = None
        try:
            with (base / "io.stat").open() as source:
                raw = source.read(65537)
            if len(raw) <= 65536:
                read_bytes = write_bytes = 0
                for line in raw.splitlines():
                    for item in line.split()[1:]:
                        key, value = item.split("=", 1)
                        if key == "rbytes":
                            read_bytes += int(value)
                        elif key == "wbytes":
                            write_bytes += int(value)
        except (OSError, ValueError):
            read_bytes = write_bytes = None
    return {"scope": "container cgroup lifetime, not process-only or build-only",
        "peak_memory_bytes": peak, "current_memory_bytes": current,
        "memory_events": events, "io_read_bytes": read_bytes, "io_write_bytes": write_bytes}


class BuildRunner:
    """Every child inherits the held flock, even if the supervisor is OOM-killed."""
    def __init__(self, code, root, lock_fd, cap, target=None):
        self.code, self.root, self.lock_fd = code, Path(root), lock_fd
        self.options, self.cap = code["index"], cap
        self.target = Path(target) if target else None
        self.started = time.monotonic()
        self.deadline = self.started + self.options["timeout_seconds"]
        self.peak_bytes = 0
        self.home = self.root / "state" / "index-home"
        if (self.root / "state").is_symlink() or self.home.is_symlink():
            raise CodeIndexError("index private-home paths must not traverse symlinks")
        self.home.mkdir(parents=True, exist_ok=True)

    def guard(self):
        if time.monotonic() >= self.deadline:
            raise CodeIndexBlocked("index build deadline exceeded")
        current = _enforced_memory_limit(self.options["memory_bytes"])
        if any(current.get(key) != self.cap.get(key) for key in (
                "version", "path", "memory_bytes", "swap_bytes", "oom_group", "oom_protection", "pid_namespace")):
            raise CodeIndexBlocked("index memory cgroup changed while building")
        if storage_available(self.root, self.code["min_free_fraction"]) < 256 * 1024**2:
            raise CodeIndexBlocked("index storage guard: preserving free-space headroom")
        size = tree_bytes(self.target)["apparent"] if self.target and self.target.exists() else 0
        self.peak_bytes = max(size, self.peak_bytes)
        if size > self.options["max_index_bytes"]:
            raise CodeIndexBlocked("index generation byte budget exceeded")

    def environment(self):
        env = _safe_environment(self.home)
        env.update(PATH="/usr/local/bin:/usr/bin:/bin", GOMAXPROCS="1",
            GOMEMLIMIT=str(self.cap["memory_bytes"] * 3 // 4),
            GIT_CONFIG_COUNT="5", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0="/dev/null",
            GIT_CONFIG_KEY_1="protocol.allow", GIT_CONFIG_VALUE_1="never",
            GIT_CONFIG_KEY_2="credential.helper", GIT_CONFIG_VALUE_2="",
            GIT_CONFIG_KEY_3="core.fsmonitor", GIT_CONFIG_VALUE_3="false",
            GIT_CONFIG_KEY_4="submodule.recurse", GIT_CONFIG_VALUE_4="false")
        if self.options["symbols"]:
            env["CTAGS_COMMAND"] = str(CTAGS_BINARY)
        return env

    def run(self, argv, *, input_bytes=None, consume=None, output_bytes=None):
        cap = self.code["limits"]["command_output_bytes"]
        maximum = output_bytes or cap
        try:
            result = bounded_run(argv, env=self.environment(), deadline=self.deadline,
                max_bytes=maximum, combined_output_bytes=maximum, stderr_bytes=cap,
                input_bytes=input_bytes, max_input_bytes=cap, consume=consume,
                pass_fds=(self.lock_fd,), guard=lambda child: self.guard())
        except CodeRunError as exc:
            raise CodeIndexBlocked("index command input byte limit exceeded") from exc
        if result.partial_reason:
            reason = {"timeout": "index build deadline exceeded", "output_limit": "index command output byte limit exceeded",
                      "stderr_limit": "index diagnostic byte limit exceeded"}.get(result.partial_reason, result.partial_reason)
            raise CodeIndexBlocked(reason)
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace")[-1500:]
            # A killed child may be the cgroup OOM victim. Never automatically
            # repeat that workload just because the coordinator survived.
            error = CodeIndexBlocked if result.returncode < 0 or storage_error(detail) else CodeIndexError
            raise error(f"index command failed ({result.returncode}): " + detail)
        return result.output


def _hash_file(path, maximum, runner=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise CodeIndexError("missing, symlinked, empty or oversized index artifact")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise CodeIndexBlocked("index artifact byte limit exceeded")
            digest.update(chunk)
            if runner:
                runner.guard()
    return digest.hexdigest()


def _read_json(path, maximum=CODE_STATE_MAX_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise CodeIndexError("missing, symlinked or oversized index/code checkpoint")
    try:
        with path.open("rb") as source:
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError, RecursionError):
        raise CodeIndexError("invalid index/code checkpoint; preserved for operator recovery") from None


def _save_state(path, state):
    if len(json.dumps(state, indent=2, sort_keys=True).encode()) + 1 > CODE_STATE_MAX_BYTES:
        raise CodeIndexBlocked("index state byte limit exceeded")
    atomic_json(path, state)


def _snapshot(code):
    tree = next(item for item in code["trees"] if item["id"] == code["index"]["pilot_tree"])
    ref = code["index"]["pilot_ref"]
    try:
        state = load_code_state(Path(code["root_dir"]) / "code-state.json", max_refs=code.get("max_refs", 10000))
    except CodeStateError as exc:
        if exc.detail == "schema":
            raise CodeIndexError("invalid published code state schema") from exc
        if exc.detail in ("json", "oversized"):
            raise CodeIndexError("invalid index/code checkpoint; preserved for operator recovery") from exc
        raise CodeIndexError("missing, symlinked or oversized index/code checkpoint") from exc
    trees = state["trees"]
    row = trees.get(tree["id"])
    if not isinstance(row, dict) or not isinstance(row.get("published_refs"), dict):
        raise CodeIndexError("invalid or unavailable published pilot ref map")
    oid = row["published_refs"].get(ref)
    if (not isinstance(oid, str) or not OID.fullmatch(oid)
            or row.get("config_fingerprint") != code_catalog_fingerprint(tree)
            or row.get("upstream_url") != tree["url"]):
        raise CodeIndexError("pilot requires an exact published ref with matching current catalog provenance")
    repo = Path(code["root_dir"]) / "repositories" / (tree["id"] + ".git")
    try:
        validate_repo_ownership(repo)
    except RepositoryOwnershipError as exc:
        raise CodeIndexError("pilot repository is not a coordinator-owned full-history bare mirror") from exc
    return tree, ref, oid, state["generation"], repo


def _source_budget(repo, oid, runner):
    """Bound source bytes without checking out or extracting an untrusted archive."""
    kind = runner.run(["git", "--git-dir", repo, "cat-file", "-t", oid]).strip()
    if kind != b"commit":
        raise CodeIndexError("published pilot commit is not locally available")
    pending = bytearray()
    result = {"source_bytes": 0, "files": 0, "oversized_files": 0, "symlinks": 0, "submodules": 0, "unsafe_paths": 0}
    def consume(chunk):
        pending.extend(chunk)
        while b"\0" in pending:
            record, _, rest = pending.partition(b"\0")
            pending[:] = rest
            header, sep, path = record.partition(b"\t")
            fields = header.split()
            if not sep or len(fields) != 4 or not path or len(path) > 4096:
                raise CodeIndexError("invalid or oversized Git tree metadata")
            try:
                path.decode("utf-8", "strict")
                unsafe = any(c < 32 or c == 127 for c in path)
            except UnicodeError:
                unsafe = True
            if path == b".sourcegraph/ignore":
                raise CodeIndexError("pilot contains .sourcegraph/ignore; exact Git fallback required")
            if unsafe:
                result["unsafe_paths"] += 1
                raise CodeIndexError("pilot contains control-character or non-UTF-8 paths; exact Git fallback required")
            mode, kind, object_id, size = fields
            if not OID.fullmatch(object_id.decode("ascii", "strict")):
                raise CodeIndexError("invalid source blob OID")
            if kind == b"commit" and mode == b"160000":
                result["submodules"] += 1
                continue
            if kind != b"blob" or not size.isdigit():
                raise CodeIndexError("unexpected pilot tree entry")
            number = int(size)
            result["source_bytes"] += number
            result["files"] += 1
            result["symlinks"] += int(mode == b"120000")
            result["oversized_files"] += int(number > runner.code["limits"]["max_file_bytes"])
            if result["source_bytes"] > runner.options["max_source_bytes"]:
                raise CodeIndexBlocked("pilot source byte budget exceeded")
            if result["files"] + result["submodules"] > 500000:
                raise CodeIndexBlocked("pilot source file-count budget exceeded")
        if len(pending) > 8192:
            raise CodeIndexError("oversized unterminated Git tree metadata")
    runner.run(["git", "--git-dir", repo, "ls-tree", "-r", "-l", "-z", oid, "--"],
               consume=consume, output_bytes=MAX_SOURCE_METADATA)
    if pending:
        raise CodeIndexError("incomplete Git tree metadata")
    return result


class ZoektIndexAdapter:
    def identity(self, runner):
        manifest = _read_json(BUILD_MANIFEST, 16384)
        if manifest.get("upstream_commit") != ENGINE_COMMIT:
            raise CodeIndexError("Zoekt build manifest does not match the pinned engine")
        indexer = _hash_file(INDEXER_BINARY, MAX_BINARY_BYTES, runner)
        query = _hash_file(QUERY_BINARY, MAX_BINARY_BYTES, runner)
        identity = {"engine_commit": ENGINE_COMMIT, "indexer_sha256": indexer, "query_bridge_sha256": query}
        if runner.options["symbols"]:
            identity["ctags_sha256"] = _hash_file(CTAGS_BINARY, MAX_BINARY_BYTES, runner)
        try:
            verify_engine_identity(manifest, identity, symbols=runner.options["symbols"])
        except EngineIdentityError as exc:
            message = {"engine": "Zoekt build manifest does not match the pinned engine",
                       "binary": "Zoekt binary identity does not match the pinned build manifest",
                       "symbols": "ctags binary identity does not match the pinned build manifest"}[exc.reason]
            raise CodeIndexError(message) from exc
        raw = runner.run([QUERY_BINARY, "--version"])
        try:
            version = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CodeIndexError("Zoekt query bridge version is invalid") from None
        if not isinstance(version, dict) or version.get("engine_commit") != ENGINE_COMMIT:
            raise CodeIndexError("Zoekt bridge does not embed the pinned engine")
        return identity

    def build(self, repo, oid, directory, runner):
        runner.run([INDEXER_BINARY, "-index", directory, "-branches", oid,
            "-parallelism", "1", "-submodules=false", "-incremental=false", "-delta=false",
            "-allow_missing_branches=false",
            "-require_ctags=" + str(runner.options["symbols"]).lower(),
            "-disable_ctags=" + str(not runner.options["symbols"]).lower(),
            "-max_trigram_count", str(MAX_TRIGRAM_COUNT),
            "-file_limit", str(runner.code["limits"]["max_file_bytes"]),
            "-shard_limit", str(64 * 1024**2), repo])

    def verify(self, directory, oid, runner):
        raw = runner.run([QUERY_BINARY], input_bytes=json.dumps({"op": "verify",
            "index_dir": str(directory), "commit": oid}).encode())
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise CodeIndexError("Zoekt verification bridge returned invalid JSON") from None


def _generation_path(root, relative):
    if not isinstance(relative, str) or not re.fullmatch(r"indexes/generations/[a-f0-9]{32}", relative):
        raise CodeIndexError("invalid immutable index generation path")
    directory = root / relative
    if any(path.is_symlink() for path in (root / "indexes", directory.parent, directory)):
        raise CodeIndexError("index generation paths must not traverse symlinks")
    if directory.resolve().parent != (root / "indexes" / "generations").resolve():
        raise CodeIndexError("index generation leaves its configured directory")
    return directory


def _shards(directory, runner):
    if directory.is_symlink():
        raise CodeIndexError("index generation must not be a symlink")
    children = list(directory.iterdir())
    if not 1 <= len(children) <= MAX_SHARDS:
        raise CodeIndexError("index build produced no shards or too many artifacts")
    result = []
    for child in sorted(children):
        if child.is_symlink() or not child.is_file() or not child.name.endswith(".zoekt"):
            raise CodeIndexError("index build produced an unexpected or unsafe artifact")
        result.append({"name": child.name, "bytes": child.stat().st_size,
            "sha256": _hash_file(child, runner.options["max_index_bytes"], runner)})
    return result


def _sync_files(directory):
    for child in directory.iterdir():
        with child.open("rb") as source:
            os.fsync(source.fileno())
    for path in (directory, directory.parent, directory.parent.parent):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _validate_index_state(state):
    if (state.get("schema_version") != 1 or type(state.get("generation")) is not int
            or state["generation"] < 0 or state.get("published") is not None and not isinstance(state["published"], dict)):
        raise CodeIndexError("invalid index checkpoint; preserved for operator recovery")
    attempt = state.get("attempt_fingerprint")
    if attempt is not None and (not isinstance(attempt, str) or not SHA256.fullmatch(attempt)):
        raise CodeIndexError("invalid index attempt checkpoint; preserved for operator recovery")
    block = state.get("budget_block")
    if block is not None and (not isinstance(block, dict) or not isinstance(block.get("fingerprint"), str)
                              or not SHA256.fullmatch(block["fingerprint"])):
        raise CodeIndexError("invalid index budget checkpoint; preserved for operator recovery")
    return state


def index_code_once(config, *, adapter=None):
    """Build only the explicitly configured pilot; adapters cannot bypass cgroups."""
    code = normalize_code_config(config.get("code"), data_dir=config["data_dir"])
    if not code["index"]["enabled"]:
        return {"schema_version": 1, "generation": 0, "status": "disabled", "published": None}
    root = Path(code["root_dir"])
    root.mkdir(parents=True, exist_ok=True)
    data = Path(config["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    state_path = root / "index-state.json"
    state = _read_json(state_path) if state_path.exists() else {
        "schema_version": 1, "generation": 0, "published": None}
    _validate_index_state(state)
    adapter = adapter or ZoektIndexAdapter()
    with (data / "index.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(state, status="waiting_for_io")
        # Re-read after admission so a just-completed writer cannot be lost.
        if state_path.exists():
            state = _validate_index_state(_read_json(state_path))
        budget_fingerprint = _budget_fingerprint(code)
        if state.get("status") == "building" and state.get("attempt_fingerprint"):
            state.update(status="blocked", last_error="Previous index supervisor was interrupted; review the failed build before changing policy")
            state["budget_block"] = {"fingerprint": state["attempt_fingerprint"], "reason": state["last_error"]}
            _save_state(state_path, state)
        if (state.get("budget_block") or {}).get("fingerprint") == budget_fingerprint:
            return dict(state, status="blocked", last_result="limits_unchanged")
        state.pop("budget_block", None)
        runner = None
        started = time.monotonic()
        try:
            cap = _enforced_memory_limit(code["index"]["memory_bytes"])
            runner = BuildRunner(code, root, lock.fileno(), cap)
            tree, ref, oid, code_generation, repo = _snapshot(code)
            identity = adapter.identity(runner)
            if (not isinstance(identity, dict) or identity.get("engine_commit") != ENGINE_COMMIT
                    or any(not isinstance(identity.get(key), str) or not SHA256.fullmatch(identity[key])
                           for key in ("indexer_sha256", "query_bridge_sha256"))):
                raise CodeIndexError("invalid pinned engine identity")
            if code["index"]["symbols"] and (not isinstance(identity.get("ctags_sha256"), str)
                    or not SHA256.fullmatch(identity["ctags_sha256"])):
                raise CodeIndexError("symbol indexing requires verified ctags identity")
            signature = code_catalog_fingerprint(tree)
            policy = {"admission_version": 1, "max_file_bytes": code["limits"]["max_file_bytes"],
                "max_trigram_count": MAX_TRIGRAM_COUNT, "symbols": code["index"]["symbols"],
                "ctags_sha256": identity.get("ctags_sha256")}
            policy_fingerprint = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
            prior = state.get("published")
            if prior and all(prior.get(key) == value for key, value in dict(tree=tree["id"], ref=ref,
                    commit_oid=oid, catalog_fingerprint=signature, policy_fingerprint=policy_fingerprint, **identity).items()):
                expected = _generation_path(root, prior.get("directory"))
                if expected.is_dir():
                    runner.target = expected
                    if _shards(expected, runner) == prior.get("shards"):
                        state.update(status="ready", last_result="unchanged", last_error=None, checked_at=_now())
                        _save_state(state_path, state)
                        return state
            source = _source_budget(repo, oid, runner)
            directory = _generation_path(root, "indexes/generations/" + uuid.uuid4().hex)
            directory.mkdir(parents=True)
            runner.target = directory
            state.update(status="building", started_at=_now(), pending_directory=str(directory.relative_to(root)),
                         attempt_fingerprint=budget_fingerprint, last_error=None)
            _save_state(state_path, state)
            adapter.build(repo, oid, directory, runner)
            runner.guard()
            shards = _shards(directory, runner)
            verified = adapter.verify(directory, oid, runner)
            if (not isinstance(verified, dict) or verified.get("engine_commit") != ENGINE_COMMIT
                    or verified.get("commit_oid") != oid or not isinstance(verified.get("repository"), str)
                    or not verified["repository"] or len(verified["repository"]) > 4096
                    or type(verified.get("has_symbols")) is not bool):
                raise CodeIndexError("built index does not verify against the exact pilot commit")
            if code["index"]["symbols"] and not verified["has_symbols"]:
                raise CodeIndexError("symbol pilot did not produce verified symbol metadata")
            # Shared heavy lock also excludes mirror publication, but verify its
            # authority again before promoting the independently generated index.
            _, _, current_oid, current_generation, _ = _snapshot(code)
            if current_oid != oid or current_generation != code_generation:
                raise CodeIndexError("published code generation changed during indexing")
            published = {"tree": tree["id"], "ref": ref, "commit_oid": oid,
                "code_generation": code_generation, "catalog_fingerprint": signature, **identity,
                "repository": verified["repository"], "directory": str(directory.relative_to(root)),
                "symbols": code["index"]["symbols"], "has_symbols": bool(verified.get("has_symbols")),
                "unsafe_paths": source["unsafe_paths"], "policy": policy, "policy_fingerprint": policy_fingerprint,
                "shards": shards, "published_at": _now(), "source": source,
                "coverage": {"max_file_bytes": code["limits"]["max_file_bytes"],
                    "max_trigram_count": MAX_TRIGRAM_COUNT, "partial": True, "exclusions": {"oversized_files": source["oversized_files"],
                        "submodules": source["submodules"], "symlinks": source["symlinks"],
                        "binary_generated_or_non_utf8": "engine policy; not counted"},
                    "note": "Selected-ref index only; engine exclusions are not proof of complete source coverage."}}
            candidate = dict(state, generation=state["generation"] + 1, published=published,
                status="ready", last_result="published", last_error=None, checked_at=_now(),
                enforced_memory=cap, measurements={"seconds": round(time.monotonic() - started, 3),
                    "peak_index_bytes_sampled": runner.peak_bytes, "cgroup": _cgroup_metrics(cap)})
            candidate.pop("pending_directory", None)
            candidate.pop("attempt_fingerprint", None)
            runner.guard()
            _sync_files(directory)
            _save_state(state_path, candidate)
            return candidate
        except Exception as exc:
            budget_blocked = isinstance(exc, CodeIndexBlocked) or storage_error(exc)
            if budget_blocked:
                state["budget_block"] = {"fingerprint": budget_fingerprint, "reason": str(exc)[:1000]}
            state.update(status="blocked" if budget_blocked or not state.get("published") else "degraded", last_result="failed",
                last_error=str(exc)[:2000], checked_at=_now(), measurements={
                    "seconds": round(time.monotonic() - started, 3),
                    "peak_index_bytes_sampled": runner.peak_bytes if runner else 0,
                    "cgroup": _cgroup_metrics(runner.cap) if runner else None})
            _save_state(state_path, state)
            return state
