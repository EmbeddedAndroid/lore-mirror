"""Regression tests from independent runtime audit; all data is temporary/local.

Native edge checks create a fresh two-list inbox from the committed fixtures.
They do not touch the active pilot or access an upstream server.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest

from starlette.testclient import TestClient

from lore_mirror.backend import FixtureBackend, bounded_run
from lore_mirror.config import load_config
from lore_mirror.errors import MirrorError
from lore_mirror.server import create_app
from lore_mirror.service import MirrorService

FIXTURES = Path(__file__).parent / "fixtures" / "real-pilot"
MID = "20261005135406.509901-1-sanderson@brivo.com"


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lore-runtime-audit-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = load_config({
            "data_dir": str(root / "mail"), "runtime_dir": str(root / "runtime"),
            "issue_dir": str(root / "issues"), "backend": "fixture",
            "allowed_hosts": ["testserver"], "allowed_origins": ["http://testserver"],
            "lists": [{"id": "u-boot-amlogic", "class": "u-boot", "aliases": ["amlogic"]}],
        })
        FixtureBackend(self.cfg).import_paths(FIXTURES)
        self.service = MirrorService(self.cfg)
        self.client = TestClient(create_app(self.cfg), raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def rpc(self, name, args, *, ident=1, client=None):
        return (client or self.client).post("/mcp", json={
            "jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }, headers={"Accept": "application/json, text/event-stream"})

    def test_mcp_integer_inputs_are_strict(self):
        for value in (True, "1", 1.0):
            with self.subTest(value=value):
                response = self.rpc("mail.search", {"limit": value})
                result = response.json().get("result", {})
                self.assertTrue(response.status_code >= 400 or "error" in response.json()
                                or result.get("isError", False), response.text[:1000])

    def test_mcp_boolean_inputs_are_strict(self):
        for name, args in (
            ("mail.get_message", {"message_id": MID, "include_raw": "true", "limit": 4}),
            ("mail.get_series", {"message_id": MID, "all_versions": 1}),
        ):
            with self.subTest(name=name):
                response = self.rpc(name, args)
                result = response.json().get("result", {})
                self.assertTrue(response.status_code >= 400 or "error" in response.json()
                                or result.get("isError", False), response.text[:1000])

    def test_rest_rejects_falsy_nonobject_arguments(self):
        for value in (None, [], False, 0, ""):
            with self.subTest(value=value):
                response = self.client.post("/api/tools/mail.status", content=json.dumps(value))
                self.assertGreaterEqual(response.status_code, 400, response.text[:1000])
                self.assertLess(response.status_code, 500)

    def test_mcp_wire_budget_includes_jsonrpc_id(self):
        response = self.rpc("mail.status", {}, ident="x" * 64500)
        self.assertLessEqual(len(response.request.content), 65536)
        self.assertLessEqual(len(response.content), self.cfg["limits"]["response_bytes"])

    def test_error_wire_budgets_cover_rest_and_mcp(self):
        cfg = dict(self.cfg, limits=dict(self.cfg["limits"], response_bytes=8192))
        with TestClient(create_app(cfg), raise_server_exceptions=False) as client:
            args = {"lists": ["y" * 10000]}
            for response in (client.post("/api/tools/mail.search", json=args),
                             self.rpc("mail.search", args, client=client)):
                with self.subTest(url=str(response.request.url)):
                    self.assertLessEqual(len(response.content), 8192)

    def test_issue_response_limit_does_not_commit_inaccessible_report(self):
        # Each Message-ID is independently legal; all request fields fit their limits.
        mids = [str(n) + "a" * 990 + "@a.x" for n in range(20)]
        args = {"title": "Valid bounded report", "description": "x" * 12000,
                "message_ids": mids, "idempotency_key": "large-report-audit"}
        response = self.client.post("/api/issues.json", json=args)
        listed = self.client.get("/api/issues.json").json()["items"]
        if response.status_code >= 400:
            self.assertEqual(listed, [], "Rejected issue was already committed and cannot be read")
        else:
            self.assertEqual(len(listed), 1)
            fetched = self.client.post("/api/tools/issue.get", json={"issue_id": listed[0]["id"]})
            self.assertEqual(fetched.status_code, 200, fetched.text)

    def test_bad_dates_fail_as_client_errors(self):
        for field, value in (("since", 0), ("since", []), ("since", False), ("until", {})):
            with self.subTest(field=field, value=value):
                response = self.client.post("/api/tools/mail.in_flight", json={
                    "paths": ["drivers/net/designware.c"], field: value,
                })
                self.assertGreaterEqual(response.status_code, 400, response.text[:1000])
                self.assertLess(response.status_code, 500, response.text[:1000])

    def test_lone_unicode_surrogate_is_client_error(self):
        response = self.client.post("/api/tools/mail.search", content='{"query":"\\ud800"}',
                                    headers={"Content-Type": "application/json"})
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500, response.text)

    def test_subsystem_filter_type_is_validated_before_search(self):
        source = Path(self.tmp.name) / "MAINTAINERS"
        source.write_text("NETWORK\nF: drivers/net/\n")
        cfg = dict(self.cfg, maintainers_file=str(source))
        with TestClient(create_app(cfg), raise_server_exceptions=False) as client:
            for value in (1, True, ["NETWORK"], [], {}):
                with self.subTest(value=value):
                    response = client.post("/api/tools/mail.in_flight", json={
                        "subsystem": value, "paths": ["drivers/net/designware.c"],
                        "since": "2026-10-01",
                    })
                    self.assertGreaterEqual(response.status_code, 400, response.text[:500])
                    self.assertLess(response.status_code, 500, response.text[:500])

    def test_deep_json_is_client_error(self):
        raw = '{"query":' + '[' * 12000 + '0' + ']' * 12000 + '}'
        self.assertLess(len(raw), 65536)
        response = self.client.post("/api/tools/mail.search", content=raw,
                                    headers={"Content-Type": "application/json"})
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500, response.text)

    def test_documented_glob_paths_do_not_drop_fixture_candidates(self):
        exact = self.service.invoke("mail.in_flight", {
            "paths": ["drivers/net/designware.c"], "since": "2026-10-01",
        })
        glob = self.service.invoke("mail.in_flight", {
            "paths": ["drivers/net/*.c"], "since": "2026-10-01",
        })
        self.assertEqual(len(exact["items"]), 2)
        self.assertEqual({r["message_id"] for r in glob["items"]},
                         {r["message_id"] for r in exact["items"]})

    def test_decode_warning_survives_get_message(self):
        path = Path(self.tmp.name) / "unknown-charset.eml"
        path.write_bytes(b"Message-ID: <unknown-charset@audit.invalid>\n"
                         b"From: A <a@audit.invalid>\nSubject: charset fallback\n"
                         b"Content-Type: text/plain; charset=x-unknown-audit\n\nhello\n")
        FixtureBackend(self.cfg).import_paths(path)
        parsed, _, _ = self.service._message("unknown-charset@audit.invalid")
        self.assertTrue(parsed["parse_warnings"])
        result = self.service.invoke("mail.get_message", {"message_id": "unknown-charset@audit.invalid"})
        self.assertIn("unknown_charset:x-unknown-audit", result["parse_warnings"])

    def test_idempotency_payload_conflict_is_explicit(self):
        args = {"title": "First", "description": "A report", "idempotency_key": "conflict-audit"}
        first = self.service.invoke("issue.report", args)
        with self.assertRaises(MirrorError) as caught:
            self.service.invoke("issue.report", dict(args, description="A different report"))
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(self.service.invoke("issue.get", {"issue_id": first["id"]})["description"], "A report")

    def test_scope_alias_and_class_filters_agree(self):
        alias = self.service.invoke("mail.search", {"lists": ["amlogic"]})
        classified = self.service.invoke("mail.search", {"class_filter": "u-boot"})
        self.assertEqual(alias["items"], classified["items"])
        with self.assertRaises(MirrorError) as caught:
            self.service.invoke("mail.search", {"class_filter": "absent"})
        self.assertEqual(caught.exception.code, "unknown_class")


class RuntimeAuditProcessTests(unittest.TestCase):
    def test_simultaneous_input_and_both_output_pipes_make_progress(self):
        source = ("import os,sys\n"
                  "os.write(1,b'o'*65536)\n"
                  "os.write(2,b'e'*60000)\n"
                  "value=sys.stdin.buffer.read()\n"
                  "sys.stdout.buffer.write(value)\n")
        result = bounded_run([sys.executable, "-c", source], input_bytes=b"i" * 65536,
                             max_bytes=140000, timeout=3)
        self.assertEqual(result, b"o" * 65536 + b"i" * 65536)

    def test_timeout_handles_descendant_holding_pipe_after_parent_exits(self):
        source = "import os,time\nif os.fork()==0: time.sleep(20)\nelse: os._exit(0)\n"
        start = time.monotonic()
        with self.assertRaises(MirrorError) as caught:
            bounded_run([sys.executable, "-c", source], timeout=.15)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - start, 3)


class RuntimeAuditStdioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lore-stdio-audit-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "config.json"
        self.config.write_text(json.dumps({"data_dir": str(root / "mail"), "backend": "fixture"}))
        self.error = tempfile.TemporaryFile()
        self.addCleanup(self.error.close)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        self.proc = subprocess.Popen([sys.executable, "-m", "lore_mirror", "stdio",
                                      "--config", str(self.config)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.error, env=env, start_new_session=True)
        self.addCleanup(self.stop)

    def stop(self):
        if self.proc.poll() is None:
            if not self.proc.stdin.closed:
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=3)
        for stream in (self.proc.stdin, self.proc.stdout):
            if not stream.closed:
                stream.close()

    def read_frame(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            if not selector.select(8):
                self.error.seek(0)
                self.fail("No stdio reply: " + self.error.read(2000).decode("utf-8", "replace"))
        raw = self.proc.stdout.readline(65538)
        self.assertTrue(raw, "Server exited before sending a frame")
        self.assertLessEqual(len(raw), 65536)
        return json.loads(raw)

    def send(self, value):
        self.proc.stdin.write(json.dumps(value).encode() + b"\n")
        self.proc.stdin.flush()
        return self.read_frame()

    def initialize(self):
        initialized = self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "runtime-audit", "version": "1"},
        }})
        self.assertIn("protocolVersion", initialized["result"])
        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.proc.stdin.flush()

    def test_stdio_initialize_discovery_status_and_bad_id_recovery(self):
        self.initialize()
        listed = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual(len(listed["result"]["tools"]), 11)
        status = self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "mail.status", "arguments": {},
        }})
        self.assertFalse(status["result"].get("isError", False))
        self.assertTrue(status["result"]["structuredContent"]["archive_read_only"])
        self.assertFalse(status["result"]["structuredContent"]["query_network"])
        rejected = self.send({"jsonrpc": "2.0", "id": "x" * 64500,
                              "method": "tools/call", "params": {"name": "mail.status", "arguments": {}}})
        self.assertIn("error", rejected)
        self.assertIsNone(rejected.get("id"))
        recovered = self.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "mail.status", "arguments": {},
        }})
        self.assertEqual(recovered["id"], 4)
        self.assertFalse(recovered["result"].get("isError", False))
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=5), 0)

    def test_stdio_oversized_frame_is_bounded_and_exits_cleanly(self):
        self.proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"' + b"x" * 66000 + b'"}\n')
        self.proc.stdin.flush()
        result = self.read_frame()
        self.assertIn("error", result)
        self.assertIn("64 KiB", result["error"]["message"])
        self.assertEqual(self.proc.wait(timeout=5), 0)


class RuntimeAuditNativeTests(unittest.TestCase):
    def test_native_long_subject_degrades_and_globs_keep_candidates(self):
        # Reuse only the isolated setup helper, not the live pilot configuration.
        from test_native_e2e import NativePublicInboxE2ETests as Native, IMPORT_PERL
        Native.setUpClass()
        try:
            path = Native.root / "long-subject-audit.eml"
            path.write_text("Message-ID: <long-subject@audit.invalid>\n"
                            "From: A <a@audit.invalid>\nSubject: [PATCH] " + "a" * 5000 +
                            "\nDate: Wed, 07 Oct 2026 12:00:00 +0000\n\n"
                            "diff --git a/drivers/test.c b/drivers/test.c\n"
                            "--- a/drivers/test.c\n+++ b/drivers/test.c\n"
                            "@@ -1 +1 @@\n-old\n+new\n")
            Native.run_native("perl", "-e", IMPORT_PERL, "u-boot-amlogic", str(path))
            Native.run_native("public-inbox-index", "-L", "basic", "--jobs=0", str(Native.inbox))
            Native.run_native("public-inbox-extindex", "-L", "full", "--jobs=0",
                              str(Native.extindex), str(Native.inbox))
            with self.subTest(case="long subject"):
                result = Native.service.invoke("mail.get_series", {"message_id": "long-subject@audit.invalid"})
                self.assertTrue(result["items"])
                self.assertTrue(result["partial"])
                self.assertTrue(result["version_discovery"]["candidate_query_unavailable"])
            with self.subTest(case="path glob"):
                result = Native.service.invoke("mail.in_flight", {
                    "paths": ["drivers/net/*.c"], "since": "2026-10-01",
                })
                self.assertEqual(len(result["items"]), 2)
        finally:
            Native.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
