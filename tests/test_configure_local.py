import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/configure-local.py"


class NativeConfigurationTests(unittest.TestCase):
    def test_absolute_paths_and_approved_scope_without_data_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "local.json"
            data = Path(tmp) / "archive"
            result = subprocess.run([sys.executable, str(SCRIPT), "--output", str(output),
                                     "--data-dir", str(data)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            cfg = json.loads(output.read_text())
            self.assertEqual(cfg["data_dir"], str(data))
            self.assertFalse(data.exists())
            self.assertEqual({item["id"] for item in cfg["lists"]},
                             {"linux-arm-msm", "u-boot", "devicetree-spec", "u-boot-amlogic"})
            self.assertTrue(Path(cfg["query_helper"]).is_file())
            for catalog in cfg["maintainers_catalogs"].values():
                self.assertTrue(Path(catalog["path"]).is_file())
            for item in cfg["lists"]:
                self.assertEqual(item["inbox_path"], str(data / "inboxes" / item["id"]))

    def test_existing_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "local.json"
            output.write_text("preserve this exact content")
            result = subprocess.run([sys.executable, str(SCRIPT), "--output", str(output)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to overwrite", result.stderr)
            self.assertEqual(output.read_text(), "preserve this exact content")


if __name__ == "__main__":
    unittest.main()
