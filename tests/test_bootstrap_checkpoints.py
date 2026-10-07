"""Completed inboxes remain readable between native indexing jobs."""
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lore_mirror.errors import MirrorError
from lore_mirror.service import MirrorService
from lore_mirror.sync import SyncError, prepare_config, sync_once


class BootstrapCheckpointTests(unittest.TestCase):
    def run_cycle(self, fail=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {}
            for ident in ('first', 'second'):
                (root / 'inboxes' / ident / 'git/0.git').mkdir(parents=True)
                manifest[f'/{ident}/git/0.git'] = {'fingerprint': 'a' * 40}
            cfg_path = root / 'config.json'
            cfg_path.write_text(json.dumps({'data_dir': tmp, 'index_batch_size': '4m',
                'lists': [{'id': name} for name in ('first', 'second')]}))
            service = object.__new__(MirrorService)
            service.cfg = {'data_dir': tmp}
            commands = []

            def command(argv, cfg, log, **kwargs):
                commands.append(argv)
                if argv[0] == 'public-inbox-clone':
                    with (root / 'index.lock').open('rb') as lock:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    if '--include=/second' in argv:
                        state = json.loads((root / 'sync-state.json').read_text())
                        if fail:
                            self.assertTrue(state['unsafe_index'])
                            with self.assertRaises(MirrorError):
                                with service.read_guard():
                                    pass
                        else:
                            self.assertEqual(state['generation'], 1)
                            self.assertEqual(state['lists']['first']['status'], 'ready')
                            with service.read_guard():
                                pass
                else:
                    with (root / 'index.lock').open('rb') as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    state = json.loads((root / 'sync-state.json').read_text())
                    self.assertTrue(state['updating'])
                    if argv[0] == fail and 'first' in str(log):
                        raise SyncError('native indexing interrupted')
                return {'seconds': .1, 'peak_aggregate_rss_bytes_sampled': 100, 'returncode': 0}

            with patch('lore_mirror.sync.fetch_manifest', return_value=manifest), \
                 patch('lore_mirror.sync.pack_budget', return_value={'complete': True, 'known_pack_bytes': 1}), \
                 patch('lore_mirror.sync._refs', return_value={}), \
                 patch('lore_mirror.sync.message_count', return_value=2), \
                 patch('lore_mirror.sync.run_command', side_effect=command):
                result = sync_once(prepare_config(cfg_path))
            self.assertEqual(result['generation'], 1 if fail else 2)
            self.assertEqual(result['unsafe_index'], bool(fail))
            for argv in commands:
                if argv[0] in ('public-inbox-index', 'public-inbox-extindex'):
                    self.assertIn('--batch-size=4m', argv)
                    self.assertIn('-j1', argv)
            return result

    def test_readable_checkpoints_and_generation_between_inboxes(self):
        self.assertEqual(self.run_cycle()['status'], 'ready')

    def test_failed_basic_index_stays_unsafe_after_later_success(self):
        state = self.run_cycle('public-inbox-index')
        self.assertEqual(state['lists']['first']['status'], 'error')
        self.assertEqual(state['lists']['second']['status'], 'ready')

    def test_failed_shared_index_stays_unsafe_after_later_success(self):
        self.run_cycle('public-inbox-extindex')

    def test_invalid_batch_sizes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            for size in ('0', '-1m', '--dangerous', '4 m', None):
                path.write_text(json.dumps({'data_dir': tmp, 'lists': [{'id': 'one'}],
                                            'index_batch_size': size}))
                with self.assertRaises(SyncError):
                    prepare_config(path)
