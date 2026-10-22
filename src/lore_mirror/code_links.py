"""Bounded local evidence backfill with an independent transactional checkpoint.

Only this explicit coordinator writes links.sqlite3. Readers never fetch, scan,
compute patch IDs, or interpret equivalent patches as application/effectiveness.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .backend import FixtureBackend, PublicInboxBackend
from .code_runtime import bounded_run
from .code_config import code_catalog_fingerprint, normalize_code_config
from .code_common import (OID, CodeStateError, RepositoryOwnershipError, load_code_state,
                          storage_error, validate_repo_ownership)
from .code_sync import _safe_environment
from .errors import MirrorError
from .sync import atomic_json, storage_available

LINKS_SCHEMA_VERSION = 1
APPLICATION_ID = 0x4C4F5245
MAX_FIELD_BYTES = 65536
MAX_READ_BYTES = 512 * 1024
READ_TIMEOUT_SECONDS = 2
WRITER_BUSY_TIMEOUT_SECONDS = READ_TIMEOUT_SECONDS + 1
TRAILER_ALGORITHM = "commit-trailer/lore-final-block-v1"
NO_MATCH = "No match in this scanned local scope; this is not evidence of non-application."
_SCHEMA = """
CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE active_catalog(tree TEXT PRIMARY KEY,catalog_fingerprint TEXT NOT NULL);
CREATE TABLE commits(tree TEXT NOT NULL,oid TEXT NOT NULL,catalog_fingerprint TEXT NOT NULL,
 parents TEXT NOT NULL,committed_at INTEGER NOT NULL,patch_id TEXT,patch_status TEXT NOT NULL,
 algorithm TEXT NOT NULL,git_version TEXT,warnings TEXT NOT NULL,code_generation INTEGER NOT NULL,
 discovered_at TEXT NOT NULL,PRIMARY KEY(tree,oid,catalog_fingerprint));
CREATE INDEX commit_patch ON commits(tree,algorithm,patch_id);
CREATE TABLE citations(tree TEXT NOT NULL,oid TEXT NOT NULL,catalog_fingerprint TEXT NOT NULL,
 message_id TEXT NOT NULL,source TEXT NOT NULL,discovered_at TEXT NOT NULL,
 PRIMARY KEY(tree,oid,catalog_fingerprint,message_id,source));
CREATE INDEX citation_mid ON citations(message_id,tree);
CREATE TABLE mail_variants(message_id TEXT NOT NULL,raw_sha256 TEXT NOT NULL,source_blob TEXT,
 patch_id TEXT,patch_status TEXT NOT NULL,algorithm TEXT NOT NULL,git_version TEXT,warnings TEXT NOT NULL,policy TEXT NOT NULL,
 mail_generation TEXT NOT NULL,discovered_at TEXT NOT NULL,PRIMARY KEY(message_id,raw_sha256));
CREATE INDEX mail_patch ON mail_variants(algorithm,patch_id);
CREATE INDEX mail_blob ON mail_variants(source_blob,message_id);
CREATE INDEX mail_raw ON mail_variants(raw_sha256);
CREATE TABLE jobs(id TEXT PRIMARY KEY,tree TEXT NOT NULL,ref TEXT NOT NULL,snapshot_oid TEXT NOT NULL,
 catalog_fingerprint TEXT NOT NULL,patch_since TEXT NOT NULL,algorithm TEXT NOT NULL,policy TEXT NOT NULL,
 code_generation INTEGER NOT NULL,status TEXT NOT NULL,scanned INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,completed_at TEXT);
CREATE INDEX job_scope ON jobs(tree,ref,snapshot_oid);
CREATE TABLE frontier(job TEXT NOT NULL,oid TEXT NOT NULL,PRIMARY KEY(job,oid));
CREATE TABLE visited(job TEXT NOT NULL,oid TEXT NOT NULL,PRIMARY KEY(job,oid));
CREATE TABLE boundaries(job TEXT NOT NULL,oid TEXT NOT NULL,base_job TEXT NOT NULL,PRIMARY KEY(job,oid));
"""


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _decode(value):
    if not isinstance(value, str) or len(value) > MAX_FIELD_BYTES:
        raise MirrorError("links_state_invalid", "Evidence checkpoint field exceeds its bound", 503)
    try:
        return json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise MirrorError("links_state_invalid", "Invalid evidence checkpoint field", 503) from exc


def _get(db, key, default=None):
    row = db.execute("SELECT value FROM info WHERE key=?", (key,)).fetchone()
    return _decode(row[0]) if row else default


def _put(db, key, value):
    encoded = _json(value)
    if len(encoded) > MAX_FIELD_BYTES:
        raise MirrorError("links_state_limit", "Evidence checkpoint field exceeds its bound", 413)
    db.execute("INSERT OR REPLACE INTO info VALUES(?,?)", (key, encoded))


def _safe_path(root):
    root = Path(root)
    path = root / "links.sqlite3"
    if any(parent.is_symlink() for parent in (root, *root.parents)) or path.is_symlink() or any((root / ("links.sqlite3" + suffix)).is_symlink()
                                                   for suffix in ("-journal", "-wal", "-shm")):
        raise MirrorError("links_state_invalid", "Evidence storage must not traverse symlinks", 503)
    return path


def _validate_db(db):
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or db.execute("PRAGMA user_version").fetchone()[0] != LINKS_SCHEMA_VERSION:
        raise MirrorError("links_state_invalid", "Unsupported independent evidence database format", 503)
    generation = _get(db, "generation")
    if type(generation) is not int or generation < 0:
        raise MirrorError("links_state_invalid", "Invalid evidence generation", 503)
    return generation


class LinksStore:
    """Read-only, bounded evidence lookup; caller supplies snapshot reachability."""
    def __init__(self, root):
        self.root = Path(root)

    @contextlib.contextmanager
    def _read(self):
        path = _safe_path(self.root)
        if not path.is_file():
            raise MirrorError("links_not_ready", "Local evidence backfill has not initialized its database", 503)
        db = None
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)
            db.row_factory = sqlite3.Row
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_FIELD_BYTES)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA cache_size=-2048")
            db.execute("PRAGMA mmap_size=0")
            deadline = time.monotonic() + READ_TIMEOUT_SECONDS
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            # All SQL and generation metadata in one SQLite snapshot. This
            # transaction never acquires the heavy writer-only index.lock.
            db.execute("BEGIN")
            _validate_db(db)
            yield db
        except sqlite3.Error as exc:
            raise MirrorError("links_state_invalid", "Evidence database is invalid, busy, or exceeds its read budget", 503) from exc
        finally:
            if db is not None:
                db.close()

    def generation(self):
        with self._read() as db:
            return str(_get(db, "generation"))

    def status(self):
        blocked = _read_budget_state(self.root)
        if blocked.get("budget_block") and not (self.root / "links.sqlite3").is_file():
            return {"schema_version": LINKS_SCHEMA_VERSION, "generation": blocked.get("generation", 0),
                    "algorithm": None, "status": "blocked", "coverage": blocked.get("coverage", {}),
                    "last_error": blocked.get("last_error"), "qualification": NO_MATCH}
        with self._read() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM jobs GROUP BY status LIMIT 8")}
            return {"schema_version": LINKS_SCHEMA_VERSION, "generation": _get(db, "generation"),
                    "algorithm": _get(db, "algorithm"), "status": "blocked" if blocked.get("budget_block") else _get(db, "status", "partial"),
                    "coverage": {"jobs": counts, "mail": _get(db, "mail_coverage", {})},
                    "last_error": blocked.get("last_error") if blocked.get("budget_block") else _get(db, "last_error"), "qualification": NO_MATCH}

    def coverage(self, tree, ref, snapshot_oid):
        with self._read() as db:
            row = db.execute("SELECT catalog_fingerprint,patch_since,status,scanned,created_at,completed_at,algorithm FROM jobs WHERE tree=? AND ref=? AND snapshot_oid=? ORDER BY created_at DESC LIMIT 1",
                             (tree, ref, snapshot_oid)).fetchone()
            return {"tree": tree, "ref": ref, "snapshot_oid": snapshot_oid,
                    "scan": dict(row) if row else {"status": "not_scanned"},
                    "catalog_fingerprint": row[0] if row else None,
                    "mail": _get(db, "mail_coverage", {}), "qualification": NO_MATCH,
                    "generation": _get(db, "generation"), "complete": bool(row and row[2] == "complete")}

    def related_mail(self, tree, commit, *, limit=100):
        return self._lookup(tree, commit=commit, limit=limit)

    def related_commits(self, message_id, tree, *, variant=None, limit=100):
        return self._lookup(tree, message_id=message_id, variant=variant, limit=limit)

    def _lookup(self, tree, *, commit=None, message_id=None, variant=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MirrorError("invalid_limit", "Evidence lookup limit must be 1..1000")
        conditions, params = ["c.tree=?", "c.catalog_fingerprint=(SELECT catalog_fingerprint FROM active_catalog WHERE tree=c.tree)"], [tree]
        if commit is not None:
            conditions.append("c.oid=?"); params.append(commit)
        if message_id is not None:
            conditions.append("m.message_id=?"); params.append(message_id)
        if variant is not None:
            conditions.append("(m.raw_sha256=? OR m.source_blob=?)"); params.extend((variant, variant))
        where = " AND ".join(conditions)
        columns = "c.tree,c.oid AS commit_oid,c.catalog_fingerprint,m.message_id,m.raw_sha256,m.source_blob,c.algorithm,c.git_version,c.code_generation,m.mail_generation,c.discovered_at,c.warnings AS commit_warnings,m.warnings AS mail_warnings"
        sql = f"""SELECT {columns},'linked' AS evidence_level,'commit_trailer' AS evidence_method,t.source AS evidence_source
 FROM citations t JOIN commits c ON c.tree=t.tree AND c.oid=t.oid AND c.catalog_fingerprint=t.catalog_fingerprint
 JOIN mail_variants m ON m.message_id=t.message_id WHERE {where}
 UNION ALL SELECT {columns},CASE WHEN c.patch_status='patch_equivalent_candidate' AND m.patch_status='patch_equivalent_candidate' THEN 'patch_equivalent' ELSE 'candidate' END,
 'git_stable_patch_id',json_object('patch_id',c.patch_id,'git_version',c.git_version) FROM commits c JOIN mail_variants m ON m.patch_id=c.patch_id AND m.algorithm=c.algorithm AND c.git_version IS NOT NULL AND m.git_version=c.git_version
 WHERE c.patch_id IS NOT NULL AND m.policy=(SELECT json_extract(value,'$') FROM info WHERE key='mail_policy') AND {where} LIMIT ?"""
        with self._read() as db:
            cursor = db.execute(sql, (*params, *params, limit + 1))
            items, used, partial = [], 0, False
            for row in cursor:
                if len(items) >= limit:
                    partial = True; break
                commit_warnings, mail_warnings = _decode(row["commit_warnings"]), _decode(row["mail_warnings"])
                if any(not isinstance(w, list) or len(w) > 256 or any(not isinstance(v, str) or len(v) > 1024 for v in w)
                       for w in (commit_warnings, mail_warnings)):
                    raise MirrorError("links_state_invalid", "Invalid evidence warnings", 503)
                warnings = commit_warnings + mail_warnings
                item = {"tree": row["tree"], "commit": row["commit_oid"], "catalog_fingerprint": row["catalog_fingerprint"],
                    "message_id": row["message_id"], "variant": row["source_blob"] or row["raw_sha256"],
                    "raw_sha256": row["raw_sha256"], "source_blob": row["source_blob"],
                    "evidence_level": row["evidence_level"], "evidence_method": row["evidence_method"],
                    "evidence_version": TRAILER_ALGORITHM if row["evidence_method"] == "commit_trailer" else row["algorithm"], "git_version": row["git_version"], "evidence_source": _decode(row["evidence_source"]),
                    "warnings": warnings, "code_generation": row["code_generation"], "mail_generation": row["mail_generation"],
                    "discovered_at": row["discovered_at"]}
                used += len(_json(item))
                if used > MAX_READ_BYTES:
                    partial = True; break
                items.append(item)
            return {"items": items, "partial": partial,
                    "metadata": {"links_generation": _get(db, "generation"), "algorithm": _get(db, "algorithm"),
                        "catalog_fingerprint": (db.execute("SELECT catalog_fingerprint FROM active_catalog WHERE tree=?", (tree,)).fetchone() or [None])[0],
                        "mail_coverage": _get(db, "mail_coverage", {}), "qualification": NO_MATCH}}


class _Runner:
    def __init__(self, code, lock_fd):
        self.code, self.options, self.lock_fd = code, code["links"], lock_fd
        self.root = Path(code["root_dir"])
        self.deadline = time.monotonic() + self.options["timeout_seconds"]
        self.work_deadline = self.deadline - .25
        self.deferred_reason = None
        self.remaining_bytes = self.options["max_patch_bytes"] * 4
        self.env = _safe_environment("/nonexistent")

    def guard(self, *, finishing=False):
        if time.monotonic() >= (self.deadline if finishing else self.work_deadline):
            raise MirrorError("links_timeout", "Evidence batch deadline reached", 504)
        if storage_available(self.root, self.code["min_free_fraction"]) < 256 * 1024**2:
            raise MirrorError("links_storage_limit", "Evidence batch preserves filesystem free-space headroom", 413)
        size = sum((self.root / ("links.sqlite3" + suffix)).stat().st_size
                   for suffix in ("", "-journal", "-wal") if (self.root / ("links.sqlite3" + suffix)).exists())
        if size > self.options["max_db_bytes"]:
            raise MirrorError("links_storage_limit", "Evidence database plus journal exceeds its byte budget", 413)

    def evidence_timeout(self):
        self.guard()
        return min(30, self.work_deadline - time.monotonic())

    def git(self, repo, args, maximum=None):
        self.guard()
        maximum = min(maximum or self.options["max_patch_bytes"], self.remaining_bytes)
        if maximum <= 0:
            raise MirrorError("links_output_limit", "Evidence batch cumulative output budget reached", 413)
        argv = ["git", "--no-pager", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "core.attributesFile=/dev/null", "-c", "protocol.allow=never", "-c", "credential.helper=",
            "-c", "diff.external=", "-c", "diff.algorithm=myers", "-c", "color.ui=false"]
        if repo is not None:
            argv.append("--git-dir=" + str(repo))
        result = bounded_run([*argv, *args], env=self.env, deadline=self.work_deadline,
            max_bytes=maximum, memory_bytes=self.code["limits"]["query_memory_bytes"],
            pass_fds=(self.lock_fd,), guard=lambda child: self.guard())
        self.remaining_bytes -= len(result.output)
        if result.returncode and storage_error(result.stderr):
            raise MirrorError("links_storage_limit", "Evidence Git subprocess reached its storage limit", 413)
        if result.partial_reason or result.returncode:
            raise MirrorError("query_" + result.partial_reason if result.partial_reason else "links_git_error",
                              "Evidence Git read failed or reached its finite budget", 503)
        self.guard()
        return result.output


def _snapshots(code):
    root = Path(code["root_dir"])
    path = root / "code-state.json"
    try:
        state = load_code_state(path, max_refs=code.get("max_refs", 10000))
    except CodeStateError as exc:
        message = ("Published code checkpoint must not be a symlink" if exc.reason == "symlink" else
                   "Invalid published code checkpoint" if exc.detail == "schema" else
                   "Published code checkpoint is unavailable or invalid")
        raise MirrorError("links_source_invalid", message, 503) from exc
    result = []
    for tree in code["trees"]:
        if not tree["index_refs"]:
            continue
        row = state["trees"].get(tree["id"])
        fingerprint = code_catalog_fingerprint(tree)
        if (not isinstance(row, dict) or not isinstance(row.get("published_refs"), dict)
                or row.get("config_fingerprint") != fingerprint or row.get("upstream_url") != tree["url"]):
            raise MirrorError("links_source_invalid", "Links require current published catalog provenance", 503)
        repo = root / "repositories" / (tree["id"] + ".git")
        try:
            validate_repo_ownership(repo)
        except RepositoryOwnershipError as exc:
            raise MirrorError("links_source_invalid", "Links require coordinator-owned full-history bare mirrors", 503) from exc
        for ref in tree["index_refs"]:
            oid = row["published_refs"].get(ref)
            if not isinstance(oid, str) or not OID.fullmatch(oid):
                raise MirrorError("links_source_invalid", "Selected evidence ref is not published", 503)
            result.append({"tree": tree["id"], "ref": ref, "oid": oid, "fingerprint": fingerprint,
                           "generation": state["generation"], "repo": repo})
    if not result:
        raise MirrorError("links_source_invalid", "Configure at least one exact tree index_refs entry for evidence backfill", 503)
    return result


def _open_writer(root, maximum):
    path = _safe_path(root)
    existed = path.exists()
    # DELETE-journal commits need existing read snapshots to drain. Waiting
    # longer than the bounded reader deadline avoids discarding a whole batch
    # merely because a healthy reader was already active at publication time.
    db = sqlite3.connect(path, timeout=WRITER_BUSY_TIMEOUT_SECONDS)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA cache_size=-4096")
        db.execute("PRAGMA mmap_size=0")
        # Reserve approximately half for rollback journals/metadata updates.
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute("PRAGMA max_page_count=" + str(max(1, maximum // (2 * page_size))))
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_FIELD_BYTES)
        uninitialized = (db.execute("PRAGMA application_id").fetchone()[0] == 0
            and db.execute("PRAGMA user_version").fetchone()[0] == 0
            and not db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone())
        if not existed or uninitialized:
            db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={LINKS_SCHEMA_VERSION}")
            _put(db, "generation", 0)
            db.commit()
        _validate_db(db)
        return db
    except BaseException:
        db.close()
        raise


def _ensure_jobs(db, scopes, options, algorithm, policy):
    current = []
    for scope in scopes:
        db.execute("INSERT OR REPLACE INTO active_catalog VALUES(?,?)", (scope["tree"], scope["fingerprint"]))
        signature = [scope[k] for k in ("tree", "ref", "oid", "fingerprint")] + [options["patch_since"], algorithm, policy]
        job = hashlib.sha256(_json(signature).encode()).hexdigest()
        current.append(job)
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (job,)).fetchone():
            continue
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,0,?,NULL)",
            (job, scope["tree"], scope["ref"], scope["oid"], scope["fingerprint"], options["patch_since"], algorithm, policy,
             scope["generation"], "pending", _now()))
        # A tip that moves before the walk completes would otherwise restart
        # from zero and strand the old job's rows. Every commit visited for the
        # old tip had its unvisited parents queued, so adopting its visited
        # set and frontier plus the new tip still reaches all new ancestors.
        prior = db.execute("SELECT id,scanned FROM jobs WHERE tree=? AND ref=? AND catalog_fingerprint=? AND patch_since=? AND algorithm=? AND policy=? AND status='pending' AND id<>? ORDER BY created_at DESC LIMIT 1",
            (scope["tree"], scope["ref"], scope["fingerprint"], options["patch_since"], algorithm, policy, job)).fetchone()
        if prior:
            db.execute("UPDATE frontier SET job=? WHERE job=?", (job, prior[0]))
            db.execute("UPDATE visited SET job=? WHERE job=?", (job, prior[0]))
            db.execute("UPDATE jobs SET scanned=? WHERE id=?", (prior[1], job))
            db.execute("UPDATE jobs SET status='superseded' WHERE id=?", (prior[0],))
        # A completed exact prior snapshot is a verified traversal boundary, not
        # a date assumption. Rewrites walk until a real boundary or graph root.
        bases = db.execute("SELECT id,snapshot_oid FROM jobs WHERE tree=? AND catalog_fingerprint=? AND patch_since=? AND algorithm=? AND policy=? AND status='complete' ORDER BY completed_at DESC LIMIT 100",
            (scope["tree"], scope["fingerprint"], options["patch_since"], algorithm, policy)).fetchall()
        db.executemany("INSERT OR IGNORE INTO boundaries VALUES(?,?,?)", ((job, row[1], row[0]) for row in bases))
        db.execute("INSERT OR IGNORE INTO frontier VALUES(?,?)", (job, scope["oid"]))
    return current


def _commit_metadata(raw):
    header, separator, message = raw.partition(b"\n\n")
    if not separator:
        raise MirrorError("links_source_invalid", "Malformed commit metadata", 503)
    parents = []
    committed_at = None
    for line in header.splitlines():
        if line.startswith(b"parent "):
            value = line[7:].decode("ascii", "strict")
            if not OID.fullmatch(value) or len(parents) >= 64:
                raise MirrorError("links_source_invalid", "Invalid or excessive commit parents", 503)
            parents.append(value)
        if line.startswith(b"committer "):
            fields = line.rsplit(b" ", 2)
            try:
                committed_at = int(fields[-2])
            except (ValueError, IndexError):
                pass
    if committed_at is None or not -62135596800 <= committed_at <= 253402300799:
        raise MirrorError("links_source_invalid", "Commit lacks a valid committer timestamp", 503)
    return parents, committed_at, message.decode("utf-8", "replace")


def _one_commit(db, job, scope, oid, runner, evidence, horizon):
    if not isinstance(oid, str) or not OID.fullmatch(oid):
        raise MirrorError("links_state_invalid", "Invalid checkpoint commit identity", 503)
    if db.execute("SELECT 1 FROM boundaries WHERE job=? AND oid=?", (job, oid)).fetchone():
        db.execute("DELETE FROM frontier WHERE job=? AND oid=?", (job, oid))
        db.execute("INSERT OR IGNORE INTO visited VALUES(?,?)", (job, oid))
        return
    cached = db.execute("SELECT * FROM commits WHERE tree=? AND oid=? AND catalog_fingerprint=?", (scope["tree"], oid, scope["fingerprint"])).fetchone()
    if cached:
        parents, stamp, message = _decode(cached["parents"]), cached["committed_at"], None
        if (not isinstance(parents, list) or len(parents) > 64
                or any(not isinstance(parent, str) or not OID.fullmatch(parent) for parent in parents)
                or type(stamp) is not int or not -62135596800 <= stamp <= 253402300799
                or cached["patch_status"] not in {"outside_horizon", "candidate", "patch_equivalent_candidate"}
                or not isinstance(cached["algorithm"], str) or len(cached["algorithm"]) > 256
                or cached["patch_id"] is not None and (not isinstance(cached["patch_id"], str) or not OID.fullmatch(cached["patch_id"]))):
            raise MirrorError("links_state_invalid", "Invalid cached commit metadata", 503)
    else:
        raw = runner.git(scope["repo"], ["cat-file", "commit", oid], min(runner.code["limits"]["command_output_bytes"], 1024**2))
        parents, stamp, message = _commit_metadata(raw)
    patch = {"patch_id": None, "status": "outside_horizon", "algorithm": evidence.PATCH_ALGORITHM, "git_version": None, "warnings": []}
    if cached and stamp >= horizon and cached["algorithm"] == evidence.PATCH_ALGORITHM and cached["patch_status"] != "outside_horizon" and cached["git_version"] == runner.git_version:
        patch = {"patch_id": cached["patch_id"], "status": cached["patch_status"], "algorithm": cached["algorithm"],
                 "git_version": cached["git_version"], "warnings": _decode(cached["warnings"])}
    elif stamp >= horizon:
        if len(parents) > 1:
            patch.update(status="candidate", warnings=["merge_commit_not_patch_equivalent"])
        else:
            try:
                diff = runner.git(scope["repo"], ["show", "--format=", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", "--full-index", "--diff-algorithm=myers", oid, "--"])
            except MirrorError as exc:
                if exc.code != "query_output_limit":
                    raise
                patch.update(status="candidate", warnings=["commit_patch_input_limit"])
            else:
                patch = evidence.commit_patch_evidence(diff, parent_count=len(parents), timeout=runner.evidence_timeout(),
                    maximum_bytes=runner.options["max_patch_bytes"], lock_fd=runner.lock_fd,
                    memory_bytes=runner.code["limits"]["query_memory_bytes"])
    _check_evidence_budget(patch)
    runner.guard()
    db.execute("INSERT OR REPLACE INTO commits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (scope["tree"], oid, scope["fingerprint"],
        _json(parents), stamp, patch.get("patch_id"), patch["status"], evidence.PATCH_ALGORITHM, patch.get("git_version"),
        _json(patch.get("warnings", [])), scope["generation"], _now()))
    if message is not None:
        for citation in evidence.commit_trailer_evidence(message, known_mids=None):
            mid = citation.get("message_id")
            if mid:
                db.execute("INSERT OR IGNORE INTO citations VALUES(?,?,?,?,?,?)", (scope["tree"], oid, scope["fingerprint"], mid, _json(citation), _now()))
    db.execute("DELETE FROM frontier WHERE job=? AND oid=?", (job, oid))
    db.execute("INSERT OR IGNORE INTO visited VALUES(?,?)", (job, oid))
    for parent in parents:
        if not db.execute("SELECT 1 FROM visited WHERE job=? AND oid=?", (job, parent)).fetchone():
            db.execute("INSERT OR IGNORE INTO frontier VALUES(?,?)", (job, parent))
    db.execute("UPDATE jobs SET scanned=scanned+1 WHERE id=?", (job,))



def _check_evidence_budget(result):
    limits = {"patch_id_timeout": "query_timeout", "patch_id_memory_limit": "query_memory_limit",
              "patch_id_output_limit": "query_output_limit"}
    for warning in result.get("warnings", []):
        if warning in limits:
            raise MirrorError(limits[warning], "Patch evidence exhausted its finite resource budget", 413)

def _defer(exc, runner):
    if _is_budget_error(exc):
        runner.deferred_reason = getattr(exc, "code", "links_storage_limit")
        return True
    return False


def _scan_commits(db, scopes, jobs, runner, evidence):
    done = 0
    horizon = int(dt.datetime.fromisoformat(runner.options["patch_since"]).replace(tzinfo=dt.timezone.utc).timestamp())
    for job, scope in zip(jobs, scopes):
        while done < runner.options["batch_commits"]:
            db.execute("SAVEPOINT commit_item")
            try:
                runner.guard()
                row = db.execute("SELECT oid FROM frontier WHERE job=? ORDER BY oid LIMIT 1", (job,)).fetchone()
                if not row:
                    db.execute("UPDATE jobs SET status='complete',completed_at=COALESCE(completed_at,?) WHERE id=?", (_now(), job))
                    db.execute("RELEASE commit_item")
                    break
                _one_commit(db, job, scope, row[0], runner, evidence, horizon)
                db.execute("RELEASE commit_item")
                done += 1
            except Exception as exc:
                if not db.in_transaction:
                    raise
                db.execute("ROLLBACK TO commit_item")
                db.execute("RELEASE commit_item")
                if _defer(exc, runner):
                    return done
                raise
        if done >= runner.options["batch_commits"]:
            break
    return done


def _mail_record(db, record, backend, runner, evidence, generation):
    source_blob = record.get("blob")
    identity = source_blob or record.get("variant")
    if not isinstance(identity, str) or not OID.fullmatch(identity):
        raise MirrorError("links_source_invalid", "Mail source returned invalid variant identity", 503)
    # Raw Git blobs and fixture raw-SHA256 variants are immutable identities.
    column = "source_blob" if source_blob else "raw_sha256"
    cached = db.execute(f"SELECT 1 FROM mail_variants WHERE {column}=? AND algorithm=? AND policy=? LIMIT 1",
                        (identity, evidence.PATCH_ALGORITHM, runner.policy)).fetchone()
    if cached:
        return None
    if isinstance(backend, PublicInboxBackend):
        size = runner.git(backend.extindex / "ALL.git", ["cat-file", "-s", identity], 128).strip()
        if not size.isdigit():
            raise MirrorError("links_source_invalid", "Invalid raw mail size", 503)
        if int(size) > runner.options["max_patch_bytes"]:
            return 1
        raw = runner.git(backend.extindex / "ALL.git", ["cat-file", "blob", identity])
    elif isinstance(backend, FixtureBackend):
        with sqlite3.connect(backend.db.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1) as source:
            row = source.execute("SELECT CASE WHEN length(raw)<=? THEN raw ELSE NULL END FROM messages WHERE variant=?",
                                 (runner.options["max_patch_bytes"], identity)).fetchone()
        if not row or row[0] is None:
            return 1
        raw = row[0]
    else:
        raw = backend.raw(record)  # Offline fixture adapter only.
    if not isinstance(raw, bytes) or len(raw) > runner.options["max_patch_bytes"]:
        return 1
    result = evidence.mail_patch_evidence(raw, timeout=runner.evidence_timeout(),
        patch_since=int(dt.datetime.fromisoformat(runner.options["patch_since"]).replace(tzinfo=dt.timezone.utc).timestamp()),
        maximum_bytes=runner.options["max_patch_bytes"], lock_fd=runner.lock_fd,
                    memory_bytes=runner.code["limits"]["query_memory_bytes"])
    _check_evidence_budget(result)
    runner.guard()
    mid = result.get("message_id")
    mids = result.get("message_ids", [])
    if not mid or mids != [mid]:
        return 1  # Ambiguous raw identity cannot establish an exact citation.
    variant = hashlib.sha256(raw).hexdigest()
    db.execute("INSERT OR REPLACE INTO mail_variants VALUES(?,?,?,?,?,?,?,?,?,?,?)", (mid, variant, source_blob,
        result.get("patch_id"), result["status"], evidence.PATCH_ALGORITHM, result.get("git_version"),
        _json(result.get("warnings", [])), runner.policy, generation, _now()))
    return int(bool(result.get("warnings")))


def _scan_mail(db, backend, runner, evidence):
    cursor = _get(db, "mail_cursor")
    previous = _get(db, "mail_coverage", {})
    done = skipped = 0
    reset = False
    # Already-hashed variants cost one probe, not a quota slot, so a pass that
    # restarts after the archive changes reaches new mail. They stop at half
    # of the remaining window; a deadline would block the unit.
    soft_deadline = (time.monotonic() + runner.work_deadline) / 2
    while done < runner.options["batch_mails"]:
        db.execute("SAVEPOINT mail_item")
        try:
            runner.guard()
            # Limit-one calls allow every completed raw variant to have an exact
            # checkpoint, without advancing past unprocessed batch entries.
            enumeration_timeout = min(runner.work_deadline-time.monotonic(), getattr(backend, "cfg", {}).get("limits", {}).get("query_timeout", 120))
            try:
                batch = backend.iter_variants(after=cursor, limit=1, timeout=enumeration_timeout)
            except MirrorError as exc:
                if exc.code != "stale_variant_cursor" or reset:
                    raise
                cursor = None
                reset = True
                batch = backend.iter_variants(after=None, limit=1, timeout=enumeration_timeout)
            if not isinstance(batch, dict) or not isinstance(batch.get("items"), list) or len(batch["items"]) > 1:
                raise MirrorError("links_source_invalid", "Mail enumerator exceeded its batch contract", 503)
            generation = str(batch.get("generation", "unknown"))
            if previous.get("complete") and previous.get("generation") == generation and previous.get("policy") == runner.policy:
                db.execute("RELEASE mail_item")
                return done  # Only the bounded generation probe, no rehashing.
            cached = 0
            for record in batch["items"]:
                outcome = _mail_record(db, record, backend, runner, evidence, generation)
                if outcome is None:
                    cached += 1
                else:
                    skipped += outcome
                    done += 1
            cursor = batch.get("next_cursor")
            coverage = {"generation": generation, "policy": runner.policy, "initial_generation": str(batch.get("initial_generation", generation)),
                "complete": bool(batch.get("complete")), "processed_total": previous.get("processed_total", 0) + done,
                "skipped_or_ambiguous_total": previous.get("skipped_or_ambiguous_total", 0) + skipped,
                "source": batch.get("coverage", {}), "cursor_reset": reset,
                "qualification": "Exact raw identities encountered in the declared local inventory pass; exclusions remain unknown."}
            _put(db, "mail_cursor", cursor)
            _put(db, "mail_coverage", coverage)
            db.execute("RELEASE mail_item")
            if cursor is None:
                break  # Complete or dirty terminal pass; restart only next unit.
            if cached and time.monotonic() >= soft_deadline:
                break
        except Exception as exc:
            if not db.in_transaction:
                raise
            db.execute("ROLLBACK TO mail_item")
            db.execute("RELEASE mail_item")
            if _defer(exc, runner):
                return done
            raise
    return done



def _is_budget_error(exc):
    return (isinstance(exc, MirrorError) and exc.code in {
        "links_timeout", "query_timeout", "links_output_limit", "query_output_limit",
        "query_stderr_limit", "query_memory_limit", "links_storage_limit", "links_state_limit"}
        or storage_error(exc))


def _budget_fingerprint(code):
    limits = {key: code["limits"][key] for key in ("command_output_bytes", "query_memory_bytes")}
    scopes = [{"tree": tree["id"], "catalog": code_catalog_fingerprint(tree), "refs": tree["index_refs"]}
              for tree in code["trees"] if tree["index_refs"]]
    return hashlib.sha256(_json([limits, code["links"], scopes, code["min_free_fraction"]]).encode()).hexdigest()


def _read_budget_state(root):
    _safe_path(root)
    path = root / "links-state.json"
    if path.is_symlink():
        raise MirrorError("links_state_invalid", "Evidence budget checkpoint must not be a symlink", 503)
    if not path.exists():
        return {}
    with path.open("rb") as source:
        raw = source.read(MAX_FIELD_BYTES + 1)
    try:
        state = json.loads(raw) if len(raw) <= MAX_FIELD_BYTES else None
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or state.get("budget_block") is not None and not isinstance(state["budget_block"], dict)):
            raise ValueError
        block = state.get("budget_block")
        if block is not None:
            fingerprint = block.get("fingerprint")
            if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                    or any(c not in "0123456789abcdef" for c in fingerprint)
                    or state.get("status") != "blocked" or type(state.get("generation")) is not int
                    or state["generation"] < 0):
                raise ValueError
        return state
    except (ValueError, RecursionError) as exc:
        raise MirrorError("links_state_invalid", "Invalid evidence budget checkpoint; preserved for recovery", 503) from exc


def _save_budget_state(root, result, fingerprint=None):
    state = dict(result, budget_block={"fingerprint": fingerprint} if fingerprint else None)
    if len(_json(state).encode()) > MAX_FIELD_BYTES:
        raise MirrorError("links_state_limit", "Evidence budget checkpoint exceeds its bound", 413)
    atomic_json(root / "links-state.json", state)

def sync_links_once(config, *, mail_backend=None, adapter=None, should_yield=None):
    """One explicit finite backfill batch, never invoked by a read API."""
    code = normalize_code_config(config.get("code"), data_dir=config["data_dir"])
    result = {"schema_version": LINKS_SCHEMA_VERSION, "generation": 0, "status": "disabled",
              "processed_commits": 0, "processed_mails": 0, "coverage": {}, "last_error": None}
    if not code["links"]["enabled"]:
        return result
    if should_yield and should_yield():
        return dict(result, status="partial", last_error="Deferred to due mail work")
    from . import code_link_evidence
    evidence = adapter or code_link_evidence
    root = Path(code["root_dir"])
    root.mkdir(parents=True, exist_ok=True)
    data = Path(config["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    db = None
    with (data / "index.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(result, status="waiting_for_io")
        fingerprint = _budget_fingerprint(code)
        prior = None
        try:
            prior = _read_budget_state(root)
            if (prior.get("budget_block") or {}).get("fingerprint") == fingerprint:
                return dict(prior, status="blocked", processed_commits=0, processed_mails=0, last_result="limits_unchanged")
            runner = _Runner(code, lock.fileno())
            runner.guard()
            scopes = _snapshots(code)
            db = _open_writer(root, code["links"]["max_db_bytes"])
            db.set_progress_handler(lambda: int(time.monotonic() >= runner.deadline), 1000)
            db.execute("BEGIN IMMEDIATE")
            runner.git_version = runner.git(None, ["--version"], 128).decode("ascii").strip()
            runner.policy = hashlib.sha256(_json({"patch_since": code["links"]["patch_since"],
                "max_patch_bytes": code["links"]["max_patch_bytes"], "algorithm": evidence.PATCH_ALGORITHM,
                "git_version": runner.git_version}).encode()).hexdigest()
            if _get(db, "mail_policy") != runner.policy:
                _put(db, "mail_cursor", None)
                old_coverage = _get(db, "mail_coverage", {})
                _put(db, "mail_coverage", dict(old_coverage, complete=False))
                _put(db, "mail_policy", runner.policy)
            jobs = _ensure_jobs(db, scopes, code["links"], evidence.PATCH_ALGORITHM, runner.policy)
            backend = mail_backend or (FixtureBackend(config) if config.get("backend") == "fixture" else PublicInboxBackend(config))
            result["processed_mails"] = _scan_mail(db, backend, runner, evidence)
            if runner.deferred_reason is None:
                result["processed_commits"] = _scan_commits(db, scopes, jobs, runner, evidence)
            runner.guard(finishing=True)
            if _snapshots(code) != scopes:
                raise MirrorError("links_source_changed", "Code snapshot changed during evidence batch", 409)
            complete = all(db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0] == "complete" for job in jobs)
            complete = complete and bool(_get(db, "mail_coverage", {}).get("complete"))
            result.update(generation=_get(db, "generation") + 1,
                          status="blocked" if runner.deferred_reason else "ready" if complete else "partial",
                          last_error=runner.deferred_reason)
            _put(db, "generation", result["generation"])
            _put(db, "status", result["status"])
            _put(db, "algorithm", evidence.PATCH_ALGORITHM)
            _put(db, "last_error", runner.deferred_reason)
            db.commit()
            result["coverage"] = {"mail": _get(db, "mail_coverage", {}), "current_jobs": len(jobs), "complete": complete, "deferred_reason": runner.deferred_reason}
            _save_budget_state(root, result, fingerprint if runner.deferred_reason else None)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception as exc:
            if db is not None:
                db.rollback()
                with contextlib.suppress(Exception):
                    result["generation"] = _get(db, "generation", 0)
            budget_blocked = _is_budget_error(exc)
            result.update(status="blocked" if budget_blocked or not result["generation"] else "degraded",
                          last_error=str(exc)[:1000], processed_commits=0, processed_mails=0)
            if budget_blocked or prior is not None and prior.get("budget_block"):
                try:
                    _save_budget_state(root, result, fingerprint if budget_blocked else None)
                except OSError as checkpoint_error:
                    result["retry_checkpoint_error"] = str(checkpoint_error)[:500]
        finally:
            if db is not None:
                db.close()
    return result
