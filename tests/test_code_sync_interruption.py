"""Interrupt a real local pack transfer, not merely a completed fetch callback."""
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import unittest
from unittest.mock import patch

from tests import test_code_sync as fixtures
from lore_mirror.code_sync import STAGING


class ActualFetchInterruptionTests(unittest.TestCase):
    def test_inflight_pack_interruption_preserves_publication_and_resumes(self):
        upload_pack = shutil.which("git-upload-pack")
        if upload_pack is None:
            self.skipTest("git-upload-pack is unavailable")
        fixture = fixtures.CodeSyncTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        previous = fixture.run_once()
        # Incompressible input and a paced upload-pack make the in-flight
        # condition observable without a network service or a timing race.
        (fixture.source / "large").write_bytes(os.urandom(4 * 1024 * 1024))
        fixtures.git(fixture.source, "add", "large")
        fixtures.git(fixture.source, "commit", "-m", "in-flight transfer fixture")
        tip = fixtures.git(fixture.source, "rev-parse", "HEAD")
        wrapper = fixture.root / "paced-upload-pack"
        wrapper.write_text(
            "#!" + sys.executable + "\n"
            "import os, subprocess, sys, time\n"
            "child = subprocess.Popen([" + repr(upload_pack) + ", *sys.argv[1:]], stdout=subprocess.PIPE)\n"
            "while True:\n"
            "    chunk = os.read(child.stdout.fileno(), 4096)\n"
            "    if not chunk:\n"
            "        break\n"
            "    os.write(1, chunk)\n"
            "    time.sleep(.005)\n"
            "sys.exit(child.wait())\n"
        )
        wrapper.chmod(0o755)
        observed_pack_bytes = []
        interrupted_groups = []

        def cleanup_groups():
            for group in interrupted_groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup_groups)

        def interrupted_fetch(tree, refs, runner):
            # The interrupted unit owns private staging work not selected by
            # the next attempt. Recovery must reconcile it before re-fetching,
            # rather than merely succeeding because atomic fetch left no refs.
            runner.git(["update-ref", STAGING + "heads/interrupted-only", fixture.first])
            original_guard = runner._guard

            def guard(child=None):
                original_guard(child)
                if child is None:
                    return
                for temporary in (runner.repo / "objects" / "pack").glob("tmp_*"):
                    try:
                        size = temporary.stat().st_size
                    except FileNotFoundError:
                        continue
                    if size:
                        observed_pack_bytes.append((temporary, size))
                        interrupted_groups.append(child.pid)
                        raise KeyboardInterrupt("stop during an actual incoming pack")

            runner._guard = guard
            runner.git([
                "-c", "protocol.file.allow=always", "fetch", "--atomic", "--no-tags",
                "--no-recurse-submodules", "--no-write-fetch-head", "--no-auto-maintenance",
                "--upload-pack=" + str(wrapper), "--", str(fixture.source),
                *[f"+{ref}:{STAGING}{ref[5:]}" for ref in refs],
            ])

        with patch.object(fixture.adapter, "fetch", side_effect=interrupted_fetch):
            with self.assertRaisesRegex(KeyboardInterrupt, "actual incoming pack"):
                fixture.run_once(14000)
        self.assertTrue(observed_pack_bytes, "fetch must stop after pack bytes arrive")
        interrupted = json.loads(fixture.state_path.read_text())
        self.assertEqual(interrupted["generation"], previous["generation"])
        self.assertEqual(interrupted["trees"]["boot"]["published_refs"],
                         previous["trees"]["boot"]["published_refs"])
        self.assertEqual(interrupted["trees"]["boot"]["status"], "fetching")
        self.assertEqual(fixtures.git(fixture.repo, "show", fixture.second + ":hello.txt"), "second")
        self.assertEqual(fixtures.git(fixture.repo, "rev-parse", "refs/lore/generations/1/heads/main"),
                         fixture.second)
        self.assertEqual(fixtures.git(fixture.repo, "rev-parse", STAGING + "heads/interrupted-only"),
                         fixture.first)
        retained = [(path, path.stat().st_size) for path, _ in observed_pack_bytes if path.exists()]
        self.assertTrue(retained, "real interrupted pack data must remain for the recovery attempt")
        self.assertGreater(sum(size for _, size in retained), 0)
        # The Git leader and its upload/index-pack helpers must be dead before
        # a new writer can retry. Ignore zombies waiting for init to reap them.
        def live_group_members():
            members = []
            for path in Path("/proc").glob("[0-9]*/stat"):
                try:
                    fields = path.read_text().rsplit(")", 1)[1].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if int(fields[2]) in interrupted_groups and fields[0] != "Z":
                    members.append(path.parent.name)
            return members
        stop_at = time.monotonic() + 2
        while live_group_members() and time.monotonic() < stop_at:
            time.sleep(.01)
        self.assertEqual(live_group_members(), [], "interrupted fetch left live writer helpers")
        # Check journal recovery independently of refetch/publish, which would
        # otherwise overwrite fetching even if startup recovery were removed.
        with patch.object(fixture.adapter, "fetch") as fetch:
            with patch.object(fixture.adapter, "remote_refs") as probe:
                recovered = fixture.run_once(14001, should_yield=lambda: True)
        fetch.assert_not_called()
        probe.assert_not_called()
        recovered_row = recovered["trees"]["boot"]
        self.assertEqual(recovered_row["status"], "degraded")
        self.assertEqual(recovered_row["last_error"], "previous code job interrupted")
        self.assertIsNone(recovered_row["next_check_at"])
        self.assertNotIn("pending_generation", recovered_row)
        self.assertEqual(recovered["generation"], previous["generation"])
        self.assertEqual(recovered_row["published_refs"], previous["trees"]["boot"]["published_refs"])
        self.assertEqual(json.loads(fixture.state_path.read_text()), recovered)
        self.assertEqual(fixtures.git(fixture.repo, "rev-parse", STAGING + "heads/interrupted-only"),
                         fixture.first)
        resumed = fixture.run_once(14002)
        self.assertEqual(resumed["trees"]["boot"]["status"], "ready", resumed)
        self.assertEqual(resumed["generation"], previous["generation"] + 1)
        self.assertEqual(resumed["trees"]["boot"]["published_refs"]["refs/heads/main"], tip)
        self.assertEqual(fixtures.git(fixture.repo, "for-each-ref", STAGING), "")
        self.assertEqual(fixtures.git(fixture.repo, "show", tip + ":hello.txt"), "second")
        self.assertTrue((fixture.repo / "packed-refs").is_file())
        for path, _ in retained:
            self.assertFalse(path.exists(), "the next unit must drop the killed fetch's temporary pack")


if __name__ == "__main__":
    unittest.main()
