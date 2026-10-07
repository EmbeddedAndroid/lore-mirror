"""Physical inbox scope must constrain native candidates before the scan cap."""
from __future__ import annotations

import unittest

import test_native_e2e as _native
from lore_mirror.service import MirrorService


class NativeScopeTests(unittest.TestCase):
    run_native = classmethod(_native.NativePublicInboxE2ETests.run_native.__func__)

    @classmethod
    def setUpClass(cls):
        _native.NativePublicInboxE2ETests.setUpClass.__func__(cls)
        cls.old_mid = "scope-old@fixture.invalid"
        cls.new_mid = "scope-new@fixture.invalid"
        cls.old_list = "u-boot-amlogic"
        cls.new_list = "devicetree-spec"
        for group, mid, day, marker in [
            (cls.old_list, cls.old_mid, "01", "oldmatch"),
            (cls.new_list, cls.new_mid, "02", "newmatch"),
        ]:
            raw = (
                "From: Scope Fixture <scope@fixture.invalid>\n"
                "To: list@fixture.invalid\n"
                f"Subject: scopecapprobe {marker}\n"
                f"Date: {day} Jan 2026 12:00:00 +0000\n"
                f"Message-ID: <{mid}>\n"
                # The physical inbox, rather than this claimed header, defines scope.
                "List-ID: <same-unrelated-header@fixture.invalid>\n"
                "Content-Type: text/plain; charset=utf-8\n\n"
                f"Scope-cap regression {marker}.\n"
            ).encode()
            path = cls.root / f"scope-{marker}.eml"
            path.write_bytes(raw)
            cls.run_native("perl", "-e", _native.IMPORT_PERL, group, str(path))
            inbox = str(cls.root / "inboxes" / group)
            cls.run_native("public-inbox-index", "-L", "basic", "--jobs=0", inbox)
            cls.run_native("public-inbox-extindex", "-L", "full", "--jobs=0",
                           str(cls.extindex), inbox)
        cls.cfg["lists"] = [dict(item, **{"class": "old-class" if item["id"] == cls.old_list
                                        else "new-class"}) for item in cls.lists]
        cls.service = MirrorService(dict(cls.cfg, limits={"scan_limit": 1}))
        cls.two_candidate_service = MirrorService(dict(cls.cfg, limits={"scan_limit": 2}))

    def query(self, service=None, **arguments):
        return (service or self.service).invoke("mail.search", {
            "query": "s:scopecapprobe", **arguments,
        })

    def assert_ids(self, result, expected):
        self.assertEqual([row["message_id"] for row in result["items"]], expected, result)

    def test_older_physical_list_match_survives_global_scan_cap_one(self):
        # Sanity: the unscoped cap would hide the older list's only match.
        self.assert_ids(self.query(), [self.new_mid])
        result = self.query(lists=[self.old_list])
        self.assert_ids(result, [self.old_mid])
        self.assertEqual(result["coverage"]["lists"], [self.old_list])
        self.assertEqual(result["scanned_count"], 1)

    def test_scope_union_selects_either_inbox_not_their_intersection(self):
        result = self.query(service=self.two_candidate_service,
                            lists=[self.old_list, self.new_list])
        self.assert_ids(result, [self.new_mid, self.old_mid])
        self.assertEqual(result["coverage"]["lists"], sorted([self.old_list, self.new_list]))
        self.assertEqual(result["scanned_count"], 2)

    def test_scope_union_still_respects_one_candidate_cap(self):
        result = self.query(lists=[self.old_list, self.new_list])
        self.assert_ids(result, [self.new_mid])
        self.assertEqual(result["scanned_count"], 1)
        self.assertTrue(result["partial"])

    def test_class_filter_is_applied_before_candidate_cap(self):
        result = self.query(class_filter="old-class")
        self.assert_ids(result, [self.old_mid])
        self.assertEqual(result["coverage"]["lists"], [self.old_list])
        combined = self.query(lists=[self.old_list, self.new_list], class_filter="old-class")
        self.assert_ids(combined, [self.old_mid])

    def test_empty_list_class_intersection_stays_empty(self):
        result = self.query(lists=[self.new_list], class_filter="old-class")
        self.assert_ids(result, [])
        self.assertEqual(result["coverage"]["lists"], [])
        self.assertEqual(result["scanned_count"], 0)
        self.assertFalse(result["partial"])

    def test_native_or_query_semantics_are_preserved_with_scope(self):
        native_query = "s:oldmatch OR s:newmatch"
        result = self.query(query=native_query, lists=[self.old_list])
        self.assert_ids(result, [self.old_mid])
        self.assertEqual(result["query"], native_query)


if __name__ == "__main__":
    unittest.main()
