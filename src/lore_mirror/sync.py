"""Bounded public-inbox replication, indexing, and durable hourly checkpoints.

The read API never imports a network client from this module. Native public-inbox
owns Git mirror semantics; this coordinator owns scope, budgets, and freshness.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

GIB = 1024 ** 3
ALLOWED_HOSTS = {"erol.kernel.org"}
EPOCH_RE = re.compile(r"^/([a-zA-Z0-9][a-zA-Z0-9_.+-]*)/git/(\d+)\.git$")
PACK_RE = re.compile(rb"^P (pack-[a-f0-9]{40,64}\.pack)$", re.M)
MAX_MANIFEST_COMPRESSED = 4 * 1024 * 1024
MAX_MANIFEST_PLAIN = 32 * 1024 * 1024
LOG_MAX_BYTES = 10 * 1024 * 1024


class SyncError(RuntimeError):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w") as out:
        json.dump(value, out, indent=2, sort_keys=True)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def tree_bytes(path):
    apparent = allocated = 0
    for base, _, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(base, name), follow_symlinks=False)
                apparent += st.st_size
                allocated += st.st_blocks * 512
            except FileNotFoundError:
                pass
    return {"apparent": apparent, "allocated": allocated}


def storage_available(root, minimum=.30):
    usage = shutil.disk_usage(root)
    return max(0, usage.free - int(usage.total * minimum))


def validate_manifest(raw):
    """Reject unsafe paths, bogus values and decompression bombs before cloning."""
    if len(raw) > MAX_MANIFEST_COMPRESSED:
        raise SyncError("compressed manifest exceeds size limit")
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        plain = stream.read(MAX_MANIFEST_PLAIN + 1)
    if len(plain) > MAX_MANIFEST_PLAIN:
        raise SyncError("expanded manifest exceeds size limit")
    result = json.loads(plain)
    if not isinstance(result, dict) or not result or len(result) > 10000:
        raise SyncError("invalid manifest inventory")
    for name, row in result.items():
        if not EPOCH_RE.fullmatch(name) or not isinstance(row, dict):
            raise SyncError(f"unsupported or unsafe manifest entry: {name!r}")
        fp = row.get("fingerprint")
        if not isinstance(fp, str) or not re.fullmatch(r"[a-f0-9]{40,64}", fp):
            raise SyncError(f"invalid fingerprint: {name}")
    return result


def _request(url, user_agent, *, method="GET", headers=None, limit=MAX_MANIFEST_COMPRESSED):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username:
        raise SyncError("upstream must be an approved HTTPS archive host")
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            target = urlparse(newurl)
            if target.scheme != "https" or target.hostname not in ALLOWED_HOSTS:
                raise SyncError("redirect leaves approved archive host")
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)
    request = urllib.request.Request(url, method=method,
        headers={"User-Agent": user_agent, **(headers or {})})
    with urllib.request.build_opener(SafeRedirect()).open(request, timeout=60) as response:
        data = b"" if method == "HEAD" else response.read(limit + 1)
        if len(data) > limit:
            raise SyncError("upstream response exceeds size limit")
        return data, dict(response.headers)


def fetch_manifest(config, state):
    headers = {}
    old = state.get("manifest", {})
    if old.get("etag"):
        headers["If-None-Match"] = old["etag"]
    if old.get("last_modified"):
        headers["If-Modified-Since"] = old["last_modified"]
    path = Path(config["data_dir"]) / "state" / "upstream-manifest.js.gz"
    try:
        data, received = _request(config["manifest_url"], config["user_agent"], headers=headers)
    except urllib.error.HTTPError as exc:
        if exc.code != 304 or not path.exists():
            raise
        data, received = path.read_bytes(), old
    manifest = validate_manifest(data)
    prior_count = old.get("entries", 0)
    if prior_count and len(manifest) < prior_count * .90:
        raise SyncError("manifest shrank by more than 10%; manual scope review required")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)
    state["manifest"] = {"checked_at": utcnow(), "sha256": hashlib.sha256(data).hexdigest(),
        "entries": len(manifest), "etag": received.get("ETag", old.get("etag")),
        "last_modified": received.get("Last-Modified", old.get("last_modified"))}
    return manifest


def pack_budget(config, epochs):
    """Measure own packs and advertised same-host object pools, counting pools once.

    These are advertised pack sizes, not a promise about negotiated Git transfer
    or final client disk use. A running storage guard remains mandatory.
    """
    rows, pools = [], {}
    upstream = config["upstream"].rstrip("/")
    def inventory(objects_url):
        data, _ = _request(objects_url + "/info/packs", config["user_agent"], limit=1024 * 1024)
        packs, complete = [], True
        for raw_name in PACK_RE.findall(data):
            name = raw_name.decode("ascii")
            _, headers = _request(objects_url + "/pack/" + name, config["user_agent"], method="HEAD")
            size = int(headers["Content-Length"]) if headers.get("Content-Length") else None
            packs.append({"name": name, "bytes": size})
            complete = complete and size is not None
        return packs, bool(packs) and complete
    for name in epochs:
        row = {"epoch": name, "pack_bytes": 0, "packs": [], "pools": [], "complete": False}
        try:
            row["packs"], row["complete"] = inventory(upstream + name + "/objects")
            row["pack_bytes"] = sum(p["bytes"] or 0 for p in row["packs"])
            if not row["packs"]:
                data, _ = _request(upstream + name + "/objects/info/alternates", config["user_agent"], limit=65536)
                for alternate in data.decode("ascii", "strict").splitlines():
                    match = re.fullmatch(r"/var/lib/git/erol\.kernel\.org/objstore/([a-f0-9-]{36})\.git/objects", alternate.strip())
                    if not match:
                        row.setdefault("unresolved_alternates", []).append(alternate[:500])
                        continue
                    key = match.group(1)
                    if key not in pools:
                        url = upstream + "/objstore/" + key + ".git/objects"
                        packs, complete = inventory(url)
                        pools[key] = {"objects_url": url, "advertised_alternate": alternate,
                            "packs": packs, "pack_bytes": sum(p["bytes"] or 0 for p in packs), "complete": complete}
                    row["pools"].append(key)
                row["complete"] = bool(row["pools"]) and not row.get("unresolved_alternates") and all(pools[key]["complete"] for key in row["pools"])
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 429, 503}:
                raise
            row["error"] = str(exc)
        except Exception as exc:
            row["error"] = str(exc)
        rows.append(row)
    return {"checked_at": utcnow(), "complete": all(row["complete"] for row in rows),
        "known_pack_bytes": sum(row["pack_bytes"] for row in rows) + sum(pool["pack_bytes"] for pool in pools.values()),
        "unique_pools": pools, "epochs": rows,
        "note": "Advertised pools counted once; client overhead and loose/new objects are additional."}


def admission_multiplier(config, state, item):
    """Use measured large-list costs; do not extrapolate from tiny pilot lists."""
    configured = config.get("initial_storage_multiplier", 4)
    eligible = []
    by_id = {row["id"]: row for row in config["lists"]}
    for ident, row in state["lists"].items():
        spec = by_id.get(ident, {})
        if spec.get("size_class", spec.get("class")) != "large":
            continue
        metric = row.get("initial_measurements", row.get("measurements"))
        if not metric or metric["git_bytes"]["allocated"] < 64 * 1024**2:
            continue
        git = metric["git_bytes"]["allocated"]
        ratio = (git + metric["basic_index_bytes"]["allocated"] + max(0, metric["extindex_increment_bytes"])) / git
        eligible.append((spec.get("class") == item.get("class"), ratio))
    matching = [ratio for matched, ratio in eligible if matched]
    values = matching or [ratio for _, ratio in eligible]
    return max([configured] + values)


def _aggregate_rss(pid):
    """Sample aggregate live process-tree RSS on Linux (not allocator peak)."""
    parents, sizes = {}, {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            parents[int(entry.name)] = int(text[1])
            sizes[int(entry.name)] = int(text[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    selected = {pid}
    while True:
        expanded = selected | {p for p, parent in parents.items() if parent in selected}
        if expanded == selected:
            return sum(sizes.get(p, 0) for p in selected)
        selected = expanded


def run_command(argv, config, log_path, *, env=None):
    """No shell; kill the whole job if headroom, deadline or RSS budget is crossed."""
    started = time.monotonic()
    peak = 0
    timeout = config.get("job_timeout_seconds", 12 * 3600)
    memory_limit = config.get("job_memory_limit_bytes", 6 * GIB)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Rotate between jobs so the child keeps a stable output descriptor.
    if log_path.exists() and log_path.stat().st_size >= LOG_MAX_BYTES:
        for number in (2, 1):
            previous = Path(f"{log_path}.{number}")
            if previous.exists():
                previous.replace(f"{log_path}.{number + 1}")
        log_path.replace(f"{log_path}.1")
    error = None
    with log_path.open("ab") as log:
        log.write((json.dumps({"at": utcnow(), "argv": argv}) + "\n").encode())
        log.flush()
        child = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            env=env, start_new_session=True)
        try:
            while child.poll() is None:
                peak = max(peak, _aggregate_rss(child.pid))
                if storage_available(config["data_dir"], config["min_free_fraction"]) < 256 * 1024 ** 2:
                    error = "storage guard: preserving configured free-space headroom"
                elif time.monotonic() - started > timeout:
                    error = "job deadline exceeded"
                elif peak > memory_limit:
                    error = "job aggregate RSS limit exceeded"
                if error:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    break
                time.sleep(.25)
            rc = child.wait()
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            raise
    result = {"seconds": round(time.monotonic() - started, 3),
        "peak_aggregate_rss_bytes_sampled": peak, "returncode": rc, "log": str(log_path)}
    if error or rc:
        raise SyncError((error or f"command exited {rc}") + f"; see {log_path}")
    return result


def _tool(config, name):
    prefix = config.get("public_inbox_bin_dir")
    return str(Path(prefix) / name) if prefix else name


def _refs(path):
    result = {}
    for repo in sorted((Path(path) / "git").glob("*.git")):
        p = subprocess.run(["git", "--git-dir", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
            check=True, stdout=subprocess.PIPE, text=True, timeout=60)
        result[repo.name] = p.stdout.splitlines()
    return result


def message_count(path):
    db = Path(path) / "msgmap.sqlite3"
    if not db.exists():
        return None
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT COUNT(*) FROM msgmap").fetchone()[0]


def date_bounds(path):
    db = Path(path) / "xap15" / "over.sqlite3"
    if not db.exists():
        return None
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT MIN(ds),MAX(ds),MIN(ts),MAX(ts) FROM over WHERE num>0").fetchone()
    return {key: dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat() if value is not None else None
        for key, value in zip(("sender_date_min", "sender_date_max", "received_date_min", "received_date_max"), row)}


def prepare_config(path):
    config = json.loads(Path(path).read_text())
    config.setdefault("upstream", "https://erol.kernel.org")
    config.setdefault("manifest_url", config["upstream"].rstrip("/") + "/manifest.js.gz")
    config.setdefault("interval_seconds", 3600)
    config.setdefault("max_workers", 2)
    config.setdefault("min_free_fraction", .30)
    config.setdefault("user_agent", "lore-mirror/0.1 (bounded archive mirror)")
    ids = [item["id"] for item in config.get("lists", [])]
    if not 1 <= len(ids) <= 1000 or len(ids) != len(set(ids)):
        raise SyncError("explicitly select 1 to 1000 unique inboxes")
    from urllib.parse import urlparse
    upstream = urlparse(config["upstream"])
    if upstream.scheme != "https" or upstream.hostname not in ALLOWED_HOSTS or upstream.username or upstream.query or upstream.fragment or upstream.path not in ("", "/"):
        raise SyncError("native replication requires an approved HTTPS archive root")
    config.setdefault("extindex", str(Path(config["data_dir"]) / "extindex"))
    config.setdefault("public_inbox_config", str(Path(config["data_dir"]) / "public-inbox-config"))
    if not 1 <= config["max_workers"] <= 2:
        raise SyncError("archive concurrency must be between one and two")
    if config["min_free_fraction"] < .30 or config["min_free_fraction"] >= 1:
        raise SyncError("pilot requires at least 30% free-space headroom")
    for item in config["lists"]:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]*", item["id"]):
            raise SyncError("invalid list id")
        item.setdefault("inbox_path", str(Path(config["data_dir"]) / "inboxes" / item["id"]))
        if Path(item["inbox_path"]).resolve() != (Path(config["data_dir"]) / "inboxes" / item["id"]).resolve():
            raise SyncError("native manifest mirror requires data_dir/inboxes/LIST paths")
        item.setdefault("newsgroup", "org.kernel.lore." + item["id"])
    return config


def _sync_once_locked(config):
    root = Path(config["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(exist_ok=True)
    state_path = root / "sync-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"schema_version": 1, "lists": {}}
    state["cycle_started_at"] = utcnow()
    state["status"] = "syncing"
    state["stage"] = "syncing"
    state["updating"] = True
    state.setdefault("generation", 0)
    atomic_json(state_path, state)
    manifest = fetch_manifest(config, state)
    env = dict(os.environ)
    for key, value in config.get("sync_env", config.get("query_env", {})).items():
        if key not in {"PATH", "PERL5LIB", "LD_LIBRARY_PATH", "PERL_INLINE_DIRECTORY", "PERL5OPT"}:
            raise SyncError(f"unsupported sync environment key: {key}")
        env[key] = str(value)
    # Keep managed proxy variables, but never reuse ambient Git identity,
    # authentication helpers, config overrides, prompting, or ~/.netrc.
    for key in list(env):
        if key.startswith("GIT_CONFIG_") or key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_SSL_NO_VERIFY"}:
            env.pop(key, None)
    home = root / "state" / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env.update({"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"})
    env["PI_CONFIG"] = config["public_inbox_config"]
    env["GIT_CONFIG_COUNT"] = "4"
    env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"] = "http.userAgent", config["user_agent"]
    env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"] = "gc.auto", "0"
    env["GIT_CONFIG_KEY_2"], env["GIT_CONFIG_VALUE_2"] = "credential.helper", ""
    env["GIT_CONFIG_KEY_3"], env["GIT_CONFIG_VALUE_3"] = "core.askPass", "/bin/false"
    curl_home = root / "state" / "curl"
    curl_home.mkdir(exist_ok=True)
    (curl_home / ".curlrc").write_text("user-agent = " + json.dumps(config["user_agent"]) + "\n")
    env["CURL_HOME"] = str(curl_home)
    errors = []
    # Small lists first provide an end-to-end measurement before admitting large lists.
    for item in sorted(config["lists"], key=lambda x: x.get("size_class", x.get("class")) != "small"):
        ident = item["id"]
        row = state["lists"].setdefault(ident, {})
        epochs = sorted(k for k in manifest if k.split("/")[1] == ident)
        if not epochs:
            row.update(status="blocked", last_error="namespace absent from validated manifest")
            errors.append(ident)
            continue
        fingerprints = {k: manifest[k].get("fingerprint") for k in epochs}
        row["upstream_epochs"] = fingerprints
        # A long cold index is not an hourly observation. Begin from the
        # latest completed index or completed unchanged-fingerprint check.
        previous_points = [dt.datetime.fromisoformat(row[key]).timestamp() for key in ("checked_at", "indexed_at") if row.get(key)]
        elapsed_observation = time.time() - max(previous_points) if previous_points else None
        row["checked_at"] = utcnow()
        # Retry interrupted work even when upstream did not change.
        if row.get("status") == "ready" and row.get("indexed_fingerprints") == fingerprints:
            row["hourly_delta"] = {"checked_at": utcnow(), "changed": False, "git_bytes": 0,
                "index_bytes": 0, "messages": 0, "configured_interval_seconds": config["interval_seconds"],
                "elapsed_observation_seconds": elapsed_observation,
                "is_hourly_observation": elapsed_observation is not None and elapsed_observation >= 3590,
                "note": "manifest unchanged; no Git or index work"}
            atomic_json(state_path, state)
            continue
        try:
            new_epochs = [epoch for epoch in epochs if epoch not in row.get("indexed_fingerprints", {})] if row.get("indexed_at") else []
            if not row.get("budget") or not row.get("fetched_at"):
                row["budget"] = pack_budget(config, epochs)
            if new_epochs:
                row["new_epoch_budget"] = pack_budget(config, new_epochs)
            budget = row["budget"]
            multiplier = admission_multiplier(config, state, item)
            row["admission"] = {"storage_multiplier": multiplier,
                "basis": "Configured minimum or measured large-list cost; not a guaranteed capacity prediction",
                "pack_sizes_complete": budget["complete"]}
            # Unknown sizes do not require new user authorization. The selected
            # clone remains bounded by its deadline, RSS limit and storage guard.
            existing = tree_bytes(item["inbox_path"])["allocated"]
            if new_epochs:
                present = sum(tree_bytes(Path(item["inbox_path"]) / "git" / epoch.rsplit("/", 1)[1])["allocated"] for epoch in new_epochs)
                reserve = max(256 * 1024**2, int(row["new_epoch_budget"]["known_pack_bytes"] * multiplier) - present)
            elif row.get("indexed_at"):
                # Existing shared-index space is already allocated. Do not
                # reserve the entire cold index again for a tiny hourly fetch.
                reserve = max(256 * 1024**2, int(config.get("incremental_reserve_bytes", 256 * 1024**2)))
            else:
                reserve = max(256 * 1024**2, int(budget["known_pack_bytes"] * multiplier) - existing)
            if storage_available(root, config["min_free_fraction"]) < reserve:
                raise SyncError(f"storage preflight requires {reserve} additional bytes while preserving 30% headroom")
            previous_git = tree_bytes(Path(item["inbox_path"]) / "git")["allocated"]
            previous_index = tree_bytes(config["extindex"])["allocated"]
            previous_count = message_count(item["inbox_path"]) or 0
            row.update(status="fetching", last_attempt_at=utcnow(), last_error=None)
            atomic_json(state_path, state)
            mirror_manifest = root / "state" / f"mirror-{ident}.js.gz"
            row["fetch"] = run_command([_tool(config, "public-inbox-clone"), "-j" + str(config["max_workers"]),
                "--inbox-config=never", "--include=/" + ident, "--manifest=" + str(mirror_manifest),
                config["upstream"], str(root / "inboxes")], config, root / "logs" / f"fetch-{ident}.log", env=env)
            actual_epochs = sorted("/" + ident + "/git/" + p.name for p in (Path(item["inbox_path"]) / "git").glob("*.git"))
            if actual_epochs != epochs:
                raise SyncError("native clone did not produce every selected epoch")
            row.update(status="indexing", fetched_at=utcnow(), fetched_fingerprints=fingerprints,
                fetched_refs=_refs(item["inbox_path"]))
            atomic_json(state_path, state)
            actual_git = tree_bytes(Path(item["inbox_path"]) / "git")["allocated"]
            input_bytes = max(0, actual_git - previous_git) if row.get("indexed_at") else actual_git
            needed_for_index = max(256 * 1024**2, int(input_bytes * max(1, multiplier - 1)))
            if storage_available(root, config["min_free_fraction"]) < needed_for_index:
                raise SyncError(f"index storage preflight requires {needed_for_index} additional bytes while preserving 30% headroom")
            run_command([_tool(config, "public-inbox-init"), "-V2", "-Lbasic", "-j1", "--ng=" + item["newsgroup"],
                ident, item["inbox_path"], "https://lore.kernel.org/" + item.get("canonical_id", ident) + "/",
                item.get("address", ident + "@lists.linux.dev")], config, root / "logs" / f"init-{ident}.log", env=env)
            row["basic_index"] = run_command([_tool(config, "public-inbox-index"), "-j1", "--no-multi-pack-index",
                item["inbox_path"]], config, root / "logs" / f"basic-{ident}.log", env=env)
            row["index_dirty"] = True
            state["unsafe_index"] = True
            atomic_json(state_path, state)
            row["extindex"] = run_command([_tool(config, "public-inbox-extindex"), "-j1", "--batch-size=16m",
                "--no-multi-pack-index", config["extindex"], item["inbox_path"]], config, root / "logs" / f"extindex-{ident}.log", env=env)
            git = tree_bytes(Path(item["inbox_path"]) / "git")
            full = tree_bytes(item["inbox_path"])
            count = message_count(item["inbox_path"])
            ext = tree_bytes(config["extindex"])
            if row.get("measurements") and row.get("indexed_at"):
                row.setdefault("initial_measurements", row["measurements"])
                row.setdefault("initial_indexed_at", row["indexed_at"])
            row["measurements"] = {"git_bytes": git, "basic_index_bytes": {
                key: full[key] - git[key] for key in git}, "shared_extindex_total_bytes": ext,
                "messages": count, "date_bounds": date_bounds(item["inbox_path"]), "index_seconds": row["basic_index"]["seconds"] + row["extindex"]["seconds"],
                "extindex_increment_bytes": ext["allocated"] - previous_index,
                "peak_aggregate_rss_bytes_sampled": max(row[step]["peak_aggregate_rss_bytes_sampled"] for step in ("fetch", "basic_index", "extindex"))}
            row.setdefault("initial_measurements", row["measurements"])
            row.setdefault("initial_indexed_at", utcnow())
            row["hourly_delta"] = {"git_bytes": git["allocated"] - previous_git,
                "index_bytes": ext["allocated"] - previous_index, "messages": count - previous_count,
                "checked_at": utcnow(), "bootstrap": not bool(row.get("indexed_at")),
                "elapsed_observation_seconds": elapsed_observation,
                "is_hourly_observation": bool(row.get("indexed_at")) and elapsed_observation is not None and elapsed_observation >= 3590,
                "note": ("bootstrap deltas are not hourly growth estimates" if not row.get("indexed_at") else "Local incremental change; catch-up and backfills may affect this observation.")}
            row.update(status="ready", index_dirty=False, indexed_at=utcnow(), indexed_fingerprints=fingerprints, retry_count=0)
        except Exception as exc:
            row.update(status="blocked" if "storage" in str(exc) or "capacity" in str(exc) else "error",
                last_error=str(exc), retry_count=row.get("retry_count", 0) + 1,
                next_retry_at=dt.datetime.fromtimestamp(time.time() + config["interval_seconds"], dt.timezone.utc).isoformat())
            errors.append(ident)
        atomic_json(state_path, state)
    state["unsafe_index"] = any(r.get("index_dirty") for r in state["lists"].values())
    state["stage"] = "recovery_required" if state["unsafe_index"] else "idle"
    state.update(status="partial" if errors else "ready", updating=any(r.get("index_dirty") for r in state["lists"].values()), cycle_finished_at=utcnow(),
        extindex_bytes=tree_bytes(config["extindex"]), disk_free_bytes=shutil.disk_usage(root).free,
        pending_lists=errors, next_run_at=dt.datetime.fromtimestamp(time.time() + config["interval_seconds"], dt.timezone.utc).isoformat())
    if any(r.get("indexed_at", "") >= state["cycle_started_at"] for r in state["lists"].values()):
        state["generation"] = state.get("generation", 0) + 1
        state["last_indexed_at"] = state["cycle_finished_at"]
    if state["status"] == "ready" and not state["unsafe_index"]:
        state["last_success"] = state["cycle_finished_at"]
        state.pop("last_error", None)
    atomic_json(state_path, state)
    history_path = root / "state" / "observations.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    history.append({"observed_at": state["cycle_finished_at"], "generation": state.get("generation", 0),
        "status": state["status"], "unsafe_index": state.get("unsafe_index", False),
        "disk_free_bytes": state["disk_free_bytes"], "extindex_bytes": state["extindex_bytes"],
        "lists": {key: {"status": row.get("status"), "indexed_at": row.get("indexed_at"),
            "measurements": row.get("measurements"), "delta": row.get("hourly_delta")}
            for key, row in state["lists"].items()}})
    # Fourteen days of hourly observations; bounded independently of log rotation.
    atomic_json(history_path, history[-336:])
    return state


def sync_once(config):
    root = Path(config["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / "index.lock").open("a") as index_lock:
        fcntl.flock(index_lock, fcntl.LOCK_EX)
        return _sync_once_locked(config)


def run_sync(config_path, once=True):
    # A stopped coordinator must not leave an orphan writer that outlives flock.
    import threading
    if threading.current_thread() is threading.main_thread():
        def stop_requested(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, stop_requested)
    config = prepare_config(config_path)
    root = Path(config["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("another sync coordinator already owns this data directory") from exc
        daemon = None
        if not once:
            daemon = {"status": "starting", "pid": os.getpid(), "pid_scope": "current process namespace",
                "started_at": utcnow(), "interval_seconds": config["interval_seconds"],
                "scope": [item["id"] for item in config["lists"]], "cycles_completed": 0,
                "persistence": "Current workspace process; survival after workspace termination is not guaranteed."}
            atomic_json(root / "daemon-state.json", daemon)
        try:
            while True:
                if daemon:
                    daemon.update(status="syncing", cycle_started_at=utcnow())
                    atomic_json(root / "daemon-state.json", daemon)
                try:
                    state = sync_once(config)
                except Exception as exc:
                    state_path = root / "sync-state.json"
                    state = json.loads(state_path.read_text()) if state_path.exists() else {"lists": {}}
                    state["unsafe_index"] = any(r.get("index_dirty") for r in state.get("lists", {}).values())
                    state.update(status="error", updating=any(r.get("index_dirty") for r in state.get("lists", {}).values()), last_error=str(exc),
                        cycle_finished_at=utcnow(), pending_lists=[r["id"] for r in config["lists"]])
                    atomic_json(state_path, state)
                    if once:
                        raise
                print(json.dumps({"status": state["status"], "pending_lists": state["pending_lists"],
                    "finished_at": state["cycle_finished_at"]}), flush=True)
                if once:
                    return state
                daemon.update(status="sleeping", last_cycle_status=state["status"],
                    cycles_completed=daemon["cycles_completed"] + 1,
                    last_cycle_finished_at=state["cycle_finished_at"],
                    next_scheduled_at=dt.datetime.fromtimestamp(time.time() + config["interval_seconds"], dt.timezone.utc).isoformat())
                if state["status"] == "ready" and not state.get("unsafe_index"):
                    daemon.setdefault("first_successful_cycle_at", state["cycle_finished_at"])
                    daemon["last_successful_cycle_at"] = state["cycle_finished_at"]
                atomic_json(root / "daemon-state.json", daemon)
                time.sleep(config["interval_seconds"])
        finally:
            if daemon:
                daemon.update(status="stopped", stopped_at=utcnow())
                atomic_json(root / "daemon-state.json", daemon)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--daemon", action="store_true")
    args = parser.parse_args()
    state = run_sync(args.config, once=not args.daemon)
    return 0 if not state or state["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
