"""Bounded index slices: process control and scheduling between slices."""
import fcntl
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest

from lore_mirror.sync import MAX_STOP_GRACE, SyncError, prepare_config, run_command
from tests import test_extindex_repair as repair
from tests import test_priority_refresh as fixtures


class RunCommandSliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'data_dir': str(self.root), 'min_free_fraction': 0,
            'job_timeout_seconds': 60, 'index_slice_grace_seconds': 5, 'index_stop_grace_seconds': 5}

    def run_leader(self, mode='quit', **kwargs):
        argv = [sys.executable, '-c', repair.LEADER, str(self.root), mode]
        return run_command(argv, self.config, self.root / 'logs' / 'job.log', **kwargs)

    def test_slice_signals_leader_only_and_returns_sliced(self):
        started = time.monotonic()
        result = self.run_leader(slice_seconds=1, graceful=True)
        self.assertTrue(result['sliced'])
        self.assertEqual(result['returncode'], 0)
        self.assertGreaterEqual(time.monotonic() - started, 1)
        self.assertEqual((self.root / 'checkpoint').read_text(), 'alive')
        self.assertFalse((self.root / 'helper-term').exists())
        self.assertIn('slice_end_requested', (self.root / 'logs' / 'job.log').read_text())

    def test_unsliced_job_has_no_slice_marker(self):
        argv = [sys.executable, '-c', 'pass']
        result = run_command(argv, self.config, self.root / 'job.log', slice_seconds=30, graceful=True)
        self.assertNotIn('sliced', result)

    def test_slice_grace_overrun_kills_group_and_fails(self):
        self.config['index_slice_grace_seconds'] = 1
        with self.assertRaisesRegex(SyncError, 'did not stop within'):
            self.run_leader('ignore', slice_seconds=1, graceful=True)
        self.assertFalse((self.root / 'checkpoint').exists())

class SliceConfigTests(unittest.TestCase):
    def test_slice_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            base = {'data_dir': tmp, 'lists': [{'id': 'one'}]}
            path.write_text(json.dumps(base))
            config = prepare_config(path)
            self.assertEqual((config['index_slice_seconds'], config['index_slice_grace_seconds'],
                config['index_stop_grace_seconds']), (3600, 900, 300))
            for good in (41, 300, 540):
                with self.subTest(good=good):
                    path.write_text(json.dumps(base | {'index_stop_grace_seconds': good}))
                    self.assertEqual(prepare_config(path)['index_stop_grace_seconds'], good)
            for bad in ({'index_slice_seconds': 59}, {'index_slice_seconds': 7200, 'job_timeout_seconds': 3600},
                    {'index_slice_grace_seconds': 0}, {'index_stop_grace_seconds': 0},
                    {'index_stop_grace_seconds': 541}):
                with self.subTest(bad=bad):
                    path.write_text(json.dumps(base | bad))
                    with self.assertRaises(SyncError):
                        prepare_config(path)

    def test_sync_compose_grace_outlasts_the_stop_cap(self):
        root = Path(__file__).resolve().parents[1]
        sync = re.search(r"^  sync:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", (root / 'compose.yaml').read_text(), re.M | re.S)
        grace = re.search(r"^    stop_grace_period: (\d+)s$", sync.group(1), re.M)
        self.assertGreaterEqual(int(grace.group(1)), MAX_STOP_GRACE + 60)
        for overlay in root.glob('compose.*.yaml'):
            with self.subTest(overlay=overlay.name):
                body = re.search(r"^  sync:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", overlay.read_text(), re.M | re.S)
                if body:
                    self.assertNotIn('stop_grace_period', body.group(1))


class SliceSchedulingTests(fixtures.RefreshHarness):
    def setUp(self):
        super().setUp()
        self.names = ['hot', 'big', 'tail']
        self.priority = ['hot']
        self.slices = {'big': 3}
        self.slice_failure = None
        self.calls = []
        self.lock_probe = None

    def command(self, argv, config, log, **kwargs):
        ident = Path(log).stem.split('-', 1)[1]
        step = argv[0].removeprefix('public-inbox-')
        self.calls.append((step, ident, kwargs.get('slice_seconds'), kwargs.get('graceful', False)))
        result = super().command(argv, config, log, **kwargs)
        if step == 'extindex' and self.slices.get(ident):
            self.slices[ident] -= 1
            if self.slice_failure == (ident, self.slices[ident]):
                raise self.slice_failure_exc
            self.now += 3600
            return dict(result, sliced=True)
        return result

    def test_priority_inbox_refreshes_between_slices(self):
        state = self.full_pass()
        self.assertEqual(self.clones(), ['big', 'hot', 'tail'])
        big = [(step, slice_seconds, graceful) for step, ident, slice_seconds, graceful in self.calls
            if ident == 'big' and step in ('index', 'extindex')]
        self.assertEqual(big, [('index', 3600, True)] + [('extindex', 3600, True)] * 4)
        # The hot refresh ran after the first slice, under its own lock
        # hold, while the importing inbox stayed dirty and unsafe.
        hot = [(step, ident, s) for step, ident, s, _ in self.calls if ident == 'hot']
        self.assertEqual(hot, [('clone', 'hot', None), ('init', 'hot', None),
            ('index', 'hot', None), ('extindex', 'hot', None)])
        position = self.calls.index(('clone', 'hot', None, False))
        self.assertEqual([c[:2] for c in self.calls[position - 1:position]], [('extindex', 'big')])
        _, _, during, _ = next(s for s in self.snapshots if s[:2] == ('extindex', 'hot'))
        self.assertTrue(during['lists']['big']['index_dirty'])
        self.assertTrue(during['unsafe_index'])
        self.assertEqual(during['lists']['big']['extindex']['completed'], False)
        self.assertEqual(during['lists']['big']['extindex']['slices'], 1)
        self.assertEqual(self.checks, [0, 3600, 7200, 10800])
        row = state['lists']['big']
        self.assertEqual(row['status'], 'ready')
        self.assertFalse(row['index_dirty'])
        self.assertEqual(row['extindex']['slices'], 4)
        self.assertNotIn('completed', row['extindex'])
        self.assertFalse(state['unsafe_index'])

    def manifest(self, config, state):
        if self.lock_probe is not None and self.checks:
            with (self.root / 'index.lock').open('rb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock_probe.append(json.loads((self.root / 'sync-state.json').read_text())['stage'])
        return super().manifest(config, state)

    def test_due_refresh_runs_before_resumed_import(self):
        # hot has new upstream mail; big was interrupted mid-import.
        self.rows['hot']['indexed_fingerprints'] = {'/hot/git/0.git': 'c' * 40}
        self.rows['big'] = {'status': 'indexing', 'index_dirty': True, 'fetched_at': self.timestamp(),
            'budget': {'complete': True, 'known_pack_bytes': 1}}
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state['last_priority_refresh_at'] = self.timestamp()
        fixtures.atomic_json(self.root / 'sync-state.json', state)
        self.now += 2 * 86400
        self.slices = {'big': 1}
        self.full_pass()
        steps = [(step, ident) for step, ident, _, _ in self.calls]
        self.assertLess(steps.index(('extindex', 'hot')), steps.index(('clone', 'big')))
        self.assertEqual(self.checks, [2 * 86400, 2 * 86400 + 3600])

    def test_priority_refresh_follows_priority_lists_order(self):
        self.names = ['hot', 'big', 'warm', 'tail']
        self.priority = ['warm', 'hot']
        self.rows['warm'] = self.indexed_row('warm')
        self.full_pass()
        refreshed = [ident for step, ident, _, _ in self.calls if step == 'clone' and ident in ('hot', 'warm')]
        self.assertEqual(refreshed[:2], ['warm', 'hot'])

    def test_lock_is_released_between_slices(self):
        self.lock_probe = []
        self.full_pass()
        self.assertEqual(self.lock_probe, ['between_slices'] * 3)

    def test_priority_refresh_and_priority_cycle_never_slice(self):
        self.names = ['hot', 'tail']
        self.slices = {'hot': 2}
        self.rows['hot']['index_dirty'] = False
        self.now += 300
        self.checks.append(0)
        from lore_mirror.sync import sync_once
        sync_once(prepare_config(self.config()), full=False)
        self.assertEqual({(s, g) for step, ident, s, g in self.calls if step in ('index', 'extindex')}, {(None, True)})
        self.assertEqual(self.slices['hot'], 0)

    def test_crash_mid_slice_resumes_same_inbox_first(self):
        # tail failed earlier and precedes big in the configured order.
        self.names = ['hot', 'tail', 'big']
        self.failure = ('clone', 'tail', SyncError('fetch failed'))
        self.slice_failure = ('big', 1)
        self.slice_failure_exc = KeyboardInterrupt()
        interrupted = self.full_pass()
        self.failure = None
        self.assertTrue(interrupted['lists']['big']['index_dirty'])
        self.assertTrue(interrupted['unsafe_index'])
        self.assertEqual(interrupted['lists']['big']['extindex']['slices'], 1)
        self.commands.clear()
        self.calls.clear()
        self.slice_failure = None
        recovered = self.full_pass()
        self.assertEqual(self.clones()[0], 'big')
        self.assertFalse(recovered['lists']['big']['index_dirty'])
        self.assertFalse(recovered['unsafe_index'])
        self.assertEqual(recovered['lists']['tail']['status'], 'ready')

    def test_failed_slice_records_error_and_pass_continues(self):
        self.slice_failure = ('big', 1)
        self.slice_failure_exc = SyncError('slice did not stop within index_slice_grace_seconds')
        state = self.full_pass()
        row = state['lists']['big']
        self.assertEqual(row['status'], 'error')
        self.assertIn('did not stop', row['last_error'])
        self.assertTrue(row['index_dirty'])
        self.assertEqual(state['lists']['tail']['status'], 'ready')
        self.assertEqual(state['pending_lists'], ['big'])
        self.assertEqual(state['stage'], 'recovery_required')


if __name__ == '__main__':
    unittest.main()
