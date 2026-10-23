"""Offline clock-driven checkpoints during long full sync passes."""
from contextlib import ExitStack
import datetime as dt
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lore_mirror.sync import SyncError, atomic_json, prepare_config, run_sync, sync_once


class RefreshHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
        self.started = self.now
        self.names = ['hot', 'slow', 'cold', 'tail']
        self.priority = ['hot', 'cold']
        self.rows = {'hot': self.indexed_row('hot')}
        self.commands, self.checks, self.snapshots = [], [], []
        self.durations = {'slow': 300}
        self.failure = None
        self.manifest_failure = False
        patches = self.enterContext(ExitStack())
        patches.enter_context(mock.patch('lore_mirror.sync.time.time', side_effect=lambda: self.now))
        patches.enter_context(mock.patch('lore_mirror.sync.utcnow', side_effect=self.timestamp))
        patches.enter_context(mock.patch('lore_mirror.sync.fetch_manifest', side_effect=self.manifest))
        patches.enter_context(mock.patch('lore_mirror.sync.pack_budget',
            return_value={'complete': True, 'known_pack_bytes': 1}))
        patches.enter_context(mock.patch('lore_mirror.sync.storage_available', return_value=1024**3))
        patches.enter_context(mock.patch('lore_mirror.sync._refs', return_value={}))
        patches.enter_context(mock.patch('lore_mirror.sync.message_count', return_value=2))
        patches.enter_context(mock.patch('lore_mirror.sync.run_command', side_effect=self.command))
        patches.enter_context(mock.patch('lore_mirror.sync.signal.signal'))
        patches.enter_context(mock.patch('lore_mirror.sync.time.sleep', side_effect=KeyboardInterrupt))
        patches.enter_context(mock.patch('builtins.print'))

    def timestamp(self):
        return dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()

    def indexed_row(self, ident, **extra):
        return {'status': 'ready', 'indexed_at': self.timestamp(), 'fetched_at': self.timestamp(),
            'indexed_fingerprints': {f'/{ident}/git/0.git': 'a' * 40},
            'budget': {'complete': True, 'known_pack_bytes': 1}, **extra}

    def manifest(self, config, state):
        self.checks.append(self.now - self.started)
        if self.manifest_failure and len(self.checks) == 2:
            raise OSError('priority manifest unavailable')
        return {f'/{name}/git/0.git': {'fingerprint':
            ('b' if len(self.checks) > 1 and name in self.priority else 'a') * 40}
            for name in self.names}

    def command(self, argv, config, log, **kwargs):
        ident = Path(log).stem.split('-', 1)[1]
        step = argv[0].removeprefix('public-inbox-')
        self.commands.append((step, ident))
        state = json.loads((self.root / 'sync-state.json').read_text())
        with (self.root / 'index.lock').open('rb') as lock:
            if step == 'clone':
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            else:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self.assertTrue(state['lists'][ident]['index_dirty'])
                self.assertTrue(state['unsafe_index'])
        daemon_path = self.root / 'daemon-state.json'
        daemon = json.loads(daemon_path.read_text()) if daemon_path.exists() else None
        self.snapshots.append((step, ident, state, daemon))
        if self.failure and (step, ident) == self.failure[:2]:
            raise self.failure[2]
        if step == 'extindex':
            self.now += self.durations.get(ident, 0)
        return {'seconds': .1, 'peak_aggregate_rss_bytes_sampled': 100, 'returncode': 0}

    def config(self):
        for name in self.names:
            (self.root / 'inboxes' / name / 'git/0.git').mkdir(parents=True, exist_ok=True)
        path = self.root / 'config.json'
        path.write_text(json.dumps({'data_dir': str(self.root), 'lists': [{'id': name} for name in self.names],
            'priority_lists': self.priority, 'priority_interval_seconds': 300}))
        if not (self.root / 'sync-state.json').exists():
            atomic_json(self.root / 'sync-state.json', {'generation': 1, 'lists': self.rows})
        return path

    def full_pass(self):
        with self.assertRaises(KeyboardInterrupt):
            run_sync(self.config(), once=False)
        return json.loads((self.root / 'sync-state.json').read_text())

    def clones(self):
        return [ident for step, ident in self.commands if step == 'clone']


class PriorityRefreshTests(RefreshHarness):
    def test_mid_pass_refreshes_only_completed_clean_priorities(self):
        self.names = ['hot', 'slow', 'other', 'cold', 'dirty', 'tail']
        self.priority = ['hot', 'other', 'cold', 'dirty']
        self.rows['other'] = self.indexed_row('other', status='error', last_error='old fetch failure')
        self.rows['dirty'] = self.indexed_row('dirty', index_dirty=True)
        state = self.full_pass()
        self.assertEqual(self.clones(), ['slow', 'hot', 'other', 'cold', 'dirty', 'tail'])
        self.assertEqual(self.checks, [0, 300])
        self.assertEqual(state['lists']['other']['indexed_fingerprints'], {'/other/git/0.git': 'b' * 40})
        self.assertFalse(state['unsafe_index'])
        self.assertEqual(state['pending_lists'], [])
        # The refresh updates both files before the outer pass resumes.
        _, _, checkpoint, daemon = next(s for s in self.snapshots if s[:2] == ('clone', 'cold'))
        self.assertEqual(checkpoint['last_priority_refresh_at'], self.timestamp())
        self.assertEqual(daemon['last_priority_refresh_at'], checkpoint['last_priority_refresh_at'])
        self.assertEqual(daemon['cycles_completed'], 0)
        self.assertEqual(checkpoint['cycle_started_at'], dt.datetime.fromtimestamp(self.started, dt.timezone.utc).isoformat())
        final_daemon = json.loads((self.root / 'daemon-state.json').read_text())
        self.assertEqual(final_daemon['last_priority_refresh_at'], checkpoint['last_priority_refresh_at'])
        self.assertEqual(final_daemon['cycles_completed'], 1)
        self.assertEqual(len(json.loads((self.root / 'state/observations.json').read_text())), 1)

    def test_unchanged_outer_list_still_runs_due_checkpoint(self):
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state['last_priority_refresh_at'] = self.timestamp()
        atomic_json(self.root / 'sync-state.json', state)
        self.now += 300
        self.durations.clear()
        self.full_pass()
        self.assertEqual(self.checks, [300, 300])
        self.assertEqual(self.clones(), ['hot', 'slow', 'cold', 'tail'])

    def test_no_refresh_before_interval(self):
        self.durations['slow'] = 299
        state = self.full_pass()
        self.assertEqual(self.clones(), ['slow', 'cold', 'tail'])
        self.assertEqual(self.checks, [0])
        self.assertIsNone(state['last_priority_refresh_at'])

    def test_persisted_refresh_time_delays_next_checkpoint(self):
        self.rows['slow'] = self.indexed_row('slow')
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state['last_priority_refresh_at'] = self.timestamp()
        atomic_json(self.root / 'sync-state.json', state)
        self.now += 299
        self.durations = {'cold': 1}
        result = self.full_pass()
        self.assertEqual(self.checks, [299, 300])
        self.assertEqual(self.clones(), ['cold', 'hot', 'cold', 'tail'])
        self.assertEqual(result['last_priority_refresh_status'], 'ready')

    def test_manifest_error_records_row_and_retries_without_aborting_pass(self):
        self.names = ['hot', 'slow', 'short', 'due', 'tail']
        self.priority = ['hot']
        self.durations = {'slow': 300, 'short': 59, 'due': 1}
        self.manifest_failure = True
        state = self.full_pass()
        self.assertEqual(self.checks, [0, 300, 360])
        self.assertEqual(self.clones(), ['slow', 'short', 'due', 'hot', 'tail'])
        _, _, failed, daemon = next(s for s in self.snapshots if s[:2] == ('clone', 'short'))
        row = failed['lists']['hot']
        self.assertEqual(row['last_error'], 'priority manifest unavailable')
        self.assertEqual(row['retry_count'], 1)
        self.assertEqual(dt.datetime.fromisoformat(row['next_retry_at']).timestamp(), self.started + 360)
        self.assertEqual(daemon['last_priority_refresh_status'], 'partial')
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['pending_lists'], [])
        self.assertEqual(state['lists']['hot']['retry_count'], 0)

    def test_failed_priority_index_stays_dirty_until_next_full_pass(self):
        self.durations['tail'] = 300
        self.failure = ('extindex', 'hot', SyncError('priority index interrupted'))
        state = self.full_pass()
        self.assertEqual(self.clones(), ['slow', 'hot', 'cold', 'tail'])
        self.assertEqual(self.checks, [0, 300, 600])
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['pending_lists'], ['hot'])
        self.assertTrue(state['lists']['hot']['index_dirty'])
        self.assertTrue(state['unsafe_index'])
        self.assertEqual(state['stage'], 'recovery_required')
        self.failure = None
        self.commands.clear()
        self.durations.clear()
        recovered = self.full_pass()
        self.assertEqual(self.clones()[0], 'hot')
        self.assertFalse(recovered['lists']['hot']['index_dirty'])
        self.assertFalse(recovered['unsafe_index'])
        self.assertEqual(recovered['status'], 'ready')

    def test_embedded_refresh_preserves_start_to_start_full_schedule(self):
        self.durations = {'slow': 3300, 'hot': 100}
        sleeps, starts, scopes = [], [], []
        def sleep(seconds):
            sleeps.append(seconds)
            state = json.loads((self.root / 'sync-state.json').read_text())
            daemon = json.loads((self.root / 'daemon-state.json').read_text())
            starts.append(dt.datetime.fromisoformat(state['cycle_started_at']).timestamp() - self.started)
            scopes.append(daemon['last_cycle_scope'])
            if len(sleeps) == 2:
                raise KeyboardInterrupt
            self.now += seconds
        with mock.patch('lore_mirror.sync.time.sleep', side_effect=sleep):
            self.full_pass()
        self.assertEqual(sleeps, [200, 100])
        self.assertEqual(starts, [0, 3600])
        self.assertEqual(scopes, ['full', 'full'])

    def test_final_checkpoint_failure_keeps_retry_deadline_between_passes(self):
        self.names = ['hot', 'slow']
        self.priority = ['hot']
        self.manifest_failure = True
        with mock.patch('lore_mirror.sync.time.sleep', side_effect=KeyboardInterrupt) as sleep:
            state = self.full_pass()
        sleep.assert_called_once_with(60)
        self.assertEqual(state['priority_consecutive_errors'], 1)
        self.assertEqual(dt.datetime.fromisoformat(state['next_priority_refresh_at']).timestamp(),
            self.now + 60)

    def test_full_pass_boundary_does_not_shorten_long_priority_backoff(self):
        self.names = ['hot', 'slow']
        self.priority = ['hot']
        self.manifest_failure = True
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state.update(priority_consecutive_errors=3, last_priority_refresh_at=self.timestamp())
        atomic_json(self.root / 'sync-state.json', state)
        with mock.patch('lore_mirror.sync.time.sleep', side_effect=KeyboardInterrupt) as sleep:
            result = self.full_pass()
        sleep.assert_called_once_with(900)
        self.assertEqual(result['priority_consecutive_errors'], 4)

    def test_full_pass_tail_does_not_reset_priority_deadline(self):
        self.durations['tail'] = 299
        with mock.patch('lore_mirror.sync.time.sleep', side_effect=KeyboardInterrupt) as sleep:
            self.full_pass()
        sleep.assert_called_once_with(1)

    def test_priority_backoff_survives_cycle_and_restart_boundaries(self):
        self.names = ['hot', 'slow']
        self.priority = ['hot']
        self.manifest_failure = True
        first = self.full_pass()
        self.assertEqual(first['priority_consecutive_errors'], 1)
        self.now += 60
        self.checks = [0]
        second = sync_once(prepare_config(self.config()), full=False)
        self.assertEqual(second['priority_consecutive_errors'], 2)
        self.assertEqual(dt.datetime.fromisoformat(second['next_priority_refresh_at']).timestamp(),
            self.now + 120)

    def test_no_eligible_priority_does_not_leave_an_overdue_deadline(self):
        self.names = ['hot']
        self.priority = ['hot']
        self.rows['hot']['index_dirty'] = True
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state['next_priority_refresh_at'] = self.timestamp()
        atomic_json(self.root / 'sync-state.json', state)
        self.now += 1
        result = sync_once(prepare_config(self.config()), full=False)
        self.assertEqual(self.checks, [])
        self.assertEqual(dt.datetime.fromisoformat(result['next_priority_refresh_at']).timestamp(),
            self.now + 300)

    def test_power_cut_during_priority_index_recovers_on_restart(self):
        self.failure = ('extindex', 'hot', KeyboardInterrupt())
        interrupted = self.full_pass()
        self.assertEqual(self.clones(), ['slow', 'hot'])
        self.assertTrue(interrupted['lists']['hot']['index_dirty'])
        self.assertTrue(interrupted['unsafe_index'])
        self.failure = None
        self.durations.clear()
        self.commands.clear()
        recovered = self.full_pass()
        self.assertEqual(self.clones()[0], 'hot')
        self.assertFalse(recovered['unsafe_index'])
        self.assertEqual(recovered['lists']['tail']['status'], 'ready')

    def test_priority_only_cycle_never_bootstraps_or_repairs_dirty_lists(self):
        self.names = ['hot', 'cold', 'dirty']
        self.priority = list(self.names)
        self.rows['dirty'] = self.indexed_row('dirty', index_dirty=True)
        self.checks.append(0)
        state = sync_once(prepare_config(self.config()), full=False)
        self.assertEqual(self.clones(), ['hot'])
        self.assertNotIn('cold', state['lists'])
        self.assertTrue(state['lists']['dirty']['index_dirty'])
        self.assertTrue(state['unsafe_index'])
        self.assertEqual(state['last_priority_refresh_at'], self.timestamp())


if __name__ == '__main__':
    unittest.main()
