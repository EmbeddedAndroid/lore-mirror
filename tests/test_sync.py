"""Offline policy/durability tests: these never contact an archive server."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lore_mirror.sync import (SyncError, atomic_json, prepare_config,
    storage_available, validate_manifest, pack_budget, sync_once, run_sync)


class SyncSafetyTests(unittest.TestCase):
    def test_valid_manifest(self):
        row = {'/list/git/0.git': {'fingerprint': 'a' * 40}}
        self.assertEqual(validate_manifest(gzip.compress(json.dumps(row).encode())), row)

    def test_unsafe_manifest(self):
        for name in ('/../../escape', 'https://bad.invalid/git/0.git', '/list/git/../0.git'):
            with self.assertRaises(SyncError):
                validate_manifest(gzip.compress(json.dumps({name: {}}).encode()))

    def test_expansion_limit(self):
        with mock.patch('lore_mirror.sync.MAX_MANIFEST_PLAIN', 100):
            with self.assertRaises(SyncError):
                validate_manifest(gzip.compress(b' ' * 101))

    def test_atomic_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'state.json'
            atomic_json(target, {'generation': 1})
            atomic_json(target, {'generation': 2})
            self.assertEqual(json.loads(target.read_text()), {'generation': 2})
            self.assertEqual(list(Path(tmp).glob('*.tmp.*')), [])

    def test_reserve_rejects_unsafe_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            for options in ({'max_workers': 3}, {'min_free_fraction': .1},
                {'lists': [{'id': '../escape'}]}):
                path.write_text(json.dumps({'data_dir': tmp, 'lists': [{'id': 'test'}], **options}))
                with self.assertRaises(SyncError):
                    prepare_config(path)

    def test_no_packs_is_unknown_not_zero_size(self):
        with mock.patch('lore_mirror.sync._request', return_value=(b'', {})):
            budget = pack_budget({'upstream': 'https://erol.kernel.org', 'user_agent': 'test'},
                ['/list/git/0.git'])
        self.assertFalse(budget['complete'])
        self.assertEqual(budget['known_pack_bytes'], 0)

    def test_advertised_pool_is_resolved_and_counted_once(self):
        key = 'd741f450-1437-4c0a-a8f0-d40b7ab6aa0b'
        def response(url, agent, **kwargs):
            if kwargs.get('method') == 'HEAD':
                return b'', {'Content-Length': '1000'}
            if '/objstore/' in url:
                return ('P pack-' + 'a'*40 + '.pack\n').encode(), {}
            if url.endswith('/info/alternates'):
                return f'/var/lib/git/erol.kernel.org/objstore/{key}.git/objects\n'.encode(), {}
            return b'', {}
        with mock.patch('lore_mirror.sync._request', side_effect=response) as request:
            budget = pack_budget({'upstream': 'https://erol.kernel.org', 'user_agent': 'test'},
                ['/list/git/0.git', '/list/git/1.git'])
        self.assertTrue(budget['complete'])
        self.assertEqual(budget['known_pack_bytes'], 1000)
        self.assertEqual(len(budget['unique_pools']), 1)
        self.assertEqual(sum(call.kwargs.get('method') == 'HEAD' for call in request.call_args_list), 1)

    def test_storage_headroom(self):
        usage = type('Usage', (), {'total': 1000, 'free': 400})()
        with mock.patch('lore_mirror.sync.shutil.disk_usage', return_value=usage):
            self.assertEqual(storage_available('/unused', .30), 100)

    def test_failed_extindex_blocks_reads_until_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / 'inboxes' / 'test'
            (inbox / 'git' / '0.git').mkdir(parents=True)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({'data_dir': tmp, 'lists': [{'id': 'test'}]}))
            cfg = prepare_config(config_path)
            done = {'seconds': .1, 'peak_aggregate_rss_bytes_sampled': 100, 'returncode': 0}
            with mock.patch('lore_mirror.sync.fetch_manifest', return_value={'/test/git/0.git': {'fingerprint': 'a'*40}}), \
                 mock.patch('lore_mirror.sync.pack_budget', return_value={'complete': True, 'known_pack_bytes': 1}), \
                 mock.patch('lore_mirror.sync._refs', return_value={}), \
                 mock.patch('lore_mirror.sync.message_count', return_value=0), \
                 mock.patch('lore_mirror.sync.run_command', side_effect=[done, done, done, SyncError('index crashed')]):
                state = sync_once(cfg)
            self.assertTrue(state['updating'])
            self.assertTrue(state['unsafe_index'])
            self.assertTrue(state['lists']['test']['index_dirty'])
            self.assertEqual(state['generation'], 0)
            with mock.patch('lore_mirror.sync.fetch_manifest', return_value={'/test/git/0.git': {'fingerprint': 'a'*40}}), \
                 mock.patch('lore_mirror.sync._refs', return_value={}), \
                 mock.patch('lore_mirror.sync.message_count', return_value=0), \
                 mock.patch('lore_mirror.sync.run_command', return_value=done):
                recovered = sync_once(cfg)
            self.assertFalse(recovered['updating'])
            self.assertFalse(recovered['unsafe_index'])
            self.assertEqual(recovered['generation'], 1)
            self.assertEqual(recovered['lists']['test']['status'], 'ready')

    def test_incremental_does_not_reserve_whole_cold_index_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'inboxes/test/git/0.git').mkdir(parents=True)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({'data_dir': tmp, 'lists': [{'id': 'test', 'class': 'kernel', 'size_class': 'large'}]}))
            cfg = prepare_config(config_path)
            gib = 1024**3
            initial = {'git_bytes': {'allocated': gib, 'apparent': gib},
                'basic_index_bytes': {'allocated': 0, 'apparent': 0},
                'extindex_increment_bytes': 4*gib, 'messages': 1}
            atomic_json(root / 'sync-state.json', {'generation': 1, 'lists': {'test': {
                'status': 'ready', 'indexed_at': '2026-01-01T00:00:00+00:00',
                'fetched_at': '2026-01-01T00:00:00+00:00',
                'indexed_fingerprints': {'/test/git/0.git': 'a'*40},
                'budget': {'complete': True, 'known_pack_bytes': gib},
                'measurements': initial}}})
            done = {'seconds': .1, 'peak_aggregate_rss_bytes_sampled': 100, 'returncode': 0}
            with mock.patch('lore_mirror.sync.fetch_manifest', return_value={'/test/git/0.git': {'fingerprint': 'b'*40}}), \
                 mock.patch('lore_mirror.sync.storage_available', return_value=300*1024**2), \
                 mock.patch('lore_mirror.sync.tree_bytes', return_value={'allocated': gib, 'apparent': gib}), \
                 mock.patch('lore_mirror.sync._refs', return_value={}), \
                 mock.patch('lore_mirror.sync.message_count', return_value=2), \
                 mock.patch('lore_mirror.sync.run_command', return_value=done) as command:
                state = sync_once(cfg)
            environment = command.call_args_list[0].kwargs['env']
            self.assertEqual(environment['GIT_CONFIG_NOSYSTEM'], '1')
            self.assertEqual(environment['GIT_CONFIG_GLOBAL'], '/dev/null')
            self.assertEqual(environment['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(environment['GIT_ASKPASS'], '/bin/false')
            self.assertEqual(environment['GIT_CONFIG_VALUE_2'], '')
            self.assertEqual(environment['HOME'], str(root / 'state/home'))
            self.assertEqual(state['status'], 'ready')
            self.assertEqual(state['lists']['test']['initial_measurements'], initial)
            self.assertEqual(state['lists']['test']['measurements']['messages'], 2)

    def test_daemon_records_pid_success_and_next_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'data_dir': tmp, 'lists': [{'id': 'test'}], 'interval_seconds': 3600}))
            result = {'status': 'ready', 'pending_lists': [], 'unsafe_index': False,
                'cycle_finished_at': '2026-01-01T00:00:00+00:00'}
            with mock.patch('lore_mirror.sync.sync_once', return_value=result), \
                 mock.patch('lore_mirror.sync.signal.signal'), \
                 mock.patch('lore_mirror.sync.time.sleep', side_effect=KeyboardInterrupt), \
                 mock.patch('builtins.print'):
                with self.assertRaises(KeyboardInterrupt):
                    run_sync(cfg, once=False)
            status = json.loads((root / 'daemon-state.json').read_text())
            self.assertEqual(status['status'], 'stopped')
            self.assertGreater(status['pid'], 0)
            self.assertEqual(status['cycles_completed'], 1)
            self.assertEqual(status['first_successful_cycle_at'], result['cycle_finished_at'])
            self.assertTrue(status['next_scheduled_at'])
            self.assertEqual(status['scope'], ['test'])

    def _run_daemon_cycles(self, cfg_dict, results, cycles):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / 'config.json'
            cfg.write_text(json.dumps(dict(cfg_dict, data_dir=tmp)))
            sleeps, scopes = [], []
            def fake_sync(config):
                scopes.append([item['id'] for item in config['lists']])
                result = results[len(scopes) - 1]
                if isinstance(result, Exception):
                    raise result
                return result
            def fake_sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) >= cycles:
                    raise KeyboardInterrupt
            with mock.patch('lore_mirror.sync.sync_once', side_effect=fake_sync), \
                 mock.patch('lore_mirror.sync.signal.signal'), \
                 mock.patch('lore_mirror.sync.time.sleep', side_effect=fake_sleep), \
                 mock.patch('builtins.print'):
                with self.assertRaises(KeyboardInterrupt):
                    run_sync(cfg, once=False)
            return sleeps, scopes, json.loads((root / 'daemon-state.json').read_text())

    def test_daemon_retries_quickly_after_errors_and_resets_on_success(self):
        ok = {'status': 'ready', 'pending_lists': [], 'unsafe_index': False,
            'cycle_finished_at': '2026-01-01T00:00:00+00:00'}
        dns = OSError('<urlopen error [Errno -3] Temporary failure in name resolution>')
        sleeps, _, status = self._run_daemon_cycles({'lists': [{'id': 'test'}], 'interval_seconds': 3600},
            [dns, dns, dns, dns, dns, ok], 6)
        self.assertEqual(sleeps, [60, 120, 300, 900, 900, 3600])
        self.assertEqual(status['consecutive_errors'], 0)

    def test_daemon_runs_priority_lists_between_full_cycles(self):
        ok = {'status': 'ready', 'pending_lists': [], 'unsafe_index': False,
            'cycle_finished_at': '2026-01-01T00:00:00+00:00'}
        cfg = {'lists': [{'id': 'u-boot'}, {'id': 'op-tee'}, {'id': 'lkml'}], 'interval_seconds': 3600,
            'priority_lists': ['u-boot', 'op-tee'], 'priority_interval_seconds': 300}
        clock = iter(range(0, 10**6, 300))
        with mock.patch('lore_mirror.sync.time.time', side_effect=lambda: next(clock)):
            sleeps, scopes, status = self._run_daemon_cycles(cfg, [ok] * 3, 3)
        self.assertEqual(scopes[0], ['u-boot', 'op-tee', 'lkml'])
        self.assertEqual(scopes[1], ['u-boot', 'op-tee'])
        self.assertTrue(all(60 <= x <= 300 for x in sleeps))
        self.assertEqual(status['last_cycle_scope'], 'priority')

    def test_priority_config_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extra in ({'priority_lists': ['nope']}, {'priority_lists': ['test'], 'priority_interval_seconds': 10},
                    {'retry_seconds': []}, {'retry_seconds': [0]}):
                cfg = Path(tmp) / 'c.json'
                cfg.write_text(json.dumps(dict({'data_dir': tmp, 'lists': [{'id': 'test'}], 'interval_seconds': 3600,
                    'upstream': 'https://erol.kernel.org'}, **extra)))
                with self.assertRaises(SyncError, msg=str(extra)):
                    prepare_config(cfg)

    def test_long_cold_index_is_not_mislabeled_hourly_observation(self):
        import datetime as dt
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / 'config.json'
            cfg_path.write_text(json.dumps({'data_dir': tmp, 'lists': [{'id': 'test'}]}))
            now = dt.datetime.now(dt.timezone.utc)
            atomic_json(root / 'sync-state.json', {'generation': 1, 'lists': {'test': {
                'status': 'ready', 'checked_at': (now-dt.timedelta(hours=2)).isoformat(),
                'indexed_at': (now-dt.timedelta(seconds=30)).isoformat(),
                'indexed_fingerprints': {'/test/git/0.git': 'a'*40}}}})
            with mock.patch('lore_mirror.sync.fetch_manifest', return_value={'/test/git/0.git': {'fingerprint': 'a'*40}}):
                state = sync_once(prepare_config(cfg_path))
            delta = state['lists']['test']['hourly_delta']
            self.assertFalse(delta['is_hourly_observation'])
            self.assertLess(delta['elapsed_observation_seconds'], 60)


if __name__ == '__main__':
    unittest.main()
