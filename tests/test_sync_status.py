"""Status honesty: resolved errors clear and move to a bounded history."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lore_mirror.service import MirrorService
from lore_mirror.sync import SyncError, atomic_json, clear_error, prepare_config, set_error, sync_once
from tests import test_priority_refresh as fixtures

DNS = '<urlopen error [Errno -3] Temporary failure in name resolution>'


class ResolvedErrorTests(fixtures.RefreshHarness):
    def setUp(self):
        super().setUp()
        self.manifest_errors = {}
        # Without priority lists every retry is a full cycle.
        self.priority = []

    def manifest(self, config, state):
        error = self.manifest_errors.get(len(self.checks) + 1)
        if error:
            self.checks.append(self.now - self.started)
            raise error
        return super().manifest(config, state)

    def run_cycles(self, cycles, between=None):
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == cycles:
                raise KeyboardInterrupt
            self.now += seconds
            if between:
                between(seconds)
        with mock.patch('lore_mirror.sync.time.sleep', side_effect=sleep):
            state = self.full_pass()
        return state, sleeps

    def test_dns_failure_clears_on_next_manifest_fetch_mid_cycle(self):
        self.manifest_errors[1] = OSError(DNS)
        self.durations = {'slow': 7200}
        state, sleeps = self.run_cycles(2)
        self.assertEqual(sleeps[0], 60)
        # The long list runs after the fresh manifest succeeded: the stale
        # error must already be gone while the cycle is still in progress.
        _, _, during, _ = next(s for s in self.snapshots if s[:2] == ('extindex', 'slow'))
        self.assertEqual(during['status'], 'syncing')
        self.assertNotIn('last_error', during)
        self.assertNotIn('last_error_at', during)
        [entry] = during['error_history']
        self.assertEqual(entry['error'], DNS)
        self.assertEqual(entry['cleared_by'], 'manifest fetch')
        self.assertEqual(entry['at'], self.timestamp_at(self.started))
        self.assertEqual(entry['cleared_at'], self.timestamp_at(self.started + 60))
        self.assertNotIn('last_error', state)

    def test_repeated_failure_keeps_first_occurrence_and_no_history(self):
        self.manifest_errors.update({1: OSError(DNS), 2: OSError(DNS)})
        state, sleeps = self.run_cycles(2)
        self.assertEqual(sleeps, [60, 120])
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['last_error'], DNS)
        self.assertEqual(state['last_error_at'], self.timestamp_at(self.started))
        self.assertNotIn('error_history', state)

    def test_different_failure_supersedes_and_is_retained(self):
        self.manifest_errors.update({1: OSError(DNS), 2: SyncError('manifest shrank')})
        state, _ = self.run_cycles(2)
        self.assertEqual(state['last_error'], 'manifest shrank')
        self.assertEqual(state['last_error_at'], self.timestamp_at(self.started + 60))
        self.assertEqual([(h['error'], h['cleared_by']) for h in state['error_history']],
            [(DNS, 'superseded')])

    def test_list_error_survives_retry_until_its_fetch_succeeds(self):
        self.rows['slow'] = self.indexed_row('slow', status='error', last_error='fetch failed',
            last_error_at='2025-12-31T00:00:00+00:00')
        self.failure = ('clone', 'slow', SyncError('fetch failed again'))
        failed = self.full_pass()
        row = failed['lists']['slow']
        self.assertEqual(row['last_error'], 'fetch failed again')
        self.assertEqual(row['error_history'][-1]['cleared_by'], 'superseded')
        self.failure = None
        self.commands.clear()
        self.snapshots.clear()
        recovered = self.full_pass()
        # While the retry runs the previous failure is still the truth.
        _, _, retrying, _ = next(s for s in self.snapshots if s[:2] == ('clone', 'slow'))
        self.assertEqual(retrying['lists']['slow']['status'], 'fetching')
        self.assertEqual(retrying['lists']['slow']['last_error'], 'fetch failed again')
        row = recovered['lists']['slow']
        self.assertNotIn('last_error', row)
        self.assertEqual(row['error_history'][-1]['cleared_by'], 'fetch')
        self.assertEqual(row['status'], 'ready')

    def stale_state_error(self):
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state.update(last_error=DNS, last_error_at=self.timestamp())
        atomic_json(self.root / 'sync-state.json', state)

    def test_non_manifest_cycle_failure_survives_next_manifest_fetch(self):
        failures = [OSError('No space left on device')]
        def history_write(path, value):
            if Path(path).name == 'observations.json' and failures:
                raise failures.pop()
            return atomic_json(path, value)
        # tail fails its first fetch, so the second cycle has work to observe.
        self.failure = ('clone', 'tail', SyncError('fetch failed'))
        def sleep_hook(seconds):
            self.failure = None
        with mock.patch('lore_mirror.sync.atomic_json', side_effect=history_write):
            state, _ = self.run_cycles(2, between=sleep_hook)
        _, _, during, _ = [s for s in self.snapshots if s[:2] == ('clone', 'tail')][-1]
        self.assertEqual(during['last_error'], 'No space left on device')
        self.assertEqual(during['last_error_step'], 'cycle')
        self.assertNotIn('last_error', state)
        self.assertEqual(state['error_history'][-1]['cleared_by'], 'cycle')

    def test_index_failure_survives_fetch_until_index_succeeds(self):
        self.failure = ('extindex', 'slow', SyncError('extindex exited 2'))
        self.full_pass()
        self.failure = None
        self.snapshots.clear()
        recovered = self.full_pass()
        _, _, indexing, _ = next(s for s in self.snapshots if s[:2] == ('extindex', 'slow'))
        self.assertEqual(indexing['lists']['slow']['last_error'], 'extindex exited 2')
        self.assertEqual(indexing['lists']['slow']['last_error_step'], 'index')
        self.assertNotIn('last_error', recovered['lists']['slow'])
        self.assertEqual(recovered['lists']['slow']['error_history'][-1]['cleared_by'], 'index')

    def test_good_fetch_clears_preflight_and_namespace_errors(self):
        for step, error in (('preflight', 'storage preflight requires 1 additional bytes'),
                ('manifest', 'namespace absent from validated manifest')):
            with self.subTest(step=step):
                (self.root / 'sync-state.json').unlink(missing_ok=True)
                self.rows['slow'] = self.indexed_row('slow', status='error', last_error=error,
                    last_error_at='2025-12-31T00:00:00+00:00', last_error_step=step)
                row = self.full_pass()['lists']['slow']
                self.assertNotIn('last_error', row)
                self.assertEqual((row['error_history'][-1]['step'], row['error_history'][-1]['cleared_by']),
                    (step, 'fetch'))

    def test_priority_manifest_success_clears_state_error(self):
        self.priority = ['hot']
        self.stale_state_error()
        self.now += 300
        state = sync_once(prepare_config(self.config()), full=False)
        self.assertNotIn('last_error', state)
        self.assertEqual(state['error_history'][-1]['cleared_by'], 'priority manifest fetch')

    def test_priority_manifest_failure_keeps_state_error(self):
        self.priority = ['hot']
        self.stale_state_error()
        self.manifest_errors[1] = OSError(DNS)
        state = sync_once(prepare_config(self.config()), full=False)
        self.assertEqual(state['last_error'], DNS)
        self.assertNotIn('error_history', state)
        self.assertEqual(state['lists']['hot']['last_error'], DNS)

    def test_priority_manifest_success_keeps_cycle_error(self):
        self.priority = ['hot']
        self.stale_state_error()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state['last_error_step'] = 'cycle'
        atomic_json(self.root / 'sync-state.json', state)
        self.now += 300
        # hot changes upstream and fails, so the cycle cannot clear the error.
        self.checks.append(0)
        self.failure = ('clone', 'hot', SyncError('fetch failed'))
        state = sync_once(prepare_config(self.config()), full=False)
        self.assertEqual(state['status'], 'partial')
        self.assertEqual((state['last_error'], state['last_error_step']), (DNS, 'cycle'))
        self.assertNotIn('error_history', state)

    def timestamp_at(self, value):
        import datetime as dt
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


class ErrorRecordTests(unittest.TestCase):
    def test_success_only_clears_the_failed_step_kind(self):
        record = {}
        set_error(record, 'disk full', step='cycle')
        clear_error(record, 'manifest fetch', steps={'manifest'})
        self.assertEqual((record['last_error'], record['last_error_step']), ('disk full', 'cycle'))
        clear_error(record, 'cycle')
        self.assertNotIn('last_error', record)
        self.assertEqual(record['error_history'][-1]['step'], 'cycle')
        # Errors written before steps were recorded clear on any success.
        record.update(last_error='legacy')
        clear_error(record, 'manifest fetch', steps={'manifest'})
        self.assertNotIn('last_error', record)

    def test_history_is_bounded_and_clear_without_error_is_noop(self):
        record = {}
        clear_error(record, 'fetch')
        self.assertEqual(record, {})
        for number in range(8):
            set_error(record, f'failure {number}', step='fetch', limit=5)
        clear_error(record, 'fetch', limit=5)
        self.assertNotIn('last_error', record)
        self.assertEqual([h['error'] for h in record['error_history']],
            [f'failure {n}' for n in range(3, 8)])


class StatusErrorFieldTests(unittest.TestCase):
    def test_status_exposes_bounded_error_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MirrorService({'data_dir': tmp, 'backend': 'fixture'})
            history = [{'error': 'x' * 2000, 'at': 'a', 'cleared_at': 'b', 'cleared_by': 'fetch',
                'path': '/secret'} for _ in range(9)] + ['junk']
            atomic_json(Path(tmp) / 'sync-state.json', {'status': 'syncing',
                'error_history': history, 'last_error_at': 'c', 'last_error_step': 'cycle'})
            sync = service.status()['sync']
        self.assertNotIn('last_error', sync)
        self.assertEqual((sync['last_error_at'], sync['last_error_step']), ('c', 'cycle'))
        self.assertEqual(len(sync['error_history']), 4)
        self.assertEqual(set(sync['error_history'][0]), {'error', 'at', 'step', 'cleared_at', 'cleared_by'})
        self.assertEqual(len(sync['error_history'][0]['error']), 300)


if __name__ == '__main__':
    unittest.main()
