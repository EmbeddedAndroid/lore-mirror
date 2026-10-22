"""Offline links policy and CLI contract tests; no live mail or Git fetches."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from lore_mirror.__main__ import main
from lore_mirror.code_config import DEFAULT_CODE_LINKS, normalize_code_config
from lore_mirror.config import load_config
from lore_mirror.errors import MirrorError


def code_config(**values):
    return {"enabled": True, "trees": [{
        "id": "linux",
        "url": "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
        "refs": ["refs/heads/master"], "default_ref": "refs/heads/master",
    }], **values}


class CodeLinksConfigTests(unittest.TestCase):
    def invalid(self, links, **values):
        with self.assertRaises(MirrorError) as caught:
            normalize_code_config(code_config(links=links, **values), data_dir="/tmp/code-links-test")
        self.assertEqual(caught.exception.code, "invalid_config")

    def test_default_disabled_has_no_invented_date_or_side_effects(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "uncreated"
            with patch("subprocess.Popen") as process, patch("urllib.request.urlopen") as network:
                result = normalize_code_config(data_dir=data)
            self.assertEqual(result["links"], {
                "enabled": False, "patch_since": None, "batch_commits": 50,
                "batch_mails": 25, "timeout_seconds": 30,
                "max_db_bytes": 1024 ** 3, "max_patch_bytes": 8 * 1024 * 1024,
            })
            self.assertEqual(result["links"], DEFAULT_CODE_LINKS)
            self.assertFalse(data.exists())
            process.assert_not_called()
            network.assert_not_called()

    def test_enabled_requires_code_and_explicit_date(self):
        self.invalid({"enabled": True})
        self.invalid({"enabled": True, "patch_since": None})
        self.invalid({"enabled": True, "patch_since": "2026-10-21"}, enabled=False)
        links = {"enabled": True, "patch_since": "2026-10-21"}
        normalized = normalize_code_config(code_config(links=links))
        self.assertTrue(normalized["links"]["enabled"])
        self.assertEqual(normalized["links"]["patch_since"], "2026-10-21")
        # Links do not imply the independently opt-in content index.
        self.assertFalse(normalized["index"]["enabled"])

    def test_date_is_exact_ascii_calendar_date_even_while_disabled(self):
        for value in ("", "20261021", "2026-1-21", "26-10-21", "2026-10-21T00:00:00Z",
                      " 2026-10-21", "2026-10-21\n", "2026-W43-3", "today",
                      "2026-02-29", "2026-13-01", "0000-01-01", "2026-00-01",
                      "2026-01-00", "2026-04-31", "２０２６-１０-２１", True, 20261021,
                      [], {}):
            with self.subTest(value=value):
                self.invalid({"patch_since": value})
        for value in (None, "2024-02-29", "2000-02-29", "0001-01-01", "9999-12-31"):
            with self.subTest(value=value):
                normalized = normalize_code_config({"links": {"patch_since": value}})
                self.assertEqual(normalized["links"]["patch_since"], value)

    def test_links_schema_and_boolean_are_strict(self):
        for value in (None, False, [], "disabled", 0):
            with self.subTest(value=value):
                self.invalid(value)
        for value in (0, 1, None, "false", [], {}):
            with self.subTest(enabled=value):
                self.invalid({"enabled": value})
        self.invalid({"enable": False})
        self.invalid({"db_path": "/tmp/arbitrary.sqlite"})

    def test_all_budget_limits_are_strict_positive_bounded_integers(self):
        maxima = {"batch_commits": 500, "batch_mails": 250, "timeout_seconds": 120,
                  "max_db_bytes": 2 * 1024 ** 3, "max_patch_bytes": 16 * 1024 * 1024}
        for key, maximum in maxima.items():
            for value in (0, -1, True, False, 1.0, "1", None, float("nan"),
                          float("inf"), maximum + 1, 10 ** 1000):
                with self.subTest(key=key, value=value):
                    self.invalid({key: value})
            for value in (1, maximum):
                with self.subTest(key=key, valid=value):
                    normalized = normalize_code_config({"links": {key: value}})
                    self.assertEqual(normalized["links"][key], value)

    def test_pure_idempotent_and_default_dict_is_not_shared(self):
        original = code_config(links={"enabled": True, "patch_since": "2026-10-21"})
        saved = copy.deepcopy(original)
        normalized = normalize_code_config(original, data_dir="/tmp/code-links-test")
        self.assertEqual(original, saved)
        self.assertEqual(normalize_code_config(normalized), normalized)
        normalized["links"]["batch_mails"] = 1
        self.assertEqual(original, saved)
        self.assertEqual(normalize_code_config()["links"]["batch_mails"], 25)

    def test_load_config_normalizes_and_rejects_invalid_links(self):
        config = load_config({"data_dir": "/tmp/code-links-test", "backend": "fixture"})
        self.assertEqual(config["code"]["links"], DEFAULT_CODE_LINKS)
        with self.assertRaises(MirrorError):
            load_config({"data_dir": "/tmp/code-links-test", "code": code_config(links={"enabled": True})})

    def test_sync_prepare_uses_identical_policy_without_creating_storage(self):
        from lore_mirror.sync import prepare_config
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            path = Path(temp) / "config.json"
            config = {"data_dir": str(root), "lists": [{"id": "u-boot"}],
                      "code": code_config(links={"enabled": True, "patch_since": "2026-10-21"})}
            path.write_text(json.dumps(config))
            self.assertEqual(prepare_config(path)["code"], load_config(path)["code"])
            self.assertFalse(root.exists())
            config["code"]["links"]["patch_since"] = None
            path.write_text(json.dumps(config))
            with self.assertRaises(MirrorError):
                prepare_config(path)
            self.assertFalse(root.exists())


class CodeLinksCLITests(unittest.TestCase):
    def test_real_disabled_cli_creates_no_data_or_subprocess(self):
        for code in ({}, code_config()):
            with self.subTest(code_enabled=code.get("enabled", False)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "data"
                path = Path(temp) / "config.json"
                path.write_text(json.dumps({"data_dir": str(root), "backend": "fixture", "code": code}))
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch("subprocess.Popen") as process, \
                        patch("urllib.request.urlopen") as network:
                    self.assertEqual(main(["code-links", "--config", str(path)]), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], "disabled")
                self.assertFalse(root.exists())
                process.assert_not_called()
                network.assert_not_called()

    def run_cli(self, result=None, error=None, *, code=None):
        module = ModuleType("lore_mirror.code_links")
        module.sync_links_once = Mock(return_value=result, side_effect=error)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"data_dir": str(Path(temp) / "data"),
                                        "backend": "fixture", "code": code or {}}))
            output, errors = io.StringIO(), io.StringIO()
            with patch.dict("sys.modules", {"lore_mirror.code_links": module}), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = main(["code-links", "--config", str(path)])
            return status, output.getvalue(), errors.getvalue(), module.sync_links_once

    def test_ready_disabled_and_partial_are_successful_bounded_progress(self):
        for status in ("ready", "disabled", "partial"):
            with self.subTest(status=status):
                result = {"schema_version": 1, "status": status, "generation": 2,
                          "processed_commits": 1, "processed_mails": 0, "coverage": {}}
                exit_code, output, errors, runner = self.run_cli(result)
                self.assertEqual(exit_code, 0)
                self.assertEqual(json.loads(output), result)
                self.assertEqual(errors, "")
                runner.assert_called_once()
                self.assertEqual(runner.call_args.args[0]["code"]["links"], DEFAULT_CODE_LINKS)

    def test_waiting_and_failure_statuses_are_nonzero_and_preserved(self):
        for status in ("blocked", "degraded", "waiting_for_io", "unknown"):
            with self.subTest(status=status):
                result = {"status": status, "last_error": "bounded failure"}
                exit_code, output, errors, _ = self.run_cli(result)
                self.assertEqual(exit_code, 2)
                self.assertEqual(json.loads(output), result)
                self.assertEqual(errors, "")

    def test_structured_errors_use_existing_error_contract(self):
        exit_code, output, errors, _ = self.run_cli(error=MirrorError("code_links_error", "unavailable", 503))
        self.assertEqual(exit_code, 1)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors), {"error": {"code": "code_links_error", "message": "unavailable"}})

    def test_invalid_policy_is_rejected_before_coordinator_runs(self):
        exit_code, output, errors, runner = self.run_cli(code=code_config(links={"enabled": True}))
        self.assertEqual(exit_code, 1)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "invalid_config")
        runner.assert_not_called()

    def test_config_is_required_and_runtime_horizon_overrides_are_rejected(self):
        for args in (["code-links"],
                     ["code-links", "--config", "unused.json", "--patch-since", "2026-10-21"],
                     ["code-links", "--config", "unused.json", "--daemon"]):
            with self.subTest(args=args), patch("lore_mirror.__main__.load_config") as loader, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(args)
            self.assertEqual(caught.exception.code, 2)
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
