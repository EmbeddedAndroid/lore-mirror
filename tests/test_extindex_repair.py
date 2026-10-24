"""Unclean extindex exits: graceful stops and the Xapian repair queue."""
import contextlib
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

from lore_mirror.sync import SyncError, extindex_floor, requeue_after_unclean_exit, run_command
from tests import test_priority_refresh as fixtures

# The leader models public-inbox: SIGTERM sets a quit flag, the leader then
# needs its own helper process alive to flush, and exits 0. The helper models
# git cat-file: it records any SIGTERM it receives.
HELPER = """
import signal, sys, time
def term(*_):
    open(sys.argv[1] + "/helper-term", "w").close()
    sys.exit(1)
signal.signal(signal.SIGTERM, term)
open(sys.argv[1] + "/helper-ready", "w").close()
while True:
    time.sleep(.05)
"""
LEADER = """
import signal, subprocess, sys, time
out, mode = sys.argv[1], sys.argv[2]
flush = float(sys.argv[3]) if len(sys.argv) > 3 else .3
helper = subprocess.Popen([sys.executable, "-c", HELPER, out])
quit = []
if mode == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *_: quit.append(1))
open(out + "/leader-ready", "w").close()
while not quit:
    time.sleep(.05)
time.sleep(flush)
with open(out + "/checkpoint", "w") as f:
    f.write("alive" if helper.poll() is None else "dead")
helper.kill()
helper.wait()
""".replace("HELPER", repr(HELPER))


class GracefulStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'data_dir': str(self.root), 'min_free_fraction': 0,
            'job_timeout_seconds': 60, 'index_slice_grace_seconds': 5, 'index_stop_grace_seconds': 5}

    def run_leader(self, mode='quit', flush=.3, **kwargs):
        argv = [sys.executable, '-c', LEADER, str(self.root), mode, str(flush)]
        return run_command(argv, self.config, self.root / 'logs' / 'job.log', **kwargs)

    def test_daemon_stop_asks_leader_to_checkpoint(self):
        def sample(pid):
            if (self.root / 'helper-ready').exists() and (self.root / 'leader-ready').exists():
                raise KeyboardInterrupt
            return 0
        with mock.patch('lore_mirror.sync._aggregate_rss', side_effect=sample), \
                self.assertRaises(KeyboardInterrupt) as raised:
            self.run_leader(graceful=True)
        self.assertTrue(raised.exception.lore_clean_stop)
        self.assertEqual((self.root / 'checkpoint').read_text(), 'alive')
        self.assertFalse((self.root / 'helper-term').exists())

    def test_unclean_daemon_stop_is_not_marked_clean(self):
        self.config['index_stop_grace_seconds'] = 1
        def sample(pid):
            if (self.root / 'leader-ready').exists():
                raise KeyboardInterrupt
            return 0
        with mock.patch('lore_mirror.sync._aggregate_rss', side_effect=sample), \
                self.assertRaises(KeyboardInterrupt) as raised:
            self.run_leader("ignore", graceful=True)
        self.assertFalse(raised.exception.lore_clean_stop)
        self.assertFalse((self.root / 'checkpoint').exists())

    def test_default_grace_lets_a_slow_leader_quit_cleanly(self):
        # Measured quits on .63 took 76 s and 275 s; 45 s exceeds the old 40 s cap.
        del self.config['index_stop_grace_seconds']
        def sample(pid):
            if (self.root / 'helper-ready').exists() and (self.root / 'leader-ready').exists():
                raise KeyboardInterrupt
            return 0
        started = time.monotonic()
        with mock.patch('lore_mirror.sync._aggregate_rss', side_effect=sample), \
                self.assertRaises(KeyboardInterrupt) as raised:
            self.run_leader(flush=45, graceful=True)
        self.assertGreaterEqual(time.monotonic() - started, 45)
        self.assertTrue(raised.exception.lore_clean_stop)
        self.assertEqual((self.root / 'checkpoint').read_text(), 'alive')
        self.assertFalse((self.root / 'helper-term').exists())

    def test_daemon_stop_during_error_stop_shares_its_deadline(self):
        self.config.update(job_timeout_seconds=1, index_stop_grace_seconds=4)
        def interrupt(*_):
            raise KeyboardInterrupt
        previous = signal.signal(signal.SIGALRM, interrupt)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.alarm(4)
        self.addCleanup(signal.alarm, 0)
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.run_leader('ignore', graceful=True)
        # Error stop starts near 1 s with a 4 s grace; a fresh wait would end near 8 s.
        self.assertLess(time.monotonic() - started, 6.5)
        self.assertFalse(raised.exception.lore_clean_stop)
        self.assertFalse((self.root / 'checkpoint').exists())

    def test_non_graceful_error_still_signals_whole_group(self):
        self.config['job_timeout_seconds'] = 1
        with self.assertRaisesRegex(SyncError, 'deadline'):
            self.run_leader()
        # Leader and helper were both signalled; the helper recorded it.
        deadline = time.monotonic() + 5
        while not (self.root / 'helper-term').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((self.root / 'helper-term').exists())


class UncleanExitRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'extindex': str(Path(self.temp.name) / 'extindex')}
        self.db = Path(self.config['extindex']) / 'ei15' / 'over.sqlite3'

    def create(self, docs, xref, queued=(), lock=''):
        self.db.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''CREATE TABLE over (num INTEGER PRIMARY KEY NOT NULL);
                CREATE TABLE xref3 (docid INTEGER NOT NULL, ibx_id INTEGER NOT NULL, xnum INTEGER NOT NULL, oidbin BLOB NOT NULL);
                CREATE TABLE eidxq (docid INTEGER PRIMARY KEY NOT NULL);
                CREATE TABLE eidx_meta (key VARCHAR(255) PRIMARY KEY, val VARCHAR(255) NOT NULL);''')
            conn.executemany('INSERT INTO over VALUES (?)', [(n,) for n in docs])
            conn.executemany('INSERT INTO xref3 VALUES (?,?,?,?)', [(d, i, x, b'o') for d, i, x in xref])
            conn.executemany('INSERT INTO eidxq VALUES (?)', [(n,) for n in queued])
            conn.execute("INSERT INTO eidx_meta VALUES ('eidxq_lock', ?)", (lock,))
            conn.commit()

    def queue(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return [r[0] for r in conn.execute('SELECT docid FROM eidxq ORDER BY docid')], \
                conn.execute("SELECT val FROM eidx_meta WHERE key='eidxq_lock'").fetchone()[0]

    def test_missing_index_has_zero_floor_and_nothing_to_queue(self):
        self.assertEqual(extindex_floor(self.config), {'over_num': 0, 'xref3_rowid': 0, 'eidxq': 0})
        self.assertEqual(requeue_after_unclean_exit(self.config, {'over_num': 0, 'xref3_rowid': 0}), 0)

    def test_requeues_new_and_updated_documents_and_clears_stale_lock(self):
        # Documents 1-3 and three xref3 rows predate the floor. The lost
        # window added document 4 and attached old document 2 to inbox 9.
        self.create([1, 2, 3], [(1, 1, 1), (2, 1, 2), (3, 1, 3)], queued=[1])
        floor = extindex_floor(self.config)
        self.assertEqual(floor, {'over_num': 3, 'xref3_rowid': 3, 'eidxq': 1})
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute('INSERT INTO over VALUES (4)')
            conn.executemany('INSERT INTO xref3 VALUES (?,?,?,?)', [(4, 1, 4, b'o'), (2, 9, 1, b'o')])
            conn.execute("UPDATE eidx_meta SET val='23-1700000000-10001-oldcontainer' WHERE key='eidxq_lock'")
            conn.commit()
        self.assertEqual(requeue_after_unclean_exit(self.config, floor), 2)
        self.assertEqual(self.queue(), ([1, 2, 4], ''))
        self.assertEqual(requeue_after_unclean_exit(self.config, floor), 0)


class RepairSchedulingTests(fixtures.RefreshHarness):
    def setUp(self):
        super().setUp()
        self.names = ['hot', 'big', 'tail']
        self.priority = []

    def test_unclean_exit_requeues_from_floor_before_next_extindex(self):
        # The first floor is clean; every later one has queued repairs.
        floors = [{'over_num': 10, 'xref3_rowid': 12, 'eidxq': 0}]
        def floor(config):
            return floors.pop(0) if floors else {'over_num': 50, 'xref3_rowid': 60, 'eidxq': 5}
        requeued = []
        self.failure = ('extindex', 'big', KeyboardInterrupt())
        with mock.patch('lore_mirror.sync.extindex_floor', side_effect=floor), \
                mock.patch('lore_mirror.sync.requeue_after_unclean_exit',
                    side_effect=lambda config, floor: requeued.append(floor) or 7):
            interrupted = self.full_pass()
            self.assertTrue(interrupted['extindex_running'])
            self.assertEqual(interrupted['extindex_floor']['over_num'], 10)
            self.failure = None
            recovered = self.full_pass()
        # Only the first unclean restart repairs; the next floor has queued
        # repairs, so the older floor is kept until the queue drains.
        self.assertEqual(requeued, [{'over_num': 10, 'xref3_rowid': 12, 'eidxq': 0}])
        self.assertEqual(recovered['extindex_recovery']['requeued_documents'], 7)
        self.assertEqual(recovered['extindex_floor']['over_num'], 10)
        self.assertFalse(recovered['extindex_running'])
        self.assertFalse(recovered['lists']['big']['index_dirty'])

    def test_clean_daemon_stop_needs_no_repair(self):
        stop = KeyboardInterrupt()
        stop.lore_clean_stop = True
        self.failure = ('extindex', 'big', stop)
        with mock.patch('lore_mirror.sync.requeue_after_unclean_exit') as requeue:
            interrupted = self.full_pass()
            self.assertFalse(interrupted['extindex_running'])
            self.assertTrue(interrupted['lists']['big']['index_dirty'])
            self.failure = None
            self.full_pass()
        requeue.assert_not_called()

    def test_failed_extindex_is_unclean(self):
        self.names = ['hot', 'tail', 'big']
        self.failure = ('extindex', 'big', SyncError('command exited 2'))
        self.assertTrue(self.full_pass()['extindex_running'])

    def test_failed_basic_index_needs_no_repair(self):
        self.names = ['hot', 'tail', 'big']
        self.failure = ('index', 'big', SyncError('command exited 2'))
        state = self.full_pass()
        self.assertFalse(state['extindex_running'])
        self.assertTrue(state['lists']['big']['index_dirty'])


if __name__ == '__main__':
    unittest.main()
