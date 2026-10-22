"""Offline strict trailer and normalized patch-equivalence evidence tests."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from lore_mirror import code_link_evidence as ev

DIFF = b"""diff --git a/demo.c b/demo.c
index 1111111..2222222 100644
--- a/demo.c
+++ b/demo.c
@@ -1 +1 @@
-old();
+new();
"""

def mail(body=DIFF, mid="One@Example.test", headers=b""):
    return b"From: Author <a@example.test>\nMessage-ID: <" + mid.encode() + b">\nSubject: [PATCH] change\n" + headers + b"\n" + body


class TrailerEvidenceTests(unittest.TestCase):
    def test_known_final_link_and_closes_preserve_source(self):
        body = "Subject\n\nDescription\n\nLink: https://lore.kernel.org/r/One%40Example.test/\nCloses: <https://lore.kernel.org/all/One%40Example.test>\nSigned-off-by: A <a@b>\n"
        rows = ev.commit_trailer_evidence(body, {"One@Example.test"})
        self.assertEqual([r["status"] for r in rows], ["linked", "linked"])
        self.assertEqual(rows[0]["source"], "Link: https://lore.kernel.org/r/One%40Example.test/")
        self.assertEqual(rows[0]["message_id"], "One@Example.test")
        self.assertEqual(rows[1]["url"], "https://lore.kernel.org/all/One%40Example.test")

    def test_b4_msgid_link_and_maintainer_note_keep_the_citation(self):
        body = ("Subject\n\nDescription\n\nSigned-off-by: A <a@b>\n"
                "Link: https://patch.msgid.link/One%40Example.test\n[ bp: Massage commit message. ]\n"
                "Signed-off-by: B <b@c>\n")
        rows = ev.commit_trailer_evidence(body, {"One@Example.test"})
        self.assertEqual([(r["kind"], r["status"], r["message_id"]) for r in rows], [("link", "linked", "One@Example.test")])
        for url in ("https://patch.msgid.link/a/One%40Example.test", "https://patch.msgid.link.evil/One%40Example.test"):
            self.assertNotIn("message_id", ev.commit_trailer_evidence("Subject\n\nLink: " + url)[0])

    def test_non_final_quoted_subject_and_mixed_prose_rejected(self):
        for body in ("Link: https://lore.kernel.org/r/x@y", "Subject\n\n> Link: https://lore.kernel.org/r/x@y", "Subject\n\nLink: https://lore.kernel.org/r/x@y\nnot a trailer", "Subject\n\nLink: https://lore.kernel.org/r/x@y\n\nFinal prose"):
            self.assertEqual(ev.commit_trailer_evidence(body, {"x@y"}), [])

    def test_lookalike_userinfo_query_fragment_and_ambiguous_url_not_linked(self):
        urls = ["https://lore.kernel.org.evil/r/x@y", "https://lore.kernel.org@evil/r/x@y", "https://lore.kernel.org:443/r/x@y", "https://lore.kernel.org/r/x@y?x=1", "https://lore.kernel.org/r/x@y#frag", "https://lore.kernel.org/r/x@y/raw", "https://lore.kernel.org/r/x%0ay", "https://lore.kernel.org/r/x%ZZy", "https://lore.kernel.org/r/x@y https://evil.invalid"]
        for url in urls:
            row = ev.commit_trailer_evidence("Subject\n\nLink: " + url, {"x@y"})[0]
            self.assertEqual(row["status"], "candidate", url)
            self.assertNotIn("message_id", row)

    def test_encoded_edge_whitespace_never_canonicalizes_to_known_mid(self):
        for edge in ("%20", "%09", "%0A", "%0D", "%C2%A0", "%E2%80%A8"):
            for mid in (edge + "p@example.org", "p@example.org" + edge):
                row = ev.commit_trailer_evidence("Title\n\nLink: https://lore.kernel.org/r/" + mid, {"p@example.org"})[0]
                self.assertEqual(row["status"], "candidate")
                self.assertNotIn("message_id", row)
        self.assertEqual(ev.commit_trailer_evidence("Title\u2028\u2028Link: https://lore.kernel.org/r/p@example.org", {"p@example.org"}), [])

    def test_unknown_and_case_distinct_ids_not_linked(self):
        row = ev.commit_trailer_evidence("Subject\n\nLink: https://lore.kernel.org/r/X@Y", {"x@y"})[0]
        self.assertEqual(row["status"], "candidate")
        self.assertFalse(row["known_message"])
        pending = ev.commit_trailer_evidence("Subject\n\nLink: https://lore.kernel.org/r/X@Y")[0]
        self.assertEqual(pending["status"], "unverified_local")

    def test_fixes_is_commit_reference_never_mail(self):
        row = ev.commit_trailer_evidence('Subject\n\nFixes: abcdef123456 ("change")', set())[0]
        self.assertEqual(row["kind"], "fixes_commit")
        self.assertEqual(row["commit_prefix"], "abcdef123456")
        self.assertNotIn("message_id", row)
        self.assertEqual(row["status"], "candidate")

    def test_folded_url_is_not_silently_reassembled(self):
        row = ev.commit_trailer_evidence("Subject\n\nLink: https://lore.kernel.org/r/\n x@y", {"x@y"})[0]
        self.assertEqual(row["status"], "candidate")
        self.assertIn("\n x@y", row["source"])


class PatchEvidenceTests(unittest.TestCase):
    def test_mail_horizon_old_missing_invalid_dates_do_not_run_git(self):
        for headers, reason in (
            (b"Date: Tue, 20 Oct 2026 00:00:00 +0000\n", "outside_horizon"),
            (b"", "unknown_horizon"),
            (b"Date: garbage\n", "unknown_horizon"),
            (b"Date: Wed, 21 Oct 2026 00:00:00 -0000\n", "unknown_horizon"),
            (b"Date: Wed, 21 Oct 2026 00:00:00 +0000\nDate: Wed, 21 Oct 2026 00:00:00 +0000\n", "unknown_horizon"),
        ):
            raw = mail(headers=headers)
            with self.subTest(headers=headers), mock.patch.object(ev, "_git") as run:
                result = ev.mail_patch_evidence(raw, patch_since=1792540800)
                run.assert_not_called()
            self.assertIsNone(result["patch_id"])
            self.assertIn(reason, result["warnings"])
            self.assertEqual(result["horizon_status"], reason)
            self.assertEqual(result["message_id"], "One@Example.test")
            self.assertEqual(result["variant"], ev.hashlib.sha256(raw).hexdigest())

    def test_mail_horizon_inclusive_and_timezone_aware(self):
        for date in (b"Wed, 21 Oct 2026 00:00:00 +0000", b"Tue, 20 Oct 2026 20:00:00 -0400"):
            result = ev.mail_patch_evidence(mail(headers=b"Date: " + date + b"\n"), patch_since=1792540800)
            self.assertEqual(result["horizon_status"], "within_horizon")
            self.assertTrue(result["patch_id"])
            self.assertEqual(result["mail_date_epoch"], 1792540800)
        for invalid in (True, "yesterday", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                ev.mail_patch_evidence(mail(), patch_since=invalid)

    def test_identical_commit_and_mail_use_same_git_version_and_algorithm(self):
        commit = ev.commit_patch_evidence(DIFF)
        posted = ev.mail_patch_evidence(mail())
        self.assertTrue(ev.patch_equivalence(commit, posted), (commit, posted))
        self.assertEqual(commit["algorithm"], ev.PATCH_ALGORITHM)
        self.assertTrue(commit["git_version"].startswith("git version "))
        self.assertEqual(posted["message_id"], "One@Example.test")
        self.assertEqual(posted["message_ids"], ["One@Example.test"])
        self.assertNotIn("applied", json.dumps(posted))

    def test_unicode_separators_do_not_become_lf_source_lines(self):
        for separator in ("\u2028", "\u0085", "\u2029"):
            embedded = DIFF.replace(b"+new();", ("+new();" + separator + "tail").encode())
            multiline = DIFF.replace(b"@@ -1 +1 @@", b"@@ -1 +1,2 @@").replace(b"+new();", b"+new();\n+tail")
            first = ev.commit_patch_evidence(embedded)
            second = ev.commit_patch_evidence(multiline)
            self.assertIsNotNone(first["patch_id"])
            self.assertIsNotNone(second["patch_id"])
            self.assertNotEqual(first["patch_id"], second["patch_id"])
            normalized, warning = ev._normalize_patch(embedded, ev.MAX_INPUT)
            self.assertIsNone(warning)
            self.assertIn(separator.encode(), normalized)

    def test_mime_base64_and_crlf_normalize_identically(self):
        encoded = base64.encodebytes(DIFF.replace(b"\n", b"\r\n"))
        posted = ev.mail_patch_evidence(mail(encoded, headers=b"Content-Type: text/x-patch; charset=utf-8\nContent-Transfer-Encoding: base64\n"))
        self.assertTrue(ev.patch_equivalence(ev.commit_patch_evidence(DIFF), posted), posted)

    def test_multipart_one_patch_is_supported(self):
        body = b"--x\nContent-Type: text/plain\n\nDescription\n--x\nContent-Type: application/x-patch\n\n" + DIFF + b"\n--x--\n"
        posted = ev.mail_patch_evidence(mail(body, headers=b"Content-Type: multipart/mixed; boundary=x\n"))
        self.assertTrue(ev.patch_equivalence(ev.commit_patch_evidence(DIFF), posted), posted)

    def test_multiple_patch_parts_are_ambiguous(self):
        body = b"--x\nContent-Type: text/plain\n\n" + DIFF + b"\n--x\nContent-Type: text/x-patch\n\n" + DIFF + b"\n--x--\n"
        posted = ev.mail_patch_evidence(mail(body, headers=b"Content-Type: multipart/mixed; boundary=x\n"))
        self.assertIsNone(posted["patch_id"])
        self.assertIn("ambiguous_or_missing_patch_part", posted["warnings"])

    def test_merge_binary_truncated_and_prose_tail_do_not_equate(self):
        self.assertIsNone(ev.commit_patch_evidence(DIFF, parent_count=2)["patch_id"])
        for diff in (DIFF[:-8], DIFF + b"unexplained prose\n", b"diff --git a/a b/a\nGIT binary patch\nliteral 1\nx\n", b"diff --cc a\n@@@ -1 -1 +1 @@@\n", DIFF.replace(b"+new();", b"+\xff")):
            self.assertIsNone(ev.commit_patch_evidence(diff)["patch_id"], diff)

    def test_binary_prefix_or_no_actual_changes_cannot_equate(self):
        for prefix in (b"diff --cc first\n", b"Binary files a/x and b/x differ\n"):
            self.assertIsNone(ev.commit_patch_evidence(prefix + DIFF)["patch_id"])
        no_change = DIFF.replace(b"-old();\n+new();", b" old();")
        self.assertIsNone(ev.commit_patch_evidence(no_change)["patch_id"])

    def test_octet_stream_patch_attachment_uses_same_normalization(self):
        body = b"--x\nContent-Type: application/octet-stream\nContent-Disposition: attachment; filename=change.patch\n\n" + DIFF + b"\n--x--\n"
        posted = ev.mail_patch_evidence(mail(body, headers=b"Content-Type: multipart/mixed; boundary=x\n"))
        self.assertTrue(ev.patch_equivalence(ev.commit_patch_evidence(DIFF), posted))

    def test_format_patch_signature_removed_but_second_patch_not_hidden(self):
        commit = ev.commit_patch_evidence(DIFF)
        self.assertTrue(ev.patch_equivalence(commit, ev.mail_patch_evidence(mail(DIFF + b"-- \n2.39.2\n"))))
        result = ev.mail_patch_evidence(mail(DIFF + b"-- \n2.39.2\n" + DIFF))
        self.assertIsNone(result["patch_id"])

    def test_duplicate_conflicting_identity_downgrades_and_retains_ids(self):
        posted = ev.mail_patch_evidence(mail(headers=b"Message-ID: <Other@Example.test>\n"))
        self.assertIsNone(posted["patch_id"])
        self.assertEqual(posted["message_ids"], ["One@Example.test", "Other@Example.test"])

    def test_version_or_algorithm_mismatch_cannot_equate(self):
        good = ev.commit_patch_evidence(DIFF)
        for key in ("git_version", "algorithm", "patch_id"):
            bad = dict(good, **{key: "different"})
            self.assertFalse(ev.patch_equivalence(good, bad))
        self.assertFalse(ev.patch_equivalence({"patch_id": "public-inbox-id"}, {"patch_id": "public-inbox-id"}))

    def test_bounds_and_timeout_are_disclosed(self):
        self.assertIsNone(ev.commit_patch_evidence(DIFF, maximum_bytes=10)["patch_id"])
        self.assertIn("mail_input_limit", ev.mail_patch_evidence(mail(), maximum_bytes=10)["warnings"])
        result = ev.commit_patch_evidence(DIFF, timeout=1e-9)
        self.assertIsNone(result["patch_id"])
        self.assertIn("patch_id_timeout", result["warnings"])
        with self.assertRaises(ValueError):
            ev.commit_patch_evidence(DIFF, maximum_bytes=17 * 1024 * 1024)

    def test_malformed_charset_and_attached_mail_not_equated(self):
        posted = ev.mail_patch_evidence(mail(headers=b"Content-Type: text/plain; charset=unknown\n"))
        self.assertIsNone(posted["patch_id"])
        attached = b"--x\nContent-Type: message/rfc822\n\n" + mail() + b"\n--x--\n"
        self.assertIsNone(ev.mail_patch_evidence(mail(attached, headers=b"Content-Type: multipart/mixed; boundary=x\n"))["patch_id"])

    def test_fresh_real_git_commit_diff_matches_format_patch_mail(self):
        with tempfile.TemporaryDirectory() as temp:
            def git(*args):
                return subprocess.check_output(["git", "-C", temp, *args], stderr=subprocess.DEVNULL)
            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            path = Path(temp) / "demo.c"
            path.write_text("old();\n")
            git("add", ".")
            git("commit", "-qm", "base")
            path.write_text("new();\n")
            git("commit", "-qam", "change")
            diff = git("diff", "--no-ext-diff", "--no-textconv", "HEAD^", "HEAD", "--")
            raw = git("format-patch", "--stdout", "-1", "--no-signature")
            # format-patch does not necessarily add Message-ID without threading.
            raw = raw.replace(b"Subject:", b"Message-ID: <fixture@example.invalid>\nSubject:", 1)
            self.assertTrue(ev.patch_equivalence(ev.commit_patch_evidence(diff), ev.mail_patch_evidence(raw)))

    def test_subprocess_inherits_lock_fd_and_uses_no_shell(self):
        with tempfile.TemporaryFile() as lock:
            real = subprocess.Popen
            with mock.patch("lore_mirror.code_runtime.subprocess.Popen", wraps=real) as call:
                result = ev.commit_patch_evidence(DIFF, lock_fd=lock.fileno())
            self.assertIsNotNone(result["patch_id"])
            for args, kwargs in call.call_args_list:
                self.assertEqual(args[0][0], "/usr/bin/git")
                self.assertEqual(kwargs["pass_fds"], (lock.fileno(),))
                self.assertNotIn("shell", kwargs)
                self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")


if __name__ == "__main__":
    unittest.main()
