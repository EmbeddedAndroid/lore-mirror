"""Reads during index writes: transient commit races are retried, then
reported as index_busy; other backend failures are not retried."""
import unittest
from unittest.mock import patch

from lore_mirror.backend import PublicInboxBackend, TRANSIENT_ATTEMPTS
from lore_mirror.errors import MirrorError


def backend():
    b = object.__new__(PublicInboxBackend)
    b.limits = {"query_timeout": 5, "query_output_bytes": 1024}
    b.env = {}
    return b


def failing(messages, result=b"ok"):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if len(calls) <= len(messages):
            raise MirrorError("backend_error", messages[len(calls) - 1], 503)
        return result
    return run, calls


class ReadDuringIndexTests(unittest.TestCase):
    def test_commit_race_is_retried(self):
        for message in ("Local backend exited 255: Xapian::DatabaseModifiedError: The revision being read has been discarded",
                        "Local backend exited 255: DBD::SQLite::db prepare failed: database is locked"):
            with self.subTest(message=message):
                run, calls = failing([message, message])
                with patch("lore_mirror.backend.bounded_run", side_effect=run), patch("time.sleep"):
                    self.assertEqual(backend()._run(["helper"]), b"ok")
                self.assertEqual(len(calls), 3)

    def test_persistent_commit_race_is_index_busy(self):
        message = "Local backend exited 255: Xapian::DatabaseModifiedError"
        run, calls = failing([message] * TRANSIENT_ATTEMPTS)
        with patch("lore_mirror.backend.bounded_run", side_effect=run), patch("time.sleep"):
            with self.assertRaises(MirrorError) as ctx:
                backend()._run(["helper"])
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("index_busy", 503))
        self.assertEqual(len(calls), TRANSIENT_ATTEMPTS)

    def test_other_backend_failures_are_not_retried(self):
        for code, message in (("backend_error", "Local backend exited 2: invalid query"),
                              ("timeout", "Local query exceeded its time budget")):
            with self.subTest(code=code):
                calls = []

                def run(*args, **kwargs):
                    calls.append(args)
                    raise MirrorError(code, message, 503)
                with patch("lore_mirror.backend.bounded_run", side_effect=run), patch("time.sleep"):
                    with self.assertRaises(MirrorError) as ctx:
                        backend()._run(["helper"])
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
