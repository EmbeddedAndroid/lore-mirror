"""Bounded inventory of raw identities, including native transport variants."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lore_mirror.backend import FixtureBackend, PublicInboxBackend
from lore_mirror.config import load_config
from lore_mirror.errors import MirrorError
from tests import test_native_e2e as native


class VariantEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config({"data_dir": str(self.root), "runtime_dir": str(self.root / "run"),
            "backend": "fixture", "lists": [{"id": "test", "newsgroup": "org.test", "class": "fixture"}]})
        self.backend = FixtureBackend(self.cfg)
        with sqlite3.connect(self.backend.db) as db:
            db.execute("CREATE TABLE messages(variant TEXT PRIMARY KEY,message_id TEXT,meta TEXT,raw BLOB)")

    def insert(self, variant, *, mid="same@example.org", meta=None, raw=b"mail"):
        with sqlite3.connect(self.backend.db) as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?)", (variant, mid,
                json.dumps(meta if meta is not None else {"list_ids": ["test"]}), raw))

    def collect(self, backend=None, after=None, limit=1):
        backend = backend or self.backend
        items = []
        for _ in range(200):
            batch = backend.iter_variants(after=after, limit=limit)
            items.extend(batch["items"])
            after = batch["next_cursor"]
            if after is None:
                return items, batch
        self.fail("inventory did not terminate")

    def error(self, code, call):
        with self.assertRaises(MirrorError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    @contextmanager
    def same_filesystem_tick(self):
        """Model coarse timestamp clocks without hiding size/inode changes."""
        original = Path.stat
        def stat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
            fields.update(st_mtime=1000, st_ctime=1000, st_mtime_ns=1000000000000, st_ctime_ns=1000000000000)
            return SimpleNamespace(**fields)
        with patch.object(Path, "stat", new=stat):
            yield

    def test_empty_inventory_is_complete(self):
        result = self.backend.iter_variants()
        self.assertEqual(result["items"], [])
        self.assertTrue(result["complete"])
        self.assertTrue(result["pass_complete"])
        self.assertIsNone(result["coverage"]["highwater"])

    def test_fixture_keyset_retains_same_mid_variants_and_raw_without_search(self):
        for value in ("c", "a", "b"):
            self.insert(value * 64, raw=value.encode())
        before = self.backend.db.read_bytes()
        with patch.object(self.backend, "_rows", side_effect=AssertionError("full scan")):
            items, final = self.collect()
        self.assertEqual([item["variant"] for item in items], [value * 64 for value in "abc"])
        self.assertEqual([self.backend.raw(item) for item in items], [b"a", b"b", b"c"])
        self.assertTrue(final["complete"])
        self.assertEqual(before, self.backend.db.read_bytes())

    def test_arrival_before_cursor_dirties_pass_then_fresh_pass_finds_it(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        first = self.backend.iter_variants(limit=1)
        self.insert("1" * 64)
        items, final = self.collect(after=first["next_cursor"])
        self.assertEqual([item["variant"] for item in items], ["c" * 64])
        self.assertFalse(final["complete"])
        self.assertTrue(final["pass_complete"])
        self.assertTrue(final["coverage"]["restart_required"])
        items, fresh = self.collect()
        self.assertEqual(len(items), 3)
        self.assertTrue(fresh["complete"])

    def test_highwater_does_not_expand_during_pass(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        first = self.backend.iter_variants(limit=1)
        self.insert("f" * 64)
        items, final = self.collect(after=first["next_cursor"])
        self.assertEqual([item["variant"] for item in items], ["c" * 64])
        self.assertEqual(final["coverage"]["highwater"], "c" * 64)
        self.assertFalse(final["complete"])

    def test_in_place_rewrite_does_not_claim_complete(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        first = self.backend.iter_variants(limit=1)
        with sqlite3.connect(self.backend.db) as db:
            db.execute("DELETE FROM messages WHERE variant=?", ("c" * 64,))
        result = self.backend.iter_variants(after=first["next_cursor"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["coverage"]["restart_required"])

    def test_same_tick_same_size_commit_dirties_pass(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        size = self.backend.db.stat().st_size
        before = self.backend.db.read_bytes()[24:28]
        with self.same_filesystem_tick():
            first = self.backend.iter_variants(limit=1)
            self.insert("1" * 64)  # Arrival behind the cursor, in existing pages.
            self.assertEqual(self.backend.db.stat().st_size, size)
            self.assertNotEqual(self.backend.db.read_bytes()[24:28], before)
            result = self.backend.iter_variants(after=first["next_cursor"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["coverage"]["restart_required"])
        self.assertNotEqual(result["generation"], first["generation"])

    def test_wal_growth_dirties_pass_without_database_counter_change(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        with sqlite3.connect(self.backend.db) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE messages SET message_id='warm@example.org'")
            writer.commit()
            wal = Path(str(self.backend.db) + "-wal")
            before, size = self.backend.db.read_bytes()[24:28], wal.stat().st_size
            with self.same_filesystem_tick():
                first = self.backend.iter_variants(limit=1)
                writer.execute("INSERT INTO messages VALUES(?,?,?,?)", ("1" * 64, "same@example.org", "{}", b"mail"))
                writer.commit()
                self.assertEqual(self.backend.db.read_bytes()[24:28], before)
                self.assertGreater(wal.stat().st_size, size)
                result = self.backend.iter_variants(after=first["next_cursor"])
            self.assertFalse(result["complete"])

    def test_reused_wal_same_salts_size_and_tick_dirties_pass(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        with sqlite3.connect(self.backend.db) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            for index in range(8):
                writer.execute("UPDATE messages SET message_id=?", (f"warm{index}@example.org",))
                writer.commit()
            writer.execute("PRAGMA wal_checkpoint(RESTART)")
            writer.execute("UPDATE messages SET message_id='first@example.org'")
            writer.commit()
            wal = Path(str(self.backend.db) + "-wal")
            before, size, salts = self.backend.db.read_bytes()[24:28], wal.stat().st_size, wal.read_bytes()[16:24]
            with self.same_filesystem_tick():
                first = self.backend.iter_variants(limit=1)
                writer.execute("INSERT INTO messages VALUES(?,?,?,?)", ("1" * 64, "same@example.org", "{}", b"mail"))
                writer.commit()
                self.assertEqual(self.backend.db.read_bytes()[24:28], before)
                self.assertEqual(wal.stat().st_size, size)
                self.assertEqual(wal.read_bytes()[16:24], salts)
                result = self.backend.iter_variants(after=first["next_cursor"])
            self.assertFalse(result["complete"])
            self.assertTrue(result["coverage"]["restart_required"])

    def assert_wal_opening_snapshot_commit_dirties_pass(self, backend, path, *, native_layout=False):
        """Use a real WAL writer at both sides of the first SQLite snapshot read.

        The post-snapshot commit is the dangerous case: all later fingerprints
        describe the new WAL, but this connection still returns the old rows.
        No fingerprint is mocked, and the writer runs on a separate connection
        and thread while the enumeration connection remains open.
        """
        original_connect = sqlite3.connect
        width = 40 if native_layout else 64
        table = "xref3" if native_layout else "messages"
        select = "SELECT oidbin FROM xref3 ORDER BY oidbin" if native_layout else "SELECT variant FROM messages ORDER BY variant"
        insert = "INSERT INTO xref3 VALUES(1,1,1,?)" if native_layout else "INSERT INTO messages VALUES(?,?,?,?)"

        def values(digit):
            identity = digit * width
            return (bytes.fromhex(identity),) if native_layout else (identity, "same@example.org", "{}", b"mail")

        def identities(connection):
            return [row[0].hex() if native_layout else row[0] for row in connection.execute(select).fetchall()]

        with closing(original_connect(path, timeout=.5)) as keeper, ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            for timing in ("before_snapshot", "after_snapshot"):
                for limit in (1, 250):
                    with self.subTest(timing=timing, limit=limit):
                        keeper.execute("DELETE FROM " + table)
                        keeper.executemany(insert, [values(digit) for digit in "4c"])
                        keeper.commit()
                        counter = path.read_bytes()[24:28]
                        wal = Path(str(path) + "-wal")
                        self.assertTrue(wal.exists())
                        old_wal_size = wal.stat().st_size
                        snapshot_rows, commits = [], []

                        def commit_arrival():
                            with closing(original_connect(path, timeout=.5)) as writer:
                                writer.execute("PRAGMA wal_autocheckpoint=0")
                                writer.execute(insert, values("1"))
                                writer.commit()
                                return identities(writer)

                        def run_writer():
                            committed = pool.submit(commit_arrival).result(timeout=3)
                            self.assertEqual(committed, [digit * width for digit in "14c"])
                            commits.append(committed)

                        class SnapshotReader(sqlite3.Connection):
                            def execute(reader, sql, *args, **kwargs):
                                if sql != "PRAGMA schema_version":
                                    return super().execute(sql, *args, **kwargs)
                                if timing == "before_snapshot":
                                    run_writer()
                                cursor = super().execute(sql, *args, **kwargs)
                                if timing == "after_snapshot":
                                    run_writer()
                                # Confirm the actual SQLite read view, rather
                                # than assuming BEGIN or a hook pinned it.
                                snapshot_rows.extend(identities(reader))
                                return cursor

                        def open_reader(*args, **kwargs):
                            return original_connect(*args, factory=SnapshotReader, **kwargs)

                        with patch("lore_mirror.backend.sqlite3.connect", side_effect=open_reader):
                            first = backend.iter_variants(limit=limit)

                        self.assertEqual(len(commits), 1)
                        expected = [digit * width for digit in ("14c" if timing == "before_snapshot" else "4c")]
                        self.assertEqual(snapshot_rows, expected)
                        self.assertEqual(path.read_bytes()[24:28], counter)
                        self.assertGreater(wal.stat().st_size, old_wal_size)
                        # The closing-vs-initial comparison cannot detect this
                        # race: both fingerprints were taken after the commit.
                        self.assertEqual(first["generation"], first["initial_generation"])
                        self.assertTrue(first["coverage"]["source_changed"])
                        self.assertFalse(first["coverage"]["consistent_snapshot"])
                        self.assertFalse(first["complete"])
                        items, final = first["items"], first
                        if first["next_cursor"] is not None:
                            rest, final = self.collect(backend, after=first["next_cursor"])
                            items += rest
                        self.assertEqual([item["variant"] for item in items], expected)
                        self.assertTrue(final["pass_complete"])
                        self.assertFalse(final["complete"])
                        self.assertTrue(final["coverage"]["restart_required"])
                        fresh, final = self.collect(backend)
                        self.assertEqual([item["variant"] for item in fresh], [digit * width for digit in "14c"])
                        self.assertTrue(final["complete"])

    def test_fixture_wal_commit_around_snapshot_open_dirties_pass(self):
        self.assert_wal_opening_snapshot_commit_dirties_pass(self.backend, self.backend.db)

    def test_native_wal_commit_around_snapshot_open_dirties_pass(self):
        backend, path = self.native_backend()
        self.assert_wal_opening_snapshot_commit_dirties_pass(backend, path, native_layout=True)

    def test_idle_wal_sidecar_initialization_does_not_keep_passes_dirty(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        with closing(sqlite3.connect(self.backend.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
        # Opening a previously quiescent WAL database can create sidecars. That
        # first observation is conservative, but unchanged later passes finish.
        self.backend.iter_variants()
        for _ in range(3):
            self.assertTrue(self.backend.iter_variants()["complete"])

    def test_last_wal_writer_close_and_other_reader_reopen_dirties_pass(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        with closing(sqlite3.connect(self.backend.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
        self.backend.iter_variants()  # Settle initial empty-sidecar creation.
        wal, shm = (Path(str(self.backend.db) + suffix) for suffix in ("-wal", "-shm"))
        counter = self.backend.db.read_bytes()[24:28]
        # The archive was last written well before this pass. The recreated
        # sidecars usually reuse their inodes, so only the checkpoint's write
        # to the main file can reveal the insert.
        os.utime(self.backend.db, ns=(10**18, 10**18))
        first = self.backend.iter_variants(limit=1)
        self.assertFalse(first["coverage"]["source_changed"])
        with closing(sqlite3.connect(self.backend.db)) as writer:
            writer.execute("INSERT INTO messages VALUES(?,?,?,?)", ("1" * 64, "same@example.org", "{}", b"mail"))
            writer.commit()
        self.assertEqual(self.backend.db.read_bytes()[24:28], counter)
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        # A different mail query can recreate empty sidecars before the
        # next cursor page. Their replacement identities still dirty it.
        with closing(sqlite3.connect(self.backend.db.as_uri() + "?mode=ro", uri=True)) as reader:
            reader.execute("SELECT count(*) FROM messages").fetchone()
        self.assertTrue(wal.exists())
        self.assertTrue(shm.exists())
        result = self.backend.iter_variants(after=first["next_cursor"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["coverage"]["restart_required"])
        self.assertTrue(self.backend.iter_variants()["complete"])

    def test_schema_and_scope_changes_require_restart(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        cursor = self.backend.iter_variants(limit=1)["next_cursor"]
        self.cfg["lists"][0]["newsgroup"] = "changed"
        self.error("stale_variant_cursor", lambda: self.backend.iter_variants(after=cursor))
        self.cfg["lists"][0]["newsgroup"] = "org.test"
        with sqlite3.connect(self.backend.db) as db:
            db.execute("CREATE TABLE extra(value)")
        self.error("stale_variant_cursor", lambda: self.backend.iter_variants(after=cursor))

    def test_replaced_source_requires_restart(self):
        self.insert("4" * 64)
        self.insert("c" * 64)
        cursor = self.backend.iter_variants(limit=1)["next_cursor"]
        replacement = self.root / "replacement"
        replacement.write_bytes(self.backend.db.read_bytes())
        replacement.replace(self.backend.db)
        self.error("stale_variant_cursor", lambda: self.backend.iter_variants(after=cursor))

    def test_replacement_during_connection_open_fails_closed(self):
        self.insert("a" * 64)
        replacement = self.root / "replacement"
        replacement.write_bytes(self.backend.db.read_bytes())
        original_connect = sqlite3.connect
        def replaced_connection(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            replacement.replace(self.backend.db)
            return connection
        with patch("lore_mirror.backend.sqlite3.connect", side_effect=replaced_connection):
            self.error("stale_variant_cursor", self.backend.iter_variants)

    def test_invalid_limits_and_cursors(self):
        for limit in (True, 0, 251, "1", 1.1):
            self.error("invalid_limit", lambda: self.backend.iter_variants(limit=limit))
        for cursor in ("!", "A" * 4097, "e30", 1):
            self.error("invalid_variant_cursor", lambda: self.backend.iter_variants(after=cursor))

    def test_timeout_override_is_positive_bounded_and_does_not_mutate_config(self):
        original = self.cfg["limits"]["query_timeout"]
        self.assertTrue(self.backend.iter_variants(timeout=.1)["complete"])
        self.assertEqual(self.cfg["limits"]["query_timeout"], original)
        for value in (True, 0, -1, float("nan"), float("inf"), original + 1, "1"):
            self.error("invalid_timeout", lambda: self.backend.iter_variants(timeout=value))

    def test_missing_source_does_not_create_database(self):
        self.backend.db.unlink()
        self.error("variant_enumeration_unavailable", self.backend.iter_variants)
        self.assertFalse(self.backend.db.exists())

    def test_oversize_metadata_does_not_fetch_raw_or_grow_output(self):
        self.insert("a" * 64, meta={"huge": "x" * 200000}, raw=b"x" * 2000000)
        result = self.backend.iter_variants()
        self.assertLess(len(json.dumps(result)), 3000)
        self.assertTrue(result["items"][0]["memberships_partial"])
        self.assertNotIn("raw", result["items"][0])

    def test_output_limit_returns_resumable_prefix(self):
        for value in "abcd":
            self.insert(value * 64)
        self.cfg["limits"]["query_output_bytes"] = 2500
        first = self.backend.iter_variants(limit=100)
        self.assertEqual(len(first["items"]), 1)
        self.assertFalse(first["pass_complete"])
        rest, final = self.collect(after=first["next_cursor"], limit=100)
        self.assertEqual(len(rest), 3)
        self.assertTrue(final["complete"])
        self.cfg["limits"]["query_output_bytes"] = 100
        self.error("variant_enumeration_output_limit", self.backend.iter_variants)

    def test_whole_operation_time_budget(self):
        self.insert("a" * 64)
        with patch("lore_mirror.backend.time.monotonic", side_effect=[0, 100, 100]):
            self.error("variant_enumeration_budget", self.backend.iter_variants)

    def native_backend(self):
        cfg = dict(self.cfg, backend="public-inbox", extindex=str(self.root / "extindex"))
        dbpath = Path(cfg["extindex"]) / "ei15" / "over.sqlite3"
        dbpath.parent.mkdir(parents=True)
        with sqlite3.connect(dbpath) as db:
            db.executescript("CREATE TABLE xref3(docid INTEGER, ibx_id INTEGER, xnum INTEGER, oidbin BLOB);"
                             "CREATE INDEX idx_oidbin ON xref3(oidbin);"
                             "CREATE TABLE inboxes(ibx_id INTEGER PRIMARY KEY, eidx_key TEXT);"
                             "INSERT INTO inboxes VALUES(1, 'org.test');")
        return PublicInboxBackend(cfg), dbpath

    def test_native_distinct_blobs_not_deduplicated_documents(self):
        backend, path = self.native_backend()
        with sqlite3.connect(path) as db:
            db.executemany("INSERT INTO xref3 VALUES(1,1,1,?)", [(bytes.fromhex(c * 40),) for c in "bbaa"])
        items, final = self.collect(backend)
        self.assertEqual([item["blob"] for item in items], ["a" * 40, "b" * 40])
        self.assertTrue(all(item["message_id"] is None for item in items))
        self.assertTrue(all(item["list_ids"] == ["test"] for item in items))
        self.assertTrue(final["complete"])

    def test_native_same_tick_same_size_commit_dirties_pass(self):
        backend, path = self.native_backend()
        with sqlite3.connect(path) as db:
            db.executemany("INSERT INTO xref3 VALUES(1,1,1,?)", [(bytes.fromhex(c * 40),) for c in "4c"])
        size = path.stat().st_size
        with self.same_filesystem_tick():
            first = backend.iter_variants(limit=1)
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO xref3 VALUES(1,1,1,?)", (bytes.fromhex("1" * 40),))
            self.assertEqual(path.stat().st_size, size)
            result = backend.iter_variants(after=first["next_cursor"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["coverage"]["restart_required"])

    def test_native_membership_scalar_is_bounded_and_qualified(self):
        backend, path = self.native_backend()
        with sqlite3.connect(path) as db:
            db.execute("UPDATE inboxes SET eidx_key=?", ("x" * 20000,))
            db.execute("INSERT INTO xref3 VALUES(1,1,1,?)", (bytes.fromhex("a" * 40),))
        item = backend.iter_variants()["items"][0]
        self.assertTrue(item["memberships_partial"])
        self.assertEqual(item["list_ids"], [])

    def test_native_oversize_scalar_fails_with_bounded_sqlite_error(self):
        backend, path = self.native_backend()
        with sqlite3.connect(path) as db:
            db.execute("UPDATE inboxes SET eidx_key=?", ("x" * 2000000,))
            db.execute("INSERT INTO xref3 VALUES(1,1,1,?)", (bytes.fromhex("a" * 40),))
        self.error("variant_enumeration_unavailable", backend.iter_variants)

    def test_native_missing_oid_index_fails_instead_of_unbounded_sort(self):
        backend, path = self.native_backend()
        with sqlite3.connect(path) as db:
            db.execute("DROP INDEX idx_oidbin")
        self.error("variant_enumeration_unavailable", backend.iter_variants)

    def test_native_malformed_oid_does_not_claim_empty_complete(self):
        backend, path = self.native_backend()
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO xref3 VALUES(1,1,1,?)", ("wrong-type",))
        self.error("backend_protocol", backend.iter_variants)

    def test_native_multiple_layouts_fail_explicitly(self):
        backend, path = self.native_backend()
        other = path.parent.parent / "ei16" / path.name
        other.parent.mkdir()
        other.write_bytes(path.read_bytes())
        self.error("variant_enumeration_unavailable", backend.iter_variants)


class NativeVariantEnumerationTests(unittest.TestCase):
    run_native = classmethod(native.NativePublicInboxE2ETests.run_native.__func__)

    @classmethod
    def setUpClass(cls):
        native.NativePublicInboxE2ETests.setUpClass.__func__(cls)
        cls.original = (native.FIXTURE / "01.eml").read_bytes()
        cls.alternate = b"X-Enumeration-Transport: alternate-list-copy\n" + cls.original
        path = cls.root / "alternate.eml"
        path.write_bytes(cls.alternate)
        cls.run_native("perl", "-e", native.IMPORT_PERL, "devicetree-spec", str(path))
        inbox = cls.lists[1]["inbox_path"]
        cls.run_native("public-inbox-index", "-L", "basic", "--jobs=0", inbox)
        cls.run_native("public-inbox-extindex", "-L", "full", "--jobs=0", str(cls.extindex), inbox)

    def test_actual_native_inventory_preserves_all_raw_variants_readonly(self):
        before = native.hashes_under(self.root / "inboxes", self.extindex)
        backend = self.service.backend
        items, cursor = [], None
        with patch.object(backend, "search", side_effect=AssertionError("search is not enumeration")):
            for _ in range(10):
                result = backend.iter_variants(after=cursor, limit=1)
                items.extend(result["items"])
                cursor = result["next_cursor"]
                if cursor is None:
                    break
        self.assertIsNone(cursor)
        self.assertTrue(result["complete"])
        expected = {hashlib.sha256((native.FIXTURE / row["file"]).read_bytes()).hexdigest()
                    for row in native.MANIFEST["messages"]}
        expected.add(hashlib.sha256(self.alternate).hexdigest())
        actual = {hashlib.sha256(backend.raw(item)).hexdigest(): item for item in items}
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(items), 4)
        self.assertEqual(actual[hashlib.sha256(self.original).hexdigest()]["list_ids"], ["u-boot-amlogic"])
        self.assertEqual(actual[hashlib.sha256(self.alternate).hexdigest()]["list_ids"], ["devicetree-spec"])
        self.assertEqual(native.hashes_under(self.root / "inboxes", self.extindex), before)

    def test_actual_native_links_builder_preserves_citation_raw_identities(self):
        from lore_mirror.code_config import code_catalog_fingerprint
        from lore_mirror.code_links import LinksStore, sync_links_once
        from lore_mirror.code_sync import SAFE_REPO_CONFIG

        before = native.hashes_under(self.root / "inboxes", self.extindex)
        scope = tempfile.TemporaryDirectory(prefix="native-links-", dir=self.root)
        self.addCleanup(scope.cleanup)
        scope_root = Path(scope.name)
        cfg = load_config(dict(self.cfg, code={"enabled": True, "root_dir": str(scope_root / "code"),
            "trees": [{"id": "boot", "url": "https://git.u-boot-project.org/u-boot/u-boot.git",
                       "default_ref": "refs/heads/main", "refs": ["refs/heads/main"], "index_refs": ["refs/heads/main"]}],
            "links": {"enabled": True, "patch_since": "2020-01-01"}}))
        code_root = Path(cfg["code"]["root_dir"])
        work = scope_root / "citation-work"
        work.mkdir()
        self.run_native("git", "-C", str(work), "init", "-b", "main")
        self.run_native("git", "-C", str(work), "config", "user.name", "Local evidence fixture")
        self.run_native("git", "-C", str(work), "config", "user.email", "fixture@example.invalid")
        self.run_native("git", "-C", str(work), "commit", "--allow-empty", "-m",
                        "Cite posted mail\n\nLink: https://lore.kernel.org/r/" + self.mids[0])
        commit = self.run_native("git", "-C", str(work), "rev-parse", "HEAD").strip()
        repo = code_root / "repositories" / "boot.git"
        repo.parent.mkdir(parents=True)
        self.run_native("git", "clone", "--bare", str(work), str(repo))
        (repo / "config").write_text(SAFE_REPO_CONFIG)
        tree = cfg["code"]["trees"][0]
        (code_root / "code-state.json").write_text(json.dumps({"schema_version": 1, "generation": 1,
            "trees": {"boot": {"published_refs": {"refs/heads/main": commit},
                "config_fingerprint": code_catalog_fingerprint(tree), "upstream_url": tree["url"]}}}))
        result = sync_links_once(cfg, mail_backend=self.service.backend)
        self.assertEqual(result["status"], "ready", result)
        rows = LinksStore(code_root).related_mail("boot", commit)["items"]
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({row["evidence_level"] for row in rows}, {"linked"})
        expected = {hashlib.sha256(raw).hexdigest():
                    hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                    for raw in (self.original, self.alternate)}
        self.assertEqual({row["raw_sha256"]: row["source_blob"] for row in rows}, expected)
        self.assertEqual(native.hashes_under(self.root / "inboxes", self.extindex), before)


if __name__ == "__main__":
    unittest.main()
