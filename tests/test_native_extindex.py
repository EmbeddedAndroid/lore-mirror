"""Native public-inbox: interrupted extindex runs resume consistently.

A synthetic v2 inbox is imported and basic-indexed, then the shared
extindex is built through the sync coordinator's run_command. A SIGKILL of
the whole process group models a power cut; the coordinator's repair queue
must restore Xapian. Sliced runs must leave a monotonically growing,
checkpointed index, with a second inbox indexed between slices. The final
extindex must hold exactly the inbox's own (article number, blob) pairs,
once, with matching over, xref3 and Xapian documents.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest

from lore_mirror.sync import extindex_floor, requeue_after_unclean_exit, run_command
from tests import test_native_e2e as native

MESSAGES = 6000
XAPIAN_DOCIDS_PERL = r"""
use strict;
use Search::Xapian;
for my $shard (@ARGV) {
    my $db = Search::Xapian::Database->new($shard);
    for (my $it = $db->postlist_begin(''); $it != $db->postlist_end(''); $it++) {
        print $it->get_docid, "\n";
    }
}
"""
XOVER_PERL = r"""
use strict;
use PublicInbox::Config;
my $ibx = PublicInbox::Config->new->lookup_name(shift @ARGV) or die "no inbox\n";
my ($beg, $end) = (1, $ibx->mm->num_highwater);
while (my $msgs = $ibx->over->query_xover($beg, $end)) {
    last unless @$msgs;
    print "$_->{num} $_->{blob}\n" for @$msgs;
    $beg = $msgs->[-1]->{num} + 1;
}
"""


def write_messages(directory, prefix, count):
    directory.mkdir(parents=True, exist_ok=True)
    for number in range(count):
        body = "\n".join(f"line {line} of {prefix} {number} alpha beta gamma {number * line}"
                         for line in range(40))
        reply = f"In-Reply-To: <{prefix}{number - 1}@fixture.invalid>\n" if number % 3 else ""
        (directory / f"{number:06d}.eml").write_text(
            f"From: Dev {number % 50} <dev{number % 50}@fixture.invalid>\n"
            f"To: {prefix}@fixture.invalid\nSubject: [PATCH {number}] {prefix} change {number}\n"
            f"Date: Mon, 1 Jan 2024 00:{number % 60:02d}:00 +0000\n"
            f"Message-ID: <{prefix}{number}@fixture.invalid>\n{reply}\n{body}\n")


def query(db, sql, *args):
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)) as conn:
        return conn.execute(sql, args).fetchall()


class NativeSliceTests(unittest.TestCase):
    slice_seconds = None

    def setUp(self):
        native.require_native()
        self.temp = tempfile.TemporaryDirectory(prefix="lore-native-slices-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("GIT_CONFIG_"):
                del self.env[key]
        self.env.update({"PI_CONFIG": str(self.root / "public-inbox-config"), "HOME": str(self.root / "home"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_COUNT": "0",
            "LC_ALL": "C", "TZ": "UTC"})
        (self.root / "home").mkdir()
        self.extindex = self.root / "extindex"
        self.expected_cache = {}
        self.config = {"data_dir": str(self.root), "extindex": str(self.extindex),
            "min_free_fraction": 0, "job_timeout_seconds": 600,
            "index_slice_grace_seconds": 120, "index_stop_grace_seconds": 30}
        self.big = self.inbox("big", MESSAGES)
        if NativeSliceTests.slice_seconds is None:
            # SIGTERM before public-inbox installs its quit handler kills perl
            # outright, so a slice must outlast native start-up on this host.
            probe = self.inbox("probe", 1)
            started = time.monotonic()
            self.native("public-inbox-extindex", "-L", "full", "-j1", str(self.root / "probe-extindex"), str(probe))
            NativeSliceTests.slice_seconds = max(1.0, 2 * (time.monotonic() - started))

    def native(self, *argv):
        result = subprocess.run(argv, cwd=self.root, env=self.env, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise AssertionError(f"{argv!r} failed\n{result.stdout}\n{result.stderr}")

    def inbox(self, name, count):
        path = self.root / "inboxes" / name
        self.native("public-inbox-init", "-V2", "-L", "basic", "-j", "1", "--ng", "org.test." + name,
                    name, str(path), "https://example.invalid/" + name, name + "@fixture.invalid")
        self.add(name, count)
        return path

    def add(self, name, count, start=0):
        mail = self.root / "mail" / f"{name}-{start}"
        write_messages(mail, name, start + count)
        files = sorted(mail.glob("*.eml"))[start:]
        self.native("perl", "-e", native.IMPORT_PERL, name, *map(str, files))
        self.native("public-inbox-index", "-L", "basic", "--jobs=0", str(self.root / "inboxes" / name))
        self.expected_cache.pop(name, None)

    def argv(self, inbox):
        return ["public-inbox-extindex", "-L", "full", "-j1", "--batch-size=64k",
                "--no-multi-pack-index", str(self.extindex), str(inbox)]

    def over(self):
        return self.extindex / "ei15" / "over.sqlite3"

    def indexed(self, name):
        rows = query(self.over(), "SELECT xnum,hex(oidbin),docid FROM xref3 WHERE ibx_id="
                     "(SELECT ibx_id FROM inboxes WHERE eidx_key=?)", "org.test." + name)
        return {(xnum, oid.lower()): docid for xnum, oid, docid in rows}

    def expected(self, name):
        if name not in self.expected_cache:
            out = subprocess.run(["perl", "-e", XOVER_PERL, name], env=self.env, capture_output=True,
                                 text=True, check=True, timeout=60).stdout
            self.expected_cache[name] = {(int(num), blob) for num, blob in
                                         (line.split() for line in out.splitlines())}
        return self.expected_cache[name]

    def checkpoint(self, name):
        return query(self.over(), "SELECT key,val FROM eidx_meta WHERE key LIKE ?", f"lc-v2:org.test.{name}//%")

    def assert_consistent(self, names):
        """Each inbox message is indexed once; over, xref3 and Xapian agree.

        Docids need not be contiguous: a killed writer may consume one.
        """
        for name in names:
            indexed = self.indexed(name)
            self.assertEqual(set(indexed), self.expected(name))
            self.assertEqual(len(query(self.over(), "SELECT 1 FROM xref3 WHERE ibx_id=(SELECT ibx_id FROM "
                                       "inboxes WHERE eidx_key=?)", "org.test." + name)), len(indexed))
        over = {num for (num,) in query(self.over(), "SELECT num FROM over WHERE num>0")}
        referenced = {docid for (docid,) in query(self.over(), "SELECT DISTINCT docid FROM xref3")}
        shards = sorted(str(path) for path in (self.extindex / "ei15").iterdir() if path.name.isdigit())
        # -j1 writes one shard, whose Xapian docids are the over docids.
        self.assertEqual(len(shards), 1)
        out = subprocess.run(["perl", "-e", XAPIAN_DOCIDS_PERL, *shards], env=self.env, capture_output=True,
                             text=True, check=True, timeout=120).stdout
        xapian = [int(line) for line in out.split()]
        self.assertEqual(len(xapian), len(set(xapian)))
        self.assertEqual(over, referenced)
        self.assertEqual(set(xapian), over)

    def sliced(self, inbox, between=None, limit=60):
        counts, checkpoints, first = [], [], None
        for _ in range(limit):
            result = run_command(self.argv(inbox), self.config, self.root / "logs" / "extindex.log",
                                 env=self.env, slice_seconds=self.slice_seconds, graceful=True)
            counts.append(len(self.indexed("big")))
            checkpoints.append(self.checkpoint("big"))
            if first is None:
                first = self.indexed("big")
            if not result.get("sliced"):
                return counts, checkpoints, first
            if between:
                between()
                between = None
        self.fail(f"import did not finish in {limit} slices: {counts}")

    def test_sliced_import_resumes_without_reindexing(self):
        counts, checkpoints, first = self.sliced(self.big)
        self.assertGreaterEqual(len(counts), 2, counts)
        self.assertLess(counts[0], MESSAGES)
        self.assertGreater(counts[0], 0)
        # Progress never goes backwards; none starts again from zero. A slice
        # shorter than native start-up on a loaded host may add nothing.
        self.assertEqual(counts, sorted(counts))
        self.assertGreater(len(set(counts)), 1)
        self.assertEqual(counts[-1], MESSAGES)
        self.assertTrue(checkpoints[0], "first slice left no last-commit checkpoint")
        self.assertGreater(len({tuple(c) for c in checkpoints}), 1)
        final = self.indexed("big")
        self.assertEqual({key: final[key] for key in first}, first)
        self.assert_consistent(["big"])
        log = (self.root / "logs" / "extindex.log").read_text()
        self.assertEqual(log.count("slice_end_requested"), len(counts) - 1)

    def test_other_inbox_indexes_between_slices(self):
        hot = self.inbox("hot", 20)
        self.native(*self.argv(hot))
        def refresh():
            before = len(self.indexed("big"))
            self.add("hot", 5, start=20)
            self.native(*self.argv(hot))
            self.assertEqual(len(self.indexed("hot")), 25)
            self.assertEqual(len(self.indexed("big")), before)
        counts, _, _ = self.sliced(self.big, between=refresh)
        self.assertGreaterEqual(len(counts), 2, counts)
        self.assert_consistent(["big", "hot"])

    def xapian_documents(self):
        total = 0
        for shard in (path for path in (self.extindex / "ei15").iterdir() if path.name.isdigit()):
            out = subprocess.run(["xapian-delve", str(shard)], capture_output=True, text=True, check=True).stdout
            total += int(re.search(r"number of documents = (\d+)", out).group(1))
        return total

    def power_cut(self, inbox, rows):
        child = subprocess.Popen(self.argv(inbox), cwd=self.root, env=self.env, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline and child.poll() is None:
                with contextlib.suppress(sqlite3.Error, FileNotFoundError):
                    if self.over().exists() and len(self.indexed("big")) >= rows:
                        break
                time.sleep(.02)
            self.assertIsNone(child.poll(), "import finished before the power cut")
        finally:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

    def test_power_cut_mid_import_recovers(self):
        floor = extindex_floor(self.config)
        self.power_cut(self.big, 500)
        cut = len(self.indexed("big"))
        self.assertLess(cut, MESSAGES)
        # SQLite commits before Xapian: the cut may leave Xapian behind.
        self.assertLessEqual(self.xapian_documents(), query(self.over(), "SELECT COUNT(*) FROM over")[0][0])
        self.assertGreaterEqual(requeue_after_unclean_exit(self.config, floor), cut)
        counts, _, _ = self.sliced(self.big)
        self.assertGreaterEqual(counts[0], cut)
        self.assert_consistent(["big"])
        self.assertEqual(query(self.over(), "SELECT COUNT(*) FROM eidxq")[0][0], 0)

    def test_power_cut_after_clean_slice_requeues_only_new_work(self):
        result = run_command(self.argv(self.big), self.config, self.root / "logs" / "extindex.log",
                             env=self.env, slice_seconds=self.slice_seconds, graceful=True)
        self.assertTrue(result.get("sliced"))
        floor = extindex_floor(self.config)
        self.assertGreater(floor["over_num"], 0)
        self.assertEqual(self.xapian_documents(), query(self.over(), "SELECT COUNT(*) FROM over WHERE num>0")[0][0])
        self.power_cut(self.big, floor["over_num"] + 200)
        requeued = requeue_after_unclean_exit(self.config, floor)
        self.assertGreater(requeued, 0)
        self.assertLessEqual(requeued, len(self.indexed("big")) - floor["over_num"])
        self.sliced(self.big)
        self.assert_consistent(["big"])

if __name__ == "__main__":
    unittest.main()
