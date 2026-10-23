"""Adversarial exact-ID evidence checks using fresh, synthetic native indexes."""
from __future__ import annotations

import base64
import hashlib
import unittest
from unittest.mock import patch

from tests import test_native_e2e as _native
from lore_mirror.errors import MirrorError
from lore_mirror.semantics import message_header_ids, parse_message


class MessageHeaderIdentityReviewTests(unittest.TestCase):
    def test_nested_and_escaped_comments_do_not_contribute_ids(self):
        cases = [
            "<real@fixture.invalid> (outer (nested <spoof@fixture.invalid>))",
            r"<real@fixture.invalid> (escaped \) <spoof@fixture.invalid>)",
            r"<real@fixture.invalid> (escaped \( <spoof@fixture.invalid>)",
            "(prefix <spoof@fixture.invalid>) <real@fixture.invalid> (suffix)",
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(message_header_ids([value]), ["real@fixture.invalid"])
                parsed = parse_message(f"Message-ID: {value}\n\nbody\n".encode())
                self.assertEqual(parsed["message_ids"], ["real@fixture.invalid"])

    def test_extraneous_quoted_angle_text_and_malformed_fields_fail_closed(self):
        for value in [
            '"<spoof@fixture.invalid>" <real@fixture.invalid>',
            '<real@fixture.invalid> "<spoof@fixture.invalid>"',
            '"<spoof@fixture.invalid>"',
            "<real@fixture.invalid> <spoof@fixture.invalid>",
            "<real@fixture.invalid> (unclosed comment",
            '<real@fixture.invalid> "unclosed quote',
        ]:
            with self.subTest(value=value):
                self.assertEqual(message_header_ids([value]), [])

    def test_quoted_local_part_and_separate_fields_preserve_exact_identity(self):
        self.assertEqual(message_header_ids(['<"quoted(paren)"@fixture.invalid>']),
                         ['"quoted(paren)"@fixture.invalid'])
        self.assertEqual(message_header_ids(["<one@fixture.invalid>",
                                              "<two@fixture.invalid>",
                                              "<one@fixture.invalid>"]),
                         ["one@fixture.invalid", "two@fixture.invalid"])


class NativeIdentityReviewTests(unittest.TestCase):
    run_native = classmethod(_native.NativePublicInboxE2ETests.run_native.__func__)

    @classmethod
    def setUpClass(cls):
        _native.NativePublicInboxE2ETests.setUpClass.__func__(cls)
        cls.canonical = "review-canonical@fixture.invalid"
        cls.alias = "review-secondary@fixture.invalid"
        cls.xalt = "review-xalt-only@fixture.invalid"
        cls.comment_only = "review-comment-only@fixture.invalid"
        cls.reference_only = "review-reference-only@fixture.invalid"
        cls.long_id = "l" * 260 + "@fixture.invalid"
        cls.dedup_primary = "review-dedup-primary@fixture.invalid"
        cls.dedup_secondary = "review-dedup-secondary@fixture.invalid"

        def raw(mid, body, extra=""):
            return (
                "From: Review Fixture <review@fixture.invalid>\n"
                "To: list@fixture.invalid\n"
                "Subject: [PATCH] Exact identity review\n"
                "Date: Thu, 01 Jan 2026 12:00:00 +0000\n"
                f"Message-ID: <{mid}>\n"
                f"{extra}"
                "Content-Type: text/plain; charset=utf-8\n\n"
                f"{body}\n"
            ).encode()

        base = raw(cls.canonical, "Same body for a transport alias")
        cls.alias_raw = raw(cls.canonical, "Same body for a transport alias",
                            f"Message-ID: <{cls.alias}>\n")
        cls.alias_blob = hashlib.sha1(b"blob " + str(len(cls.alias_raw)).encode()
                                      + b"\0" + cls.alias_raw).hexdigest()
        records = {
            "u-boot-amlogic": [
                base,
                raw(cls.alias, "Independent body with its own primary ID"),
                raw(cls.dedup_primary, "Same body across mismatched primary IDs",
                    f"Message-ID: <{cls.dedup_secondary}>\n"),
            ],
            "devicetree-spec": [
                cls.alias_raw,
                raw("review-xalt-primary@fixture.invalid", "X-Alt evidence",
                    f"X-Alt-Message-ID: <{cls.xalt}>\n"),
                raw(cls.long_id, "Long ID evidence"),
                raw("review-referrer@fixture.invalid", "Reference-only evidence",
                    f"References: <{cls.reference_only}>\n"),
                raw(cls.dedup_secondary, "Same body across mismatched primary IDs"),
                raw("review-comment-primary@fixture.invalid", "Comment-only evidence").replace(
                    b"<review-comment-primary@fixture.invalid>\n",
                    f"<review-comment-primary@fixture.invalid> (comment <{cls.comment_only}>)\n".encode()),
            ],
        }
        for group, messages in records.items():
            paths = []
            for index, source in enumerate(messages):
                path = cls.root / f"review-{group}-{index}.eml"
                path.write_bytes(source)
                paths.append(str(path))
            cls.run_native("perl", "-e", _native.IMPORT_PERL, group, *paths)
            inbox = str(cls.root / "inboxes" / group)
            cls.run_native("public-inbox-index", "-L", "basic", "--jobs=0", inbox)
            cls.run_native("public-inbox-extindex", "-L", "full", "--jobs=0",
                           str(cls.extindex), inbox)

    def test_primary_match_does_not_hide_scoped_alias_posting(self):
        result = self.service.invoke("mail.was_posted", {
            "message_id": self.alias, "lists": ["devicetree-spec"],
        })
        self.assertEqual(result["posted_result"], "matches_found", result)
        self.assertIn(self.alias_blob, [row["variant"] for row in result["items"]])

    def test_primary_match_does_not_hide_readable_alias_variant(self):
        result = self.service.invoke("mail.get_message", {
            "message_id": self.alias, "variant": self.alias_blob, "part": "raw",
        })
        self.assertEqual(result["raw_sha256"], hashlib.sha256(self.alias_raw).hexdigest())

    def test_alias_raw_continuation_keeps_exact_variant_and_bytes(self):
        arguments = {"message_id": self.alias, "variant": self.alias_blob,
                     "part": "raw", "limit": 128}
        content = bytearray()
        while True:
            result = self.service.invoke("mail.get_message", arguments)
            self.assertEqual(result["items"][0]["byte_offset"], len(content))
            content.extend(base64.b64decode(result["items"][0]["raw_base64"]))
            self.assertIn(self.alias_blob, result["variants"])
            if not result["has_more"]:
                break
            self.assertLess(len(content), len(self.alias_raw))
            arguments["cursor"] = result["next_cursor"]
        self.assertEqual(bytes(content), self.alias_raw)

    def test_mixed_alias_thread_continuations_preserve_scope_and_provenance(self):
        arguments = {"message_id": self.alias, "limit": 1}
        first = result = self.service.invoke("mail.get_thread", arguments)
        items = list(result["items"])
        while result["has_more"]:
            arguments["cursor"] = result["next_cursor"]
            result = self.service.invoke("mail.get_thread", arguments)
            self.assertEqual(result["resolved_thread_anchors"], first["resolved_thread_anchors"])
            self.assertEqual(result["partial"], first["partial"])
            items.extend(result["items"])
            self.assertLess(len(items), 20)
        self.assertEqual(len(items), len({(row["message_id"], row["variant"]) for row in items}))
        self.assertIn(self.canonical, {row["message_id"] for row in items})
        self.assertIn(self.alias, {row["message_id"] for row in items})
        self.assertTrue(first["partial"])

    def test_mixed_alias_thread_uses_anchor_in_requested_scope(self):
        # Force the formerly direct-only canonical candidate first even after
        # get() is fixed to union all aliases. This isolates thread selection.
        rows = self.service.backend.get(self.alias)
        if hasattr(self.service.backend, "_lookup_alias"):
            rows += self.service.backend._lookup_alias(self.alias)
        rows.sort(key=lambda row: "devicetree-spec" in row.get("list_ids", []))
        with patch.object(self.service.backend, "get", return_value=rows):
            result = self.service.invoke("mail.get_thread", {
                "message_id": self.alias, "lists": ["devicetree-spec"],
            })
        self.assertTrue(result["items"], result)
        self.assertTrue(all("devicetree-spec" in row["list_ids"]
                            for row in result["items"]))

    def assert_no_exact_posting(self, mid, *, lists=None):
        try:
            result = self.service.invoke("mail.was_posted", {
                "message_id": mid, **({"lists": lists} if lists else {}),
            })
        except MirrorError as exc:
            self.assertIn(exc.code, {"identity_mismatch", "identity_unverified", "not_found"})
            return
        exact = {"exact_message_id", "exact_archive_message_id_alias"}
        self.assertFalse(any(row["evidence_type"] in exact for row in result["items"]),
                         result)

    def test_xalt_header_is_not_exact_raw_message_id_evidence(self):
        self.assert_no_exact_posting(self.xalt)

    def test_rfc_comment_is_not_exact_raw_message_id_evidence(self):
        self.assert_no_exact_posting(self.comment_only)

    def test_native_truncated_id_is_not_exact_raw_message_id_evidence(self):
        self.assert_no_exact_posting(self.long_id[:244])

    def test_reference_only_id_does_not_become_posting_evidence(self):
        self.assert_no_exact_posting(self.reference_only)
        with self.assertRaises(MirrorError) as error:
            self.service.invoke("mail.get_thread", {"message_id": self.reference_only})
        self.assertEqual(error.exception.code, "not_found")

    def test_deduplicated_blob_cannot_claim_missing_primary_id_in_other_list(self):
        self.assert_no_exact_posting(self.dedup_primary, lists=["devicetree-spec"])


if __name__ == "__main__":
    unittest.main()
