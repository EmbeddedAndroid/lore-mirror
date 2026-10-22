"""Opt-in real namespace lifecycle tests; not a Docker/cgroup/capacity claim."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts/index-lifecycle-fixture.py"


class NativeIndexLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tini = Path(os.environ.get("LORE_TINI", "/usr/bin/tini"))
        # Explicit opt-in prevents Docker's offline unit suite trying to create
        # nested user namespaces, which its security policy need not authorize.
        if os.environ.get("LORE_INDEX_LIFECYCLE_REQUIRED") != "1":
            raise unittest.SkipTest("run native namespace lifecycle gate explicitly")
        if not cls.tini.is_file() or not os.access(cls.tini, os.X_OK) or not shutil.which("unshare"):
            raise RuntimeError("native lifecycle gate requires executable Tini and unshare")
        probe = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", "true"],
                               capture_output=True, text=True, timeout=5)
        if probe.returncode:
            raise RuntimeError("native lifecycle namespace unavailable: " + probe.stderr)

    def lifecycle(self, mode, expected):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            result = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--kill-child=SIGKILL", "--mount-proc",
                                     str(self.tini), "--", sys.executable, str(FIXTURE), "supervise", tmp, mode,
                                     "--tini", str(self.tini), "--native-no-cgroup"],
                                    env=env, capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, expected, result.stderr)
            checked = subprocess.run([sys.executable, str(FIXTURE), "verify", tmp, mode, "--native-no-cgroup"],
                                     env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_supervisor_sigkill_removes_escaped_descendant_and_releases_lock(self):
        self.lifecycle("supervisor-kill", 137)

    def test_child_sigkill_is_detected_promptly_and_namespace_reaps_escaped_descendant(self):
        self.lifecycle("child-kill", 42)

    def test_init_sigterm_forwards_to_supervisor_and_releases_lock(self):
        self.lifecycle("supervisor-term", 143)
