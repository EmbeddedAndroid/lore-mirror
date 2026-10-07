"""Offline regression fixtures for mail/patch evidence boundaries."""
import hashlib
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from lore_mirror import semantics as sem


DIFF = """diff --git a/drivers/demo.c b/drivers/demo.c
index 1111111..2222222 100644
--- a/drivers/demo.c
+++ b/drivers/demo.c
@@ -1 +1 @@
-old();
+new();
"""


def mail(mid="one@example.test", subject="[PATCH] demo: fix thing", body=DIFF,
         sender="Author <author@example.test>", parent=None, extra=""):
    headers = f"From: {sender}\nSubject: {subject}\nDate: Wed, 7 Oct 2026 11:00:00 +0200\n"
    if mid is not None:
        headers += f"Message-ID: <{mid}>\n"
    if parent:
        headers += f"In-Reply-To: <{parent}>\nReferences: <{parent}>\n"
    return (headers + extra + "\n" + body).encode()


class MessageTests(unittest.TestCase):
    def test_exact_id_case_is_preserved(self):
        self.assertEqual(sem.normalize_message_id(" <X@Example.test> "), "X@Example.test")
        for bad in ("", "<>", "a b", "<a> <b>", "x\r\nSubject:y", "<x", "a\x00b", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sem.normalize_message_id(bad)

    def test_missing_and_conflicting_ids_are_not_invented(self):
        self.assertIsNone(sem.parse_message(mail(mid=None))["message_id"])
        parsed = sem.parse_message(mail(extra="Message-ID: <other@example.test>\n"))
        self.assertIsNone(parsed["message_id"])
        self.assertIn("conflicting_message_id_headers", parsed["parse_warnings"])

    def test_variants_hash_all_octets_and_membership_stays_separate(self):
        raw = mail(extra="List-Id: <claimed.example>\nX-Test: one\nX-Test: two\n")
        first = sem.parse_message(raw, ["physical-b", "physical-a", "physical-a"])
        second = sem.parse_message(raw.replace(b"X-Test: one", b"X-Test: changed"))
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertNotEqual(first["variant"], second["variant"])
        self.assertEqual(first["variant"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(first["headers"]["x-test"], ["one", "two"])
        self.assertEqual(first["list_ids"], ["physical-a", "physical-b"])
        self.assertEqual(first["list_id_headers"], ["<claimed.example>"])
        self.assertEqual(first["date"], "2026-10-07T09:00:00+00:00")
        self.assertEqual(first["applied_status"], "unknown")

    def test_raw_header_folding_is_preserved_separately(self):
        raw = mail(extra="X-Folded: first\n second\n")
        parsed = sem.parse_message(raw)
        self.assertIn({"name": "X-Folded", "value": "first\n second"}, parsed["raw_headers"])
        self.assertEqual(parsed["body"], DIFF)

    def test_unknown_charset_and_malformed_mime_are_reported(self):
        raw = mail(body="hello", extra="Content-Type: text/plain; charset=not-a-real-charset\n")
        parsed = sem.parse_message(raw)
        self.assertEqual(parsed["body"], "hello")
        self.assertIn("unknown_charset:not-a-real-charset", parsed["parse_warnings"])
        malformed = sem.parse_message(mail(body="not a multipart body", extra="Content-Type: multipart/mixed; boundary=missing\n"))
        self.assertIn("StartBoundaryNotFoundDefect", malformed["parse_warnings"])
        json.dumps(malformed)

    def test_multipart_patch_and_html_are_preserved_without_execution(self):
        body = "--x\nContent-Type: text/plain\n\nExplanation\n--x\nContent-Type: text/x-patch\nContent-Disposition: attachment; filename=change.patch\n\n" + DIFF + "\n--x\nContent-Type: text/html\n\n<script>bad()</script>\n--x--\n"
        parsed = sem.parse_message(mail(body=body, extra="Content-Type: multipart/mixed; boundary=x\n"))
        self.assertTrue(parsed["patch"])
        self.assertEqual(len(parsed["textparts"]), 3)
        self.assertNotIn("<script>", parsed["body"])
        self.assertIn("<script>", parsed["textparts"][2]["text"])

    def test_attached_message_is_not_sender_patch(self):
        body = "--x\nContent-Type: text/plain\n\nSee attached\n--x\nContent-Type: message/rfc822\n\n" + mail().decode() + "\n--x--\n"
        parsed = sem.parse_message(mail(body=body, extra="Content-Type: multipart/mixed; boundary=x\n"))
        self.assertFalse(parsed["patch"])
        self.assertIn("attached_message_excluded_from_body", parsed["parse_warnings"])

    def test_raw_and_part_bounds(self):
        with self.assertRaises(ValueError):
            sem.parse_message(b"x" * (sem.MAX_RAW_BYTES + 1))
        with patch.object(sem, "MAX_PARTS", 2), self.assertRaises(ValueError):
            sem.parse_message(mail(body="--x\nContent-Type: text/plain\n\na\n--x\nContent-Type: text/plain\n\nb\n--x--", extra="Content-Type: multipart/mixed; boundary=x\n"))

    def test_received_date_and_unknown_timezone(self):
        parsed = sem.parse_message(mail(extra="Received: from x; Wed, 7 Oct 2026 08:00:00 -0100\n"))
        self.assertEqual(parsed["received_date"], "2026-10-07T09:00:00+00:00")
        self.assertIsNone(sem._date("Wed, 7 Oct 2026 09:00:00 -0000"))

    def test_subject_and_trailer_bounds_are_explicit(self):
        with self.assertRaises(ValueError):
            sem.parse_subject("x" * (sem.MAX_SUBJECT_CHARS + 1))
        with patch.object(sem, "MAX_TRAILERS", 1):
            parsed = sem.parse_message(mail(body="Reviewed-by: A <a@example.test>\nTested-by: B <b@example.test>\n"))
        self.assertTrue(parsed["trailers_truncated"])
        self.assertEqual(len(parsed["trailers"]), 1)

    def test_application_patch_attachment_is_included(self):
        parsed = sem.parse_message(mail(extra="Content-Type: application/octet-stream\nContent-Disposition: attachment; filename=fix.patch\n"))
        self.assertTrue(parsed["patch"])
        self.assertEqual(parsed["paths"], ["drivers/demo.c"])

    def test_git_binary_patch_is_retained_as_patch(self):
        body = "diff --git a/logo.bin b/logo.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\nliteral 1\nIc${MZ000310RR91\n\nliteral 0\nHcmV?d00001\n"
        parsed = sem.parse_message(mail(body=body))
        self.assertTrue(parsed["patch"])
        self.assertEqual(parsed["body"], body)
        self.assertEqual(parsed["paths"], ["logo.bin"])

    def test_quoted_diff_does_not_make_patch(self):
        parsed = sem.parse_message(mail(subject="Re: [PATCH] demo", body="\n".join("> " + line for line in DIFF.splitlines())))
        self.assertFalse(parsed["patch"])
        self.assertEqual(parsed["paths"], [])

    def test_trailer_provenance_never_becomes_approval(self):
        body = "> Reviewed-by: Prior <prior@example.test>\n\nTested-by: Other <other@example.test>\n\n\nI withdraw this review.\nReviewed-by: Reviewer <reviewer@example.test>\n"
        parsed = sem.parse_message(mail(subject="Re: [PATCH v2] demo", body=body, sender="Reviewer <reviewer@example.test>", parent="patch@example.test"))
        trailers = parsed["trailers"]
        self.assertEqual(trailers[0]["classification"], "quoted")
        self.assertTrue(trailers[1]["copied_or_embedded"])
        self.assertEqual(trailers[2]["classification"], "retraction_context")
        self.assertEqual(trailers[2]["source_message_id"], "one@example.test")
        self.assertEqual(trailers[2]["line"], 7)
        self.assertEqual(trailers[2]["target_message_ids"], ["patch@example.test"])
        self.assertTrue(all(not t["verified_approval"] for t in trailers))

    def test_embedded_trailer_is_not_new_reviewer_mail(self):
        parsed = sem.parse_message(mail(body="Reviewed-by: Reviewer <reviewer@example.test>\n\n---\n" + DIFF))
        self.assertEqual(parsed["trailers"][0]["classification"], "embedded_claim")
        self.assertEqual(parsed["trailers"][0]["scope"], "patch")


class PatchTests(unittest.TestCase):
    def test_rename_copy_and_c_quoted_paths(self):
        body = 'diff --git a/old.c b/new.c\nsimilarity index 100%\nrename from old.c\nrename to new.c\n' + 'diff --git "a/odd\\040name.c" "b/odd\\040name.c"\n--- "a/odd\\040name.c"\n+++ "b/odd\\040name.c"\n@@ -1 +1 @@\n-a\n+b\n'
        self.assertEqual(sem.extract_diff_paths(body), ["new.c", "odd name.c", "old.c"])
        unicode = 'diff --git "a/caf\\303\\251.c" "b/caf\\303\\251.c"\n'
        self.assertEqual(sem.extract_diff_paths(unicode), ["café.c"])

    def test_diff_body_lines_cannot_forge_paths(self):
        body = DIFF + "--- fake.c\n+++ fake-too.c\n"
        self.assertEqual(sem.extract_diff_paths(body), ["drivers/demo.c"])

    def test_deletion_excludes_dev_null(self):
        body = "diff --git a/old.c b/old.c\n--- a/old.c\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        self.assertEqual(sem.extract_diff_paths(body), ["old.c"])

    def test_ambiguous_space_paths_use_authoritative_headers(self):
        body = "diff --git a/old b/name b/new b/name\nrename from old b/name\nrename to new b/name\n"
        self.assertEqual(sem.extract_diff_paths(body), ["new b/name", "old b/name"])

    def test_multiple_traditional_unified_files(self):
        body = "--- a/one\n+++ b/one\n@@ -1 +1 @@\n-old\n+new\n--- a/two\n+++ b/two\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(sem.extract_diff_paths(body), ["one", "two"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for patch-id equivalence")
    def test_git_patch_id_ignores_line_numbers_and_whitespace(self):
        altered = DIFF.replace("@@ -1 +1 @@", "@@ -50 +50 @@").replace("+new();", "+new ( );")
        self.assertIsNotNone(sem.stable_patch_id(DIFF))
        self.assertEqual(sem.stable_patch_id(DIFF), sem.stable_patch_id(altered))
        self.assertNotEqual(sem.stable_patch_id(DIFF), sem.stable_patch_id(DIFF.replace("+new();", "+other();")))

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_patch_id_stable_across_file_order(self):
        other = DIFF.replace("demo.c", "other.c").replace("new();", "other();")
        self.assertEqual(sem.stable_patch_id(DIFF + other), sem.stable_patch_id(other + DIFF))

    def test_git_is_fixed_readonly_and_failure_is_unknown(self):
        with patch.object(sem.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)) as runner:
            self.assertIsNone(sem.stable_patch_id(DIFF))
        args, kwargs = runner.call_args
        self.assertEqual(args[0][1:], ["--no-pager", "--no-replace-objects", "patch-id", "--stable"])
        self.assertEqual(kwargs["cwd"], "/")
        self.assertEqual(kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertNotIn("shell", kwargs)
        with patch.object(sem.shutil, "which", return_value=None):
            self.assertIsNone(sem.stable_patch_id(DIFF))

    def test_path_and_maintainers_matching(self):
        message = {"paths": ["drivers/net/a.c", "drivers/net/internal/b.c", "include/a.h"]}
        self.assertTrue(sem.match_paths(message, ["drivers/net/"]))
        self.assertTrue(sem.match_paths(message, ["include/*.h"]))
        self.assertFalse(sem.match_paths({"paths": ["drivers/net/internal/b.c"]}, ["drivers/net/*.c"]))
        self.assertTrue(sem.match_paths(message, ["drivers/**/*.c"]))
        text = "NETWORK\nM: Maintainer <m@example.test>\nF: drivers/net/\nX: drivers/net/internal/\nK: (a+)+$\n\nHEADERS\nF: include/*.h\n\n"
        matches = sem.classify_subsystems(message, text)
        self.assertEqual([item["name"] for item in matches], ["NETWORK", "HEADERS"])
        self.assertEqual(matches[0]["matched_paths"], ["drivers/net/a.c"])
        self.assertEqual(matches[0]["unevaluated_regex_rules"], {"K": ["(a+)+$"]})


class SeriesTests(unittest.TestCase):
    def test_unknown_anchor_is_error_not_global_search(self):
        with self.assertRaisesRegex(ValueError, "Unknown anchor"):
            sem.summarize_series([sem.parse_message(mail())], "missing@example.test")

    def test_subject_facts_and_query_escaping(self):
        subject = sem.parse_subject("Re: [RFC PATCH v3 00/02] networking: improve")
        self.assertEqual((subject["version"], subject["ordinal"], subject["total"]), (3, 0, 2))
        self.assertTrue(subject["is_cover"])
        self.assertTrue(subject["is_reply"])
        query = sem.series_candidates_query({"subject": '[PATCH] a "quoted" fix', "from_email": "a@example.test"})
        self.assertEqual(query, 's:"a \\"quoted\\" fix" AND f:"a@example.test"')
        with self.assertRaises(ValueError):
            sem.series_candidates_query({"subject": "", "from_email": ""})

    @unittest.skipUnless(sem._lore_subject is not None, "optional liblore is not installed")
    def test_liblore_subject_and_trailer_reuse(self):
        parsed = sem.parse_message(mail(subject="[PATCHv3 1/2] fix", body="Reviewed-by: A <a@example.test>\n\n" + DIFF))
        self.assertEqual(parsed["subject_info"]["version"], 3)
        self.assertEqual(parsed["subject_info"]["parser"], "liblore 0.10.1")
        self.assertEqual(parsed["trailers"][0]["trailer_type"], "person")
        self.assertFalse(parsed["trailers"][0]["verified_approval"])

    def test_missing_cover_and_partial_reroll_are_not_synthesized(self):
        patch1 = sem.parse_message(mail(mid="p1@example.test", subject="[PATCH v2 1/3] one", parent="missing-cover@example.test"))
        patch3 = sem.parse_message(mail(mid="p3@example.test", subject="[PATCH v2 3/3] three", parent="missing-cover@example.test"))
        summary = sem.summarize_series([patch1, patch3], "p1@example.test")
        version = summary["versions"][0]
        self.assertEqual(version["observed_patch_count"], 2)
        self.assertEqual(version["missing_indices"], [2])
        self.assertTrue(version["missing_cover"])
        self.assertEqual(version["partial_reroll"], "possible")
        self.assertFalse(version["synthesized"])
        self.assertTrue(all(m["actually_posted"] for m in version["messages"]))

    def test_detached_ambiguous_subjects_are_candidate_versions(self):
        first = sem.parse_message(mail(mid="v1@example.test", subject="[PATCH] fix bug"))
        second = sem.parse_message(mail(mid="v2@example.test", subject="[PATCH v2] fix bug", body=DIFF.replace("new();", "other();")))
        unrelated = sem.parse_message(mail(mid="unrelated@example.test", subject="[PATCH] fix bug", body=DIFF.replace("new();", "different();")))
        summary = sem.summarize_series([first, second, unrelated], "v1@example.test")
        self.assertEqual(len(summary["versions"]), 1)
        self.assertEqual(len(summary["candidate_versions"]), 2)
        self.assertTrue(all(link["heuristic"] and link["series_identity"] == "candidate_only" for link in summary["version_links"]))
        self.assertEqual(summary["applied_status"], "unknown")

    def test_all_versions_false_excludes_candidates(self):
        first = sem.parse_message(mail(mid="v1@example.test"))
        second = sem.parse_message(mail(mid="v2@example.test", subject="[PATCH v2] demo: fix thing"))
        self.assertEqual(sem.summarize_series([first, second], "v1@example.test", False)["candidate_versions"], [])

    def test_linked_versions_preserve_actual_posts_and_evidence(self):
        one = sem.parse_message(mail(mid="v1@example.test", subject="[PATCH 0/2] feature", body="Cover"))
        two = sem.parse_message(mail(mid="v2@example.test", subject="[PATCH v2 0/2] feature", body="Partial reroll", parent="v1@example.test"))
        patch2 = sem.parse_message(mail(mid="v2p2@example.test", subject="[PATCH v2 2/2] change", parent="v2@example.test"))
        result = sem.summarize_series([one, two, patch2], "v2@example.test")
        self.assertEqual([v["version"] for v in result["versions"]], [1, 2])
        second = result["versions"][1]
        self.assertEqual(second["missing_indices"], [1])
        self.assertEqual(second["partial_reroll"], "declared")
        self.assertTrue(any(link["kind"] == "explicit_thread_reference" for link in result["version_links"]))

    def test_variants_and_duplicate_counters_are_visible(self):
        first = sem.parse_message(mail(mid="same@example.test", subject="[PATCH 1/1] fix"))
        variant = sem.parse_message(mail(mid="same@example.test", subject="[PATCH 1/1] fix", extra="X-Variant: yes\n"))
        other = sem.parse_message(mail(mid="other@example.test", subject="[PATCH 1/1] fix", parent="same@example.test"))
        result = sem.summarize_series([first, variant, other], "same@example.test")
        self.assertEqual(len(result["variant_conflicts"]["same@example.test"]), 2)
        self.assertEqual(result["versions"][0]["count_status"], "conflicting")

    def test_missing_diff_does_not_count_as_observed_patch(self):
        message = sem.parse_message(mail(subject="[PATCH 1/1] broken", body="Sorry, attachment missing"))
        summary = sem.summarize_series([message], message["message_id"])["versions"][0]
        self.assertEqual(summary["observed_patch_count"], 0)
        self.assertEqual(summary["missing_indices"], [1])
        self.assertEqual(summary["missing_patch_payload_ids"], [message["message_id"]])

    def test_posting_signals_do_not_imply_application(self):
        message = sem.parse_message(mail())
        candidate = sem.posting_evidence(message, subject=message["subject"], author=message["from_email"])
        self.assertEqual(candidate["was_posted"], "candidate_only")
        exact = sem.posting_evidence(message, message_id="<one@example.test>")
        self.assertEqual(exact["was_posted"], "local_archive_evidence")
        self.assertEqual(exact["applied_status"], "unknown")
        self.assertEqual(sem.posting_evidence(message)["was_posted"], "unknown")


if __name__ == "__main__":
    unittest.main()
