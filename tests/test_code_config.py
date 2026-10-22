"""Offline catalog validation: no remote repository is contacted by these tests."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lore_mirror.code_config import normalize_code_config, validate_code_ref
from lore_mirror.errors import MirrorError

ROOT = Path(__file__).resolve().parents[1]


def kernel_tree(**values):
    return {"id": "linux", "url": "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
            "refs": ["refs/heads/master"], "default_ref": "refs/heads/master", **values}


def uboot_tree(**values):
    return {"id": "u-boot", "url": "https://git.u-boot-project.org/u-boot/u-boot.git",
            "refs": ["refs/heads/master", "refs/heads/next"], "default_ref": "refs/heads/master", **values}


class CodeConfigTests(unittest.TestCase):
    def normalize(self, **values):
        return normalize_code_config({"trees": [kernel_tree()], **values}, data_dir="/tmp/code-test")

    def invalid(self, value):
        with self.assertRaises(MirrorError) as caught:
            normalize_code_config(value, data_dir="/tmp/code-test")
        self.assertEqual(caught.exception.code, "invalid_config")

    def test_mail_only_default_is_disabled_and_side_effect_free(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "not-created"
            with patch("subprocess.Popen") as proc, patch("urllib.request.urlopen") as network:
                result = normalize_code_config(data_dir=data)
            self.assertFalse(result["enabled"])
            self.assertEqual(result["trees"], [])
            self.assertEqual(result["root_dir"], str(data / "code"))
            self.assertFalse(data.exists())
            proc.assert_not_called()
            network.assert_not_called()

    def test_code_cursor_budgets_have_independent_defaults_and_bounds(self):
        limits = self.normalize()["limits"]
        self.assertEqual((limits["max_sessions"], limits["session_bytes"], limits["cursor_ttl"]),
                         (16, 8 * 1024**2, 900))
        for key, maximum in (("max_sessions", 64), ("session_bytes", 32 * 1024**2), ("cursor_ttl", 3600)):
            self.assertEqual(self.normalize(limits={key: maximum})["limits"][key], maximum)
            for invalid in (False, 0, maximum + 1):
                with self.subTest(key=key, invalid=invalid):
                    self.invalid({"limits": {key: invalid}})

    def test_pure_and_idempotent(self):
        original = {"enabled": True, "trees": [kernel_tree(tag_patterns=[{"pattern": "refs/tags/v*", "keep": None}])]}
        saved = copy.deepcopy(original)
        normalized = normalize_code_config(original, data_dir="/tmp/code-test")
        self.assertEqual(original, saved)
        self.assertEqual(normalize_code_config(normalized, data_dir="/tmp/ignored"), normalized)
        normalized["trees"][0]["refs"].append("refs/heads/topic")
        self.assertEqual(original, saved)

    def test_enabled_requires_boolean_and_explicit_catalog(self):
        for value in ({"enabled": True}, {"enabled": "false"}, {"enabled": 0}, {"trees": {}}, False, []):
            with self.subTest(value=value):
                self.invalid(value)

    def test_malformed_disabled_catalog_is_not_silently_ignored(self):
        self.invalid({"enabled": False, "trees": [kernel_tree(url="file:///tmp/repo")]})

    def test_kernel_manifest_binding(self):
        result = self.normalize()["trees"][0]
        self.assertEqual(result["fetch_mode"], "grokmirror_manifest")
        self.assertEqual(result["manifest_url"], "https://git.kernel.org/manifest.js.gz")
        self.assertEqual(result["manifest_key"], "/pub/scm/linux/kernel/git/torvalds/linux.git")
        for changes in ({"manifest_key": "/other.git"}, {"fetch_mode": "conditional_refs"},
                        {"manifest_url": "https://source.denx.de/manifest.js.gz"},
                        {"manifest_url": "https://git.kernel.org/another/manifest.js.gz"}):
            with self.subTest(changes=changes):
                self.invalid({"trees": [kernel_tree(**changes)]})

    def test_uboot_uses_hourly_or_slower_conditional_refs(self):
        result = self.normalize(trees=[uboot_tree()])["trees"][0]
        self.assertEqual(result["fetch_mode"], "conditional_refs")
        self.assertEqual(result["interval_seconds"], 3600)
        self.assertNotIn("manifest_url", result)
        for changes in ({"interval_seconds": 3599}, {"fetch_mode": "grokmirror_manifest"},
                        {"manifest_url": "https://git.kernel.org/manifest.js.gz"}):
            with self.subTest(changes=changes):
                self.invalid({"trees": [uboot_tree(**changes)]})

    def test_https_host_namespace_and_url_tricks_rejected(self):
        suffix = "/pub/scm/linux/kernel/git/torvalds/linux.git"
        for url in ("http://git.kernel.org" + suffix, "ssh://git.kernel.org" + suffix,
                    "https://git.kernel.org.evil.invalid" + suffix,
                    "https://attacker.invalid/repo.git", "https://localhost/repo.git",
                    "https://127.0.0.1/repo.git", "https://git.kernel.org:444" + suffix,
                    "https://a@ git.kernel.org" + suffix, "https://a@git.kernel.org" + suffix,
                    "https://git.kernel.org" + suffix + "?x=y", "https://git.kernel.org" + suffix + "#ref",
                    "https://git.kernel.org" + suffix + "?", "https://git.kernel.org" + suffix + "#",
                    " https://git.kernel.org" + suffix, "https://git.kernel.org\n" + suffix,
                    "https://git.kernel.org/pub/../private.git",
                    "https://git.kernel.org/pub/%2e%2e/private.git",
                    "https://git.kernel.org/pub//scm/linux/kernel/git/torvalds/linux.git",
                    "https://git.kernel.org/other.git", "https://git.kernel.org" + suffix + "/tree",
                    "https://source.denx.de/other/repo.git", "ext::sh -c echo"):
            with self.subTest(url=url):
                self.invalid({"trees": [kernel_tree(url=url)]})

    def test_url_normalization_and_single_repo_per_upstream(self):
        canonical = kernel_tree()["url"]
        result = self.normalize(trees=[kernel_tree(url=canonical.replace("git.kernel.org", "GIT.KERNEL.ORG:443") + "/")])
        self.assertEqual(result["trees"][0]["url"], canonical)
        self.invalid({"trees": [kernel_tree(), kernel_tree(id="second", url=canonical + "/")]})
        self.invalid({"trees": [kernel_tree(), uboot_tree(id="linux")]})
        self.invalid({"trees": [uboot_tree(), uboot_tree(id="legacy", url="https://source.denx.de/u-boot/u-boot.git")]})

    def test_ids_are_stable_path_safe_and_bounded(self):
        for tree_id in ("../tree", "/tmp/repo", "-option", "Tree", "a.b", "a" * 65, "", 3, "a\n"):
            with self.subTest(tree_id=tree_id):
                self.invalid({"trees": [kernel_tree(id=tree_id)]})
        self.assertEqual(self.normalize(trees=[kernel_tree(id="linux-qcom_1")])["trees"][0]["id"], "linux-qcom_1")

    def test_refs_are_not_git_revision_expressions_or_refspecs(self):
        for ref in ("master", "HEAD", "--help", "refs/heads/master~1", "refs/heads/master:path",
                    "+refs/heads/master:refs/heads/a", "refs/heads/*", "refs/heads/next?",
                    "refs/heads/a[b]", "refs/heads/a b", "refs/heads/a\n", "refs/heads/a\\b",
                    "refs/heads/a..b", "refs/heads/.hidden", "refs/heads/a.lock/x",
                    "refs/heads/a/", "refs/heads/a.", "refs/heads//a", "refs/remotes/origin/a",
                    "refs/heads/@{1}", "refs/heads/" + "x" * 512, "a" * 40):
            with self.subTest(ref=ref), self.assertRaises(MirrorError):
                validate_code_ref(ref)
        self.assertEqual(validate_code_ref("refs/heads/linux-6.18.y"), "refs/heads/linux-6.18.y")
        self.assertEqual(validate_code_ref("refs/tags/next-20261021"), "refs/tags/next-20261021")

    def test_default_refs_are_explicit_subsets(self):
        for changes in ({"default_ref": "refs/heads/next"}, {"refs": []}, {"refs": "refs/heads/master"},
                        {"refs": ["refs/heads/master"] * 2}):
            with self.subTest(changes=changes):
                self.invalid({"trees": [kernel_tree(**changes)]})
        result = self.normalize(trees=[kernel_tree(
            tag_patterns=[{"pattern": "refs/tags/next-*", "keep": 3}])])["trees"][0]
        self.assertEqual(result["tag_patterns"], [{"pattern": "refs/tags/next-*", "keep": 3}])

    def test_tags_require_safe_bounded_explicit_selectors(self):
        for selectors in ({}, ["refs/tags/v*"], [{"pattern": "refs/tags/v*"}],
                          [{"pattern": "refs/*", "keep": 3}], [{"pattern": "refs/tags/*", "keep": 3}],
                          [{"pattern": "refs/tags/v[0-9]*", "keep": 3}],
                          [{"pattern": "refs/tags/v*rc*", "keep": 3}],
                          [{"pattern": "refs/tags/v*", "keep": 0}], [{"pattern": "refs/tags/v*", "keep": True}],
                          [{"pattern": "refs/tags/v*", "keep": 1001}], [{"pattern": "refs/tags/v*", "keep": "all"}],
                          [{"pattern": "refs/tags/v*", "keep": 3}] * 2):
            with self.subTest(selectors=selectors):
                self.invalid({"trees": [kernel_tree(tag_patterns=selectors)]})

    def test_branch_patterns_declare_numeric_release_intent(self):
        patterns = [{"pattern": "refs/heads/arm64-for-*", "keep": 1},
                    {"pattern": "refs/heads/drivers-for-*", "keep": 1},
                    {"pattern": "refs/heads/clk-for-*", "keep": 1}]
        tree = kernel_tree(refs=["refs/heads/for-next"], default_ref="refs/heads/for-next",
                           branch_patterns=patterns)
        before = copy.deepcopy(tree)
        normalized = self.normalize(trees=[tree])["trees"][0]
        self.assertEqual(normalized["branch_patterns"], patterns)
        self.assertEqual(normalized["refs"], ["refs/heads/for-next"])
        self.assertEqual(tree, before)
        normalized["branch_patterns"][0]["keep"] = 9
        self.assertEqual(tree, before)

    def test_branch_patterns_require_literal_head_prefix_and_keep_one(self):
        for patterns in ({}, ["refs/heads/arm64-for-*"], [{"pattern": "refs/heads/arm64-for-*"}],
                         [{"pattern": "refs/tags/v*", "keep": 1}],
                         [{"pattern": "refs/heads/*", "keep": 1}],
                         [{"pattern": "refs/heads/a*fix*", "keep": 1}],
                         [{"pattern": "refs/heads/a[0-9]*", "keep": 1}],
                         [{"pattern": "refs/heads/a?*", "keep": 1}],
                         [{"pattern": "refs/heads/../a*", "keep": 1}],
                         [{"pattern": "refs/heads/a*", "keep": None}],
                         [{"pattern": "refs/heads/a*", "keep": 0}],
                         [{"pattern": "refs/heads/a*", "keep": 2}],
                         [{"pattern": "refs/heads/a*", "keep": True}],
                         [{"pattern": "refs/heads/a*", "keep": 1, "sort": "lexical"}],
                         [{"pattern": "refs/heads/a*", "keep": 1}] * 2):
            with self.subTest(patterns=patterns):
                self.invalid({"trees": [kernel_tree(branch_patterns=patterns)]})

    def test_limits_are_finite_integer_bounded_and_strict(self):
        for key, value in (("fetch_timeout_seconds", 3601), ("fetch_memory_bytes", 1024 ** 3 + 1),
                           ("repository_bytes", 0), ("max_fetch_bytes", True),
                           ("command_output_bytes", 1.0), ("manifest_bytes", float("nan")),
                           ("manifest_expanded_bytes", float("inf")), ("typo_limit", 10)):
            with self.subTest(key=key, value=value):
                self.invalid({"limits": {key: value}})
        self.invalid({"limits": {"manifest_bytes": 1024, "manifest_expanded_bytes": 512}})
        for reserve in (.29, 1, True, "0.3", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(reserve=reserve):
                self.invalid({"min_free_fraction": reserve})
        for count in (0, True, 10001, 1.2):
            self.invalid({"max_refs": count})
        self.invalid({"max_refs": 1, "trees": [uboot_tree()]})

    def test_query_limits_support_finite_fractional_timeout(self):
        self.assertEqual(self.normalize(limits={"query_timeout": .1})["limits"]["query_timeout"], .1)
        for value in (0, -1, True, float("inf"), float("nan"), 120.1, "15", 10 ** 1000):
            with self.subTest(value=value):
                self.invalid({"limits": {"query_timeout": value}})
        for key in ("query_output_bytes", "query_memory_bytes", "max_file_bytes", "max_line_bytes", "scan_limit"):
            with self.subTest(key=key):
                self.invalid({"limits": {key: 0}})

    def test_unknown_fields_fail_closed(self):
        self.invalid({"enable": True})
        self.invalid({"trees": [kernel_tree(fetch_mod="anything")]})
        self.invalid({"trees": [kernel_tree(tag_patterns=[{"pattern": "refs/tags/v*", "keep": 2, "typo": 1}])]})

    def test_api_and_coordinator_normalize_the_same_catalog(self):
        from lore_mirror.config import load_config
        from lore_mirror.sync import prepare_config
        with tempfile.TemporaryDirectory() as temp:
            config = {"data_dir": temp, "lists": [{"id": "fixture"}],
                      "code": {"trees": [kernel_tree()]}}
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(config))
            api, coordinator = load_config(config), prepare_config(path)
            self.assertEqual(api["code"], coordinator["code"])
            self.assertFalse((Path(temp) / "code").exists())


    def test_example_is_valid_disabled_and_preserves_requested_refs(self):
        example = json.loads((ROOT / "config" / "code-trees.json").read_text())
        normalized = normalize_code_config(example["code"], data_dir="/tmp/example")
        self.assertFalse(normalized["enabled"])
        self.assertEqual(len(normalized["trees"]), 6)
        by_id = {tree["id"]: tree for tree in normalized["trees"]}
        self.assertEqual(by_id["linux-qcom"]["refs"], ["refs/heads/for-next"])
        self.assertEqual(by_id["linux-qcom"]["default_ref"], "refs/heads/for-next")
        self.assertEqual(by_id["linux-qcom"]["branch_patterns"], [
            {"pattern": "refs/heads/arm64-for-*", "keep": 1},
            {"pattern": "refs/heads/drivers-for-*", "keep": 1},
            {"pattern": "refs/heads/clk-for-*", "keep": 1}])
        self.assertEqual(by_id["linux-next"]["tag_patterns"], [{"pattern": "refs/tags/next-*", "keep": 3}])
        self.assertEqual(by_id["u-boot-qcom"]["refs"], ["refs/heads/qcom-main", "refs/heads/qcom-next"])
        self.assertIn("refs/heads/linux-6.18.y", by_id["linux-stable"]["refs"])


if __name__ == "__main__":
    unittest.main()
