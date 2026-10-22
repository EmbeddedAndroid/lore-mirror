"""Offline local evidence coordinator fixtures, never production repositories."""
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from lore_mirror.code_config import code_catalog_fingerprint, normalize_code_config
from lore_mirror.code_links import LinksStore, sync_links_once
from lore_mirror.code_sync import SAFE_REPO_CONFIG
from lore_mirror.errors import MirrorError


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


class MailFixture:
    def __init__(self, messages):
        self.messages = messages
        self.calls = 0
        self.generation = "fixture1"
    def iter_variants(self, after=None, limit=25, timeout=None):
        self.calls += 1
        start = after or 0
        rows = [{"variant": hashlib.sha256(raw).hexdigest(), "blob": hashlib.sha256(raw).hexdigest(), "raw": raw}
                for raw in self.messages[start:start+limit]]
        end = start + len(rows)
        return {"items": rows, "next_cursor": end if end < len(self.messages) else None,
                "complete": end >= len(self.messages), "generation": self.generation}
    def raw(self, record):
        return record["raw"]


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.code_root = self.data / "code"
        self.repo = self.code_root / "repositories" / "boot.git"
        self.work = self.root / "work"
        self.work.mkdir()
        git(self.work, "init", "-b", "main")
        git(self.work, "config", "user.name", "Evidence Test")
        git(self.work, "config", "user.email", "test@example.invalid")
        (self.work / "file.c").write_text("int old;\n")
        git(self.work, "add", "file.c")
        git(self.work, "commit", "-m", "base")
        self.base = git(self.work, "rev-parse", "HEAD").decode().strip()
        (self.work / "file.c").write_text("int replacement;\n")
        git(self.work, "commit", "-am", "Change\n\nLink: https://lore.kernel.org/all/patch@example.invalid/")
        self.oid = git(self.work, "rev-parse", "HEAD").decode().strip()
        patch = git(self.work, "show", "--format=", "--no-renames", "--full-index", self.oid)
        self.mail = b"Message-ID: <patch@example.invalid>\nSubject: [PATCH] Change\nDate: Wed, 21 Oct 2026 12:00:00 +0000\nContent-Type: text/plain; charset=UTF-8\n\n" + patch
        self.backend = MailFixture([self.mail])
        self.repo.parent.mkdir(parents=True)
        git(self.work, "clone", "--bare", str(self.work), str(self.repo))
        (self.repo / "config").write_text(SAFE_REPO_CONFIG)
        self.config = {"data_dir": str(self.data), "code": {"enabled": True,
            "trees": [{"id": "boot", "url": "https://git.u-boot-project.org/u-boot/u-boot.git", "default_ref": "refs/heads/main",
                       "refs": ["refs/heads/main"], "index_refs": ["refs/heads/main"]}],
            "links": {"enabled": True, "patch_since": "2020-01-01"}}}
        self.publish()
        self.store = LinksStore(self.code_root)

    def publish(self, generation=1):
        code = normalize_code_config(self.config["code"], data_dir=self.data)
        tree = code["trees"][0]
        (self.code_root / "code-state.json").write_text(json.dumps({"schema_version": 1, "generation": generation,
            "trees": {"boot": {"published_refs": {"refs/heads/main": self.oid}, "config_fingerprint": code_catalog_fingerprint(tree),
                              "upstream_url": tree["url"]}}}))

    def run_once(self, **kwargs):
        return sync_links_once(self.config, mail_backend=self.backend, **kwargs)

    def test_exact_citation_and_equivalence_are_distinct(self):
        result = self.run_once()
        self.assertEqual(result["status"], "ready", result)
        rows = self.store.related_mail("boot", self.oid)["items"]
        self.assertEqual({row["evidence_level"] for row in rows}, {"linked", "patch_equivalent"})
        self.assertTrue(all(row["raw_sha256"] == hashlib.sha256(self.mail).hexdigest() for row in rows))
        self.assertTrue(self.store.coverage("boot", "refs/heads/main", self.oid)["complete"])
        self.assertEqual(len(self.store.related_commits("patch@example.invalid", "boot")["items"]), 2)
        self.assertEqual(len(self.store.related_commits("patch@example.invalid", "boot", variant=rows[0]["source_blob"])["items"]), 2)

    def test_disabled_does_no_work(self):
        self.config["code"]["links"]["enabled"] = False
        self.assertEqual(self.run_once()["status"], "disabled")
        self.assertEqual(self.backend.calls, 0)
        self.assertFalse((self.code_root / "links.sqlite3").exists())

    def test_bounded_resume_and_atomic_rollback(self):
        self.config["code"]["links"]["batch_commits"] = 1
        first = self.run_once()
        self.assertEqual(first["status"], "partial", first)
        generation = self.store.generation()
        with mock.patch("lore_mirror.code_links._scan_commits", side_effect=RuntimeError("interrupted")):
            failed = self.run_once()
        self.assertEqual(failed["status"], "degraded")
        self.assertEqual(self.store.generation(), generation)
        self.assertEqual(self.run_once()["status"], "partial")
        self.assertEqual(self.run_once()["status"], "ready")

    def test_lock_collision_no_db_creation(self):
        with (self.data / "index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_once()["status"], "waiting_for_io")
        self.assertFalse((self.code_root / "links.sqlite3").exists())

    def test_missing_unknown_and_symlink_database(self):
        with self.assertRaisesRegex(MirrorError, "backfill"):
            self.store.generation()
        self.run_once()
        self.assertEqual(self.store.related_commits("absent@example.invalid", "boot")["items"], [])
        self.assertIn("not evidence", self.store.related_commits("absent@example.invalid", "boot")["metadata"]["qualification"])
        path = self.code_root / "links.sqlite3"
        moved = self.code_root / "moved.sqlite3"
        path.rename(moved)
        path.symlink_to(moved)
        self.assertEqual(self.run_once()["status"], "blocked")
        with self.assertRaises(MirrorError):
            self.store.generation()

    def test_horizon_and_unresolved_citation_do_not_invent_matches(self):
        self.config["code"]["links"]["patch_since"] = "2099-01-01"
        self.assertEqual(self.run_once()["status"], "ready")
        rows = self.store.related_mail("boot", self.oid)["items"]
        self.assertEqual([row["evidence_level"] for row in rows], ["linked"])
        self.assertEqual(self.store.related_mail("boot", self.base)["items"], [])

    def test_git_version_mismatch_never_equates(self):
        self.assertEqual(self.run_once()["status"], "ready")
        with sqlite3.connect(self.code_root / "links.sqlite3") as db:
            db.execute("UPDATE mail_variants SET git_version='git version incompatible'")
        rows = self.store.related_mail("boot", self.oid)["items"]
        self.assertEqual([row["evidence_level"] for row in rows], ["linked"])
        self.assertEqual(rows[0]["evidence_version"], "commit-trailer/lore-final-block-v1")

    def test_configured_timeout_120_is_capped_for_evidence_helpers(self):
        self.config["code"]["links"]["timeout_seconds"] = 120
        self.assertEqual(self.run_once()["status"], "ready")

    def test_ambiguous_mail_identity_cannot_confirm_a_trailer(self):
        self.backend.messages = [self.mail.replace(b"Subject:", b"Message-ID: <other@example.invalid>\nSubject:")]
        self.assertEqual(self.run_once()["status"], "ready")
        self.assertEqual(self.store.related_mail("boot", self.oid)["items"], [])
        self.assertEqual(self.store.status()["coverage"]["mail"]["skipped_or_ambiguous_total"], 1)

    def test_old_mail_has_citation_but_no_patch_equivalence(self):
        self.backend.messages = [self.mail.replace(b"21 Oct 2026", b"21 Oct 1999")]
        self.assertEqual(self.run_once()["status"], "ready")
        self.assertEqual([row["evidence_level"] for row in self.store.related_mail("boot", self.oid)["items"]], ["linked"])

    def test_oversize_mail_advances_and_counts_exclusions(self):
        self.config["code"]["links"]["max_patch_bytes"] = 1024
        self.backend.messages = [b"x" * 2048, self.mail]
        self.assertEqual(self.run_once()["status"], "ready")
        self.assertEqual(len(self.store.related_mail("boot", self.oid)["items"]), 2)
        self.assertEqual(self.store.status()["coverage"]["mail"]["skipped_or_ambiguous_total"], 1)
        self.run_once()
        self.assertEqual(self.store.status()["coverage"]["mail"]["skipped_or_ambiguous_total"], 1)

    def test_oversize_diff_is_candidate_and_history_advances(self):
        (self.work / "file.c").write_text("int large;\n" * 500)
        git(self.work, "commit", "-am", "large change")
        self.oid = git(self.work, "rev-parse", "HEAD").decode().strip()
        git(self.repo, "fetch", str(self.work), "main")
        self.publish(generation=2)
        self.config["code"]["links"]["max_patch_bytes"] = 1024
        self.assertEqual(self.run_once()["status"], "ready")
        with sqlite3.connect(self.code_root / "links.sqlite3") as db:
            row = db.execute("SELECT patch_id,patch_status,warnings FROM commits WHERE oid=?", (self.oid,)).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "candidate")
        self.assertIn("input_limit", row[2])

    def test_catalog_changes_replace_active_scope_without_poisoning_reads(self):
        self.run_once()
        first = self.store.related_mail("boot", self.oid)["items"][0]["catalog_fingerprint"]
        self.config["code"]["trees"][0]["refs"].append("refs/heads/another")
        self.publish(generation=2)
        self.assertEqual(self.run_once()["status"], "ready")
        rows = self.store.related_mail("boot", self.oid)["items"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["catalog_fingerprint"] != first for row in rows))

    def test_unchanged_completed_mail_pass_does_not_rehash(self):
        self.run_once()
        with mock.patch("lore_mirror.code_link_evidence.mail_patch_evidence", side_effect=AssertionError("rehash")):
            self.assertEqual(self.run_once()["status"], "ready")

    def test_changed_archive_pass_spends_quota_only_on_new_mail(self):
        mails = [b"Message-ID: <m%d@example.invalid>\nSubject: note %d\nDate: Wed, 21 Oct 2026 12:00:00 +0000\n\nbody\n" % (i, i)
                 for i in range(20)]
        self.backend.messages[:] = mails
        self.config["code"]["links"]["batch_mails"] = 5
        runs = 0
        while not self.run_once()["coverage"]["mail"].get("complete"):
            runs += 1
            self.assertLess(runs, 10)
        self.backend.messages.insert(0, mails[0].replace(b"<m0@", b"<new@"))
        self.backend.generation = "fixture2"
        with mock.patch("lore_mirror.code_link_evidence.mail_patch_evidence",
                        wraps=__import__("lore_mirror.code_link_evidence", fromlist=["x"]).mail_patch_evidence) as hashed:
            result = self.run_once()
        self.assertTrue(result["coverage"]["mail"]["complete"], result)
        self.assertEqual(hashed.call_count, 1)

    def test_mail_cache_probe_uses_an_index(self):
        self.run_once()
        with sqlite3.connect(self.code_root / "links.sqlite3") as db:
            for column in ("source_blob", "raw_sha256"):
                plan = " ".join(row[-1] for row in db.execute(
                    f"EXPLAIN QUERY PLAN SELECT 1 FROM mail_variants WHERE {column}=? AND algorithm=? AND policy=? LIMIT 1", ("a", "b", "c")))
                self.assertIn("mail_raw" if column == "raw_sha256" else "mail_blob", plan)

    def test_moved_tip_resumes_the_unfinished_walk(self):
        for index in range(4):
            (self.work / "file.c").write_text(f"int value{index};\n")
            git(self.work, "commit", "-qam", f"step {index}")
        def advance(generation):
            git(self.repo, "fetch", "--quiet", str(self.work), "+refs/heads/main:refs/heads/main")
            self.oid = git(self.work, "rev-parse", "HEAD").decode().strip()
            self.publish(generation)
        advance(1)
        self.config["code"]["links"]["batch_commits"] = 2
        self.assertEqual(self.run_once()["status"], "partial")
        (self.work / "file.c").write_text("int moved;\n")
        git(self.work, "commit", "-qam", "moved tip")
        advance(2)
        runs = 0
        while self.run_once()["status"] != "ready":
            runs += 1
            self.assertLess(runs, 10)
        with sqlite3.connect(self.code_root / "links.sqlite3") as db:
            jobs = dict(db.execute("SELECT status,scanned FROM jobs"))
            self.assertEqual(jobs, {"superseded": 2, "complete": 7})
            self.assertEqual(db.execute("SELECT count(*) FROM visited").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT count(*) FROM frontier").fetchone()[0], 0)

    def test_expanded_horizon_reprocesses_mail_and_commits(self):
        self.config["code"]["links"]["patch_since"] = "2099-01-01"
        self.run_once()
        self.assertEqual(len(self.store.related_mail("boot", self.oid)["items"]), 1)
        self.config["code"]["links"]["patch_since"] = "2020-01-01"
        self.assertEqual(self.run_once()["status"], "ready")
        self.assertEqual(len(self.store.related_mail("boot", self.oid)["items"]), 2)

    def test_invalid_checkpoint_oid_never_becomes_git_option(self):
        self.config["code"]["links"]["batch_commits"] = 1
        self.run_once()
        with sqlite3.connect(self.code_root / "links.sqlite3") as db:
            db.execute("UPDATE frontier SET oid='--output=/tmp/unsafe-evidence'")
        from lore_mirror.code_links import _Runner
        original = _Runner.git
        calls = []
        def command(runner, repo, args, maximum=None):
            calls.append(args)
            return original(runner, repo, args, maximum)
        with mock.patch("lore_mirror.code_links._Runner.git", new=command):
            result = self.run_once()
        self.assertEqual(result["status"], "degraded")
        self.assertIn("checkpoint commit identity", result["last_error"])
        self.assertTrue(all("--output=/tmp/unsafe-evidence" not in str(call) for call in calls))

    def test_malformed_cached_parents_and_reader_fields_fail_bounded(self):
        self.config["code"]["links"]["batch_commits"] = 1
        self.run_once()
        with sqlite3.connect(self.code_root / "links.sqlite3") as db:
            db.execute("UPDATE commits SET parents='[\"--output=bad\"]'")
            job = db.execute("SELECT id FROM jobs LIMIT 1").fetchone()[0]
            db.execute("DELETE FROM frontier")
            db.execute("INSERT OR IGNORE INTO frontier VALUES(?,?)", (job, self.oid))
        result = self.run_once()
        self.assertEqual(result["status"], "degraded")
        self.assertIn("cached commit", result["last_error"])
        with sqlite3.connect(self.code_root / "links.sqlite3") as db:
            db.execute("UPDATE commits SET warnings='{}'")
        with self.assertRaises(MirrorError):
            self.store.related_mail("boot", self.oid)

    def test_empty_initialization_resumes_and_deadline_keeps_completed_work(self):
        (self.code_root / "links.sqlite3").touch()
        from lore_mirror.code_links import _one_commit
        count = 0
        def limited(*args):
            nonlocal count
            count += 1
            if count > 1:
                raise MirrorError("links_timeout", "simulated deadline", 504)
            return _one_commit(*args)
        with mock.patch("lore_mirror.code_links._one_commit", side_effect=limited):
            first = self.run_once()
        self.assertEqual(first["status"], "blocked", first)
        self.assertEqual(first["processed_commits"], 1)
        self.assertEqual(self.store.generation(), "1")
        with mock.patch("lore_mirror.code_links._Runner.git", side_effect=AssertionError("automatic retry")):
            self.assertEqual(self.run_once()["status"], "blocked")
        self.config["code"]["links"]["timeout_seconds"] = 31
        self.assertEqual(self.run_once()["status"], "ready")


    def test_storage_budget_failure_does_not_retry_until_work_or_limits_change(self):
        first = self.run_once()
        with mock.patch("lore_mirror.code_links.storage_available", return_value=0):
            blocked = self.run_once()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.store.status()["status"], "blocked")
        self.assertEqual(self.store.generation(), str(first["generation"]))
        self.config["code"]["limits"] = {"max_sessions": 17}
        with mock.patch("lore_mirror.code_links._Runner.git", side_effect=AssertionError("automatic retry")):
            self.assertEqual(self.run_once()["last_result"], "limits_unchanged")
        self.config["code"]["links"]["batch_commits"] = 1
        self.assertEqual(self.run_once()["status"], "ready")
        self.assertEqual(self.store.status()["status"], "ready")

    def test_lookup_snapshot_never_holds_heavy_writer_lock(self):
        self.run_once()
        with self.store._read() as db:
            self.assertTrue(db.in_transaction)
            generation = db.execute("SELECT value FROM info WHERE key='generation'").fetchone()[0]
            with (self.data / "index.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(self.store.generation(), "1")
            self.assertEqual(db.execute("SELECT value FROM info WHERE key='generation'").fetchone()[0], generation)

    def test_writer_waits_longer_than_reader_deadline(self):
        from lore_mirror.code_links import READ_TIMEOUT_SECONDS, _open_writer
        self.run_once()
        started, finished, errors = threading.Event(), threading.Event(), []
        def write():
            try:
                with _open_writer(self.code_root, 16 * 1024**2) as db:
                    self.assertGreater(db.execute("PRAGMA busy_timeout").fetchone()[0], READ_TIMEOUT_SECONDS * 1000)
                    db.execute("UPDATE info SET value='2' WHERE key='generation'")
                    started.set()
                    db.commit()
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        worker = threading.Thread(target=write)
        with self.store._read() as reader:
            reader.execute("SELECT value FROM info WHERE key='generation'").fetchone()
            worker.start()
            try:
                self.assertTrue(started.wait(2), errors)
                self.assertFalse(finished.wait(.25), errors)
            finally:
                reader.rollback()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(self.store.generation(), "2")

    def test_wal_lookup_keeps_one_generation_while_writer_commits(self):
        self.run_once()
        path = self.code_root / "links.sqlite3"
        with sqlite3.connect(path) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            with self.store._read() as db:
                before = db.execute("SELECT value FROM info WHERE key='generation'").fetchone()[0]
                writer.execute("UPDATE info SET value='2' WHERE key='generation'")
                writer.commit()
                self.assertEqual(db.execute("SELECT value FROM info WHERE key='generation'").fetchone()[0], before)
        self.assertEqual(self.store.generation(), "2")

    def test_independent_corrupt_database_does_not_touch_mail_or_code(self):
        source_before = (self.code_root / "code-state.json").read_bytes()
        (self.code_root / "links.sqlite3").write_bytes(b"corrupt")
        result = self.run_once()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual((self.code_root / "code-state.json").read_bytes(), source_before)
        with self.assertRaises(MirrorError):
            self.store.status()


if __name__ == "__main__":
    unittest.main()
