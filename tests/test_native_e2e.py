"""Offline native public-inbox replay of exact, provenance-labelled real mail.

The golden answers were captured once from an existing local read-only snapshot,
restricted to these three Message-IDs. Tests never read that snapshot, contact
lore, fetch a repository, or call the mirror sync worker. Each run imports the
committed bytes into new v2/basic inboxes and builds a full native extindex.

Missing native dependencies are an explicit unittest skip. To provision the
pinned native tools (the installer downloads software, not mail), then replay:
    scripts/bootstrap-public-inbox.sh "$PWD/.runtime"
    LORE_NATIVE_ENV="$PWD/.runtime/env.sh" scripts/test-native-e2e.sh
The dedicated replay script requires the native tests, so a skip is not success.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lore_mirror.backend import PublicInboxBackend
from lore_mirror.errors import MirrorError
from lore_mirror.service import MirrorService


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "real-pilot"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_text())
GOLDEN = json.loads((FIXTURE / "golden-queries.json").read_text())
UNKNOWN_MID = "absent-native-e2e@fixture.invalid"
BOOTSTRAP = (
    'scripts/bootstrap-public-inbox.sh "$PWD/.runtime"; '
    'LORE_NATIVE_ENV="$PWD/.runtime/env.sh" scripts/test-native-e2e.sh'
)
NATIVE_COMMANDS = ("perl", "git", "public-inbox-init", "public-inbox-index",
                   "public-inbox-extindex")

# Use upstream's ordinary archive importer, without an MDA, filter, or an
# alternate SQLite fixture backend. Limiting local $/ to the file read matters:
# fast-import itself uses line-based protocol reads. No raw-header rewriting.
IMPORT_PERL = r"""
use strict;
use warnings;
use PublicInbox::Config;
use PublicInbox::InboxWritable;
use PublicInbox::Eml;
my $ibx = PublicInbox::Config->new->lookup_name(shift @ARGV)
    or die "temporary inbox was not configured\n";
my $im = PublicInbox::InboxWritable->new($ibx)->importer(0);
for my $file (@ARGV) {
    open my $fh, '<:raw', $file or die "$file: $!";
    my $raw = do { local $/; <$fh> };
    close $fh or die "$file: $!";
    my $eml = PublicInbox::Eml->new(\$raw);
    defined($im->add($eml)) or die "duplicate fixture: $file\n";
}
$im->done;
"""


def require_native():
    missing = [name for name in NATIVE_COMMANDS if not shutil.which(name)]
    reason = "missing executables: " + ", ".join(missing) if missing else None
    if not reason:
        probe = subprocess.run(
            ["perl", "-MPublicInbox::Config", "-MPublicInbox::InboxWritable",
             "-MPublicInbox::ExtSearch", "-MDBI", "-MDBD::SQLite",
             "-MSearch::Xapian", "-e", "PublicInbox::Search::load_xapian()"],
            capture_output=True, text=True, timeout=15,
        )
        if probe.returncode:
            reason = "native Perl dependencies unavailable: " + probe.stderr[:800].strip()
    if reason:
        message = f"Native public-inbox E2E NOT RUN ({reason}). Run: {BOOTSTRAP}"
        if os.environ.get("LORE_NATIVE_E2E_REQUIRED") == "1":
            raise RuntimeError(message)
        raise unittest.SkipTest(message)


def hashes_under(*roots):
    """Check archive/index content, including added or removed files."""
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in roots for path in root.rglob("*") if path.is_file()}


class RealFixtureIntegrityTests(unittest.TestCase):
    def test_committed_raw_octets_and_golden_provenance(self):
        self.assertEqual(GOLDEN["schema_version"], 1)
        self.assertEqual(
            hashlib.sha256((FIXTURE / "manifest.json").read_bytes()).hexdigest(),
            GOLDEN["provenance"]["fixture_manifest_sha256"],
        )
        self.assertEqual(len(MANIFEST["messages"]), 3)
        for record in MANIFEST["messages"]:
            with self.subTest(file=record["file"]):
                raw = (FIXTURE / record["file"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sha256"])
                blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                self.assertEqual(blob, record["blob"])
                self.assertIn(record["message_id"], GOLDEN["search_headers"])


class NativePublicInboxE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_native()
        cls.tmp = tempfile.TemporaryDirectory(prefix="lore-native-e2e-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.env = os.environ.copy()
        # Isolate user and pilot configuration. Git may only access local files.
        for key in list(cls.env):
            if key.startswith("GIT_CONFIG_") or key in {
                "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
            }:
                del cls.env[key]
        cls.env.update({
            "PI_CONFIG": str(cls.root / "public-inbox-config"),
            "HOME": str(cls.root / "home"),
            "XDG_CONFIG_HOME": str(cls.root / "home" / "config"),
            "XDG_CACHE_HOME": str(cls.root / "home" / "cache"),
            "XDG_DATA_HOME": str(cls.root / "home" / "data"),
            "XDG_RUNTIME_DIR": str(cls.root / "run"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_COUNT": "0", "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file", "LC_ALL": "C", "TZ": "UTC",
        })
        (cls.root / "home").mkdir()
        (cls.root / "run").mkdir(mode=0o700)
        cls.lists = []
        for list_id, address in (("u-boot-amlogic", "u-boot-amlogic@groups.io"),
                                 ("devicetree-spec", "devicetree-spec@fixture.invalid")):
            inbox = cls.root / "inboxes" / list_id
            cls.run_native("public-inbox-init", "-V2", "-L", "basic", "-j", "1",
                           "--ng", "org.kernel.lore." + list_id, list_id, str(inbox),
                           "https://example.invalid/" + list_id, address)
            cls.lists.append({"id": list_id, "class": "fixture", "inbox_path": str(inbox),
                              "newsgroup": "org.kernel.lore." + list_id,
                              "aliases": [list_id]})
        cls.inbox = Path(cls.lists[0]["inbox_path"])
        cls.extindex = cls.root / "extindex"
        cls.run_native("perl", "-e", IMPORT_PERL, "u-boot-amlogic",
                       *(str(FIXTURE / record["file"]) for record in MANIFEST["messages"]))
        cls.run_native("public-inbox-index", "-L", "basic", "--jobs=0", str(cls.inbox))
        cls.run_native("public-inbox-extindex", "-L", "full", "--jobs=0",
                       str(cls.extindex), str(cls.inbox))
        cls.cfg = {
            "data_dir": str(cls.root), "runtime_dir": str(cls.root / "runtime"),
            "issue_dir": str(cls.root / "issues"), "backend": "public-inbox",
            "extindex": str(cls.extindex), "public_inbox_config": cls.env["PI_CONFIG"],
            "query_helper": str(ROOT / "scripts" / "public-inbox-query.pl"),
            "perl_bin": shutil.which("perl"), "lists": cls.lists,
            "query_env": {key: cls.env[key] for key in
                          ("PATH", "PERL5LIB", "LD_LIBRARY_PATH", "PERL_INLINE_DIRECTORY")
                          if key in cls.env},
            "allowed_hosts": ["testserver"], "allowed_origins": ["http://testserver"],
        }
        cls.service = MirrorService(cls.cfg)
        cls.mids = [record["message_id"] for record in MANIFEST["messages"]]
        cls.archive_hashes = hashes_under(cls.root / "inboxes", cls.extindex)

    @classmethod
    def run_native(cls, *argv):
        # An unavailable dependency is handled by require_native; an import or
        # indexing regression must fail rather than be silently converted to skip.
        result = subprocess.run(argv, cwd=cls.root, env=cls.env, capture_output=True,
                                text=True, timeout=60)
        if result.returncode:
            raise AssertionError(f"Native command failed: {argv!r}\n"
                                 f"stdout: {result.stdout}\nstderr: {result.stderr}")
        return result.stdout

    def setUp(self):
        self.assertIsInstance(self.service.backend, PublicInboxBackend)
        self.assertTrue(self.service.backend.native_syntax)
        # Python-side network trips are forbidden. The native helper has fixed
        # local-only arguments; this is not a claim of OS network sandboxing.
        self.network = ExitStack()
        self.addCleanup(self.network.close)
        for name in ("socket.create_connection", "socket.getaddrinfo",
                     "socket.socket.connect", "socket.socket.connect_ex"):
            self.network.enter_context(patch(name, side_effect=AssertionError("offline E2E attempted network")))

    def tearDown(self):
        self.assertEqual(hashes_under(self.root / "inboxes", self.extindex), self.archive_hashes,
                         "read tools changed the native archive or index")

    def page_all(self, name, arguments, *, limit=1, call=None):
        call = call or self.service.invoke
        rows, cursors, pages = [], set(), []
        args = dict(arguments, limit=limit)
        while True:
            page = call(name, args)
            self.assertFalse(page["partial"], page)
            self.assertLessEqual(len(page["items"]), limit)
            self.assertEqual(page["has_more"], page["next_cursor"] is not None)
            self.assertEqual(page["truncated"], page["has_more"])
            rows.extend(page["items"])
            pages.append(page)
            if not page["has_more"]:
                break
            self.assertNotIn(page["next_cursor"], cursors, "paging failed to make progress")
            cursors.add(page["next_cursor"])
            args["cursor"] = page["next_cursor"]
            self.assertLess(len(pages), 50, "unexpectedly large fixture result")
        self.assertTrue(all(p["matched_count"] == len(rows) for p in pages))
        return rows, pages

    def test_native_layout_has_no_remote_or_production_alternates(self):
        self.assertFalse((self.root / "fixtures.sqlite3").exists())
        self.assertTrue((self.inbox / "msgmap.sqlite3").is_file())
        self.assertTrue(list(self.inbox.glob("git/*.git")))
        self.assertTrue(list(self.extindex.glob("ei*/over.sqlite3")))
        self.assertTrue((self.extindex / "ALL.git").is_dir())
        for git_dir in [*self.inbox.glob("git/*.git"), self.extindex / "ALL.git"]:
            self.assertEqual(self.run_native("git", "--git-dir=" + str(git_dir), "remote"), "")
            alternates = git_dir / "objects" / "info" / "alternates"
            if alternates.exists():
                for entry in alternates.read_text().splitlines():
                    resolved = (git_dir / "objects" / entry).resolve()
                    self.assertTrue(resolved.is_relative_to(self.root), resolved)
        status = self.service.invoke("mail.status")
        self.assertTrue(status["archive_read_only"])
        self.assertFalse(status["query_network"])
        self.assertEqual(status["backend"]["backend"], "public-inbox")

    def test_native_search_matches_local_snapshot_golden_queries(self):
        for query in GOLDEN["searches"]:
            with self.subTest(query=query["name"]):
                rows, pages = self.page_all("mail.search", query["arguments"])
                self.assertEqual([row["message_id"] for row in rows], query["expected_message_ids"])
                for row in rows:
                    self.assertEqual(row, GOLDEN["search_headers"][row["message_id"]])
                self.assertTrue(pages[0]["coverage"]["native_query_syntax"])
                self.assertEqual(pages[0]["count_kind"], "exact_within_local_scope")

    def test_exact_message_raw_body_headers_and_variant_paging(self):
        for fixture, expected in zip(MANIFEST["messages"], GOLDEN["messages"], strict=True):
            with self.subTest(message_id=fixture["message_id"]):
                args = {"message_id": fixture["message_id"], "part": "raw", "limit": 1024}
                collected = bytearray()
                while True:
                    result = self.service.invoke("mail.get_message", args)
                    for key, value in expected.items():
                        self.assertEqual(result[key], value)
                    item = result["items"][0]
                    self.assertEqual(item["byte_offset"], len(collected))
                    chunk = base64.b64decode(item["raw_base64"], validate=True)
                    self.assertTrue(chunk)
                    self.assertLessEqual(len(chunk), 1024)
                    collected.extend(chunk)
                    self.assertFalse(result["partial"])
                    self.assertEqual(result["has_more"], result["next_cursor"] is not None)
                    if not result["has_more"]:
                        break
                    args["cursor"] = result["next_cursor"]
                self.assertEqual(bytes(collected), (FIXTURE / fixture["file"]).read_bytes())
                self.assertEqual(hashlib.sha256(collected).hexdigest(), fixture["sha256"])
                for variant in (fixture["blob"], fixture["sha256"]):
                    result = self.service.invoke("mail.get_message", {
                        "message_id": "<" + fixture["message_id"] + ">", "variant": variant,
                    })
                    self.assertEqual(result["raw_sha256"], fixture["sha256"])
                    self.assertIn("MDIO", result["items"][0]["text"])
                header_args = {"message_id": fixture["message_id"], "part": "headers", "limit": 1024}
                headers = []
                while True:
                    page = self.service.invoke("mail.get_message", header_args)
                    headers.append(page["items"][0]["text"])
                    if not page["has_more"]:
                        break
                    header_args["cursor"] = page["next_cursor"]
                decoded = json.loads("".join(headers))
                self.assertEqual(decoded["message-id"], ["<" + fixture["message_id"] + ">"])
                self.assertEqual(decoded["list-id"], ["<u-boot-amlogic.groups.io>"])

    def test_unknown_message_does_not_fall_back_to_entire_corpus(self):
        for name in ("mail.get_message", "mail.get_thread", "mail.get_series"):
            with self.subTest(tool=name), self.assertRaises(MirrorError) as caught:
                self.service.invoke(name, {"message_id": UNKNOWN_MID})
            self.assertEqual(caught.exception.code, "not_found")
            self.assertEqual(caught.exception.status, 404)
        result = self.service.invoke("mail.was_posted", {"message_id": UNKNOWN_MID})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["posted_result"], "no_match_in_local_scope")
        rows, partial = self.service.backend.search("", 100, thread_id=UNKNOWN_MID)
        self.assertEqual(rows, [])
        self.assertFalse(partial)

    def test_thread_pages_match_snapshot_from_every_anchor(self):
        for mid in self.mids:
            with self.subTest(anchor=mid):
                rows, pages = self.page_all("mail.get_thread", {"message_id": mid})
                self.assertEqual(rows, GOLDEN["thread"]["expected_items"])
                self.assertEqual(len(pages), 3)
                self.assertEqual(len({row["message_id"] for row in rows}), 3)
        empty = self.service.invoke("mail.get_thread", {
            "message_id": self.mids[0], "lists": ["devicetree-spec"],
        })
        self.assertEqual(empty["items"], [])

    def test_series_cover_patches_trailers_and_evidence_match_snapshot(self):
        rows, pages = self.page_all("mail.get_series", {
            "message_id": self.mids[0], "all_versions": False,
        }, limit=2)
        self.assertEqual(rows, GOLDEN["series"]["expected_items"])
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(page["applied_status"] == "unknown" for page in pages))
        cover = [row for row in rows if row["kind"] == "cover"]
        patches = [row for row in rows if row["kind"] == "patch"]
        trailers = [row for row in rows if row["kind"] == "trailer"]
        self.assertEqual([row["message_id"] for row in cover], [self.mids[0]])
        self.assertEqual([row["ordinal"] for row in patches], [1, 2])
        self.assertTrue(all(row["actually_posted"] and not row["synthesized"] for row in cover + patches))
        self.assertEqual(sum(row["name"] == "Signed-off-by" for row in trailers), 2)
        self.assertTrue(all(not row["verified_approval"] for row in trailers))
        self.assertTrue(all(row["classification"] == "embedded_claim" for row in trailers))
        # all_versions can discover only candidates, never assert global completeness.
        found = self.service.invoke("mail.get_series", {"message_id": self.mids[0]})
        self.assertFalse(found["version_discovery"]["complete"])

    def test_native_stable_patch_id_and_path_candidate_evidence(self):
        patches = [row for row in GOLDEN["series"]["expected_items"] if row["kind"] == "patch"]
        for member in patches:
            with self.subTest(patch_id=member["patch_id"]):
                result = self.service.invoke("mail.was_posted", {"patch_id": member["patch_id"]})
                self.assertEqual([row["message_id"] for row in result["items"]], [member["message_id"]])
                self.assertEqual(result["items"][0]["evidence_type"], "stable_patch_id_equivalence")
                body = self.service.invoke("mail.get_message", {"message_id": member["message_id"]})
                self.assertFalse(body["has_more"])
                by_diff = self.service.invoke("mail.was_posted", {"diff": body["items"][0]["text"]})
                self.assertEqual(by_diff["items"], result["items"])
                self.assertEqual(result["applied_status"], "unknown")
        rows, _ = self.page_all("mail.in_flight", {
            "paths": ["drivers/net/designware.c"], "since": "2026-10-05",
            "until": "2026-10-05", "lists": ["u-boot-amlogic"],
        })
        self.assertEqual({row["message_id"] for row in rows}, set(self.mids[1:]))
        self.assertTrue(all(row["applied_status"] == "unknown" for row in rows))
        self.assertTrue(all("drivers/net/designware.c" in row["paths"] for row in rows))

    def test_official_mcp_reads_the_fresh_native_index_in_process(self):
        from starlette.testclient import TestClient
        from lore_mirror.server import create_app

        with TestClient(create_app(self.cfg)) as client:
            def rpc(method, params):
                response = client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
                }, headers={"Accept": "application/json, text/event-stream"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertLess(len(response.content), self.service.limits["response_bytes"])
                body = response.json()
                self.assertNotIn("error", body, body)
                return body["result"]

            def call(name, arguments):
                result = rpc("tools/call", {"name": name, "arguments": arguments})
                self.assertFalse(result.get("isError", False), result)
                structured = result["structuredContent"]
                self.assertEqual(json.loads(result["content"][0]["text"]), structured)
                return structured

            initialized = rpc("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "offline-native-e2e", "version": "1"},
            })
            self.assertIn("protocolVersion", initialized)
            listed = rpc("tools/list", {})["tools"]
            self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in listed
                                if t["name"].startswith("mail.")))
            status = call("mail.status", {})
            self.assertEqual(status["backend"]["backend"], "public-inbox")
            rows, _ = self.page_all("mail.search", {"query": "f:sanderson@brivo.com"}, call=call)
            self.assertEqual([row["message_id"] for row in rows], list(reversed(self.mids)))
            rows, _ = self.page_all("mail.get_thread", {"message_id": self.mids[0]}, call=call)
            self.assertEqual(rows, GOLDEN["thread"]["expected_items"])
            rows, _ = self.page_all("mail.get_series", {
                "message_id": self.mids[0], "all_versions": False,
            }, limit=2, call=call)
            self.assertEqual(rows, GOLDEN["series"]["expected_items"])
            message = call("mail.get_message", {"message_id": self.mids[1], "part": "raw", "limit": 1024})
            self.assertEqual(message["raw_sha256"], MANIFEST["messages"][1]["sha256"])
            self.assertEqual(base64.b64decode(message["items"][0]["raw_base64"]),
                             (FIXTURE / "02.eml").read_bytes()[:1024])
            missing = rpc("tools/call", {"name": "mail.get_thread", "arguments": {"message_id": UNKNOWN_MID}})
            self.assertTrue(missing["isError"])


if __name__ == "__main__":
    unittest.main()
