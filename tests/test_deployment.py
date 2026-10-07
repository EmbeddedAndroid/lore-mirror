"""Deployment gates use isolated paths and never contact an archive server."""
import datetime as dt
import fcntl
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lore_mirror.deployment import check_sync, manifest_config
from lore_mirror.sync import SyncError, prepare_config, run_command, sync_once

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_native_command_logs_rotate_and_preserve_failure_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'job.log'
            config = {'data_dir': tmp, 'min_free_fraction': .3}
            with patch('lore_mirror.sync.LOG_MAX_BYTES', 64):
                for number in range(5):
                    run_command([sys.executable, '-c', f'print("entry{number}")'], config, log)
                with self.assertRaises(SyncError):
                    run_command([sys.executable, '-c', 'print("failed job"); raise SystemExit(2)'], config, log)
            self.assertIn('failed job', log.read_text())
            for number, entry in ((1, 4), (2, 3), (3, 2)):
                self.assertIn(f'entry{entry}', Path(f'{log}.{number}').read_text())
            self.assertFalse(Path(f'{log}.4').exists())

    def test_native_command_logs_append_below_rotation_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'job.log'
            log.write_text('previous output\n')
            run_command([sys.executable, '-c', 'print("new output")'],
                        {'data_dir': tmp, 'min_free_fraction': .3}, log)
            self.assertIn('previous output', log.read_text())
            self.assertIn('new output', log.read_text())
            self.assertFalse(Path(f'{log}.1').exists())

    def test_clone_runtime_dependencies_load_without_network(self):
        result = subprocess.run(['perl', '-MPublicInbox::LEI', '-e', '1'],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_container_scope_is_small_and_native_scope_is_preserved(self):
        config = json.loads((ROOT / 'config/container.json').read_text())
        self.assertEqual({row['id'] for row in config['lists']},
                         {'devicetree-spec', 'u-boot-amlogic'})
        self.assertEqual(config['max_workers'], 1)
        self.assertEqual(len(json.loads((ROOT / 'config/pilot.json').read_text())['lists']), 4)

    def test_manifest_config_has_explicit_shared_scope_without_mutating_template(self):
        template = json.loads((ROOT / 'config/pilot.json').read_text())
        result = manifest_config(template, {'/devicetree-spec/git/0.git': {},
            '/example/git/0.git': {}, '/example/git/1.git': {}})
        self.assertEqual([row['id'] for row in result['lists']], ['devicetree-spec', 'example'])
        self.assertEqual(result['lists'][0], template['lists'][0])
        self.assertEqual(result['lists'][1]['inbox_path'], '/data/inboxes/example')
        self.assertEqual(result['lists'][1]['class'], 'unclassified')
        self.assertEqual(len(template['lists']), 4)
        with self.assertRaises(ValueError):
            manifest_config(template, {})
        with self.assertRaises(ValueError):
            manifest_config(template, {f'/list{i}/git/0.git': {} for i in range(1001)})

    def test_manifest_generator_emits_config_from_validated_offline_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'manifest.js.gz'
            path.write_bytes(gzip.compress(json.dumps({
                '/devicetree-spec/git/0.git': {'fingerprint': 'a' * 40},
                '/extra/git/0.git': {'fingerprint': 'b' * 40},
            }).encode()))
            command = ['python', str(ROOT / 'scripts/configure-manifest.py'),
                       '--full-manifest', '--manifest', str(path),
                       '--template', str(ROOT / 'config/pilot.json')]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([row['id'] for row in json.loads(result.stdout)['lists']],
                             ['devicetree-spec', 'extra'])
            path.write_bytes(gzip.compress(b'{"/../escape": {}}'))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_full_manifest_requires_explicit_option(self):
        result = subprocess.run(['python', str(ROOT / 'scripts/configure-manifest.py')],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--full-manifest', result.stderr)

    def test_sync_refuses_empty_and_duplicate_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            for lists in ([], [{'id': 'one'}, {'id': 'one'}]):
                path.write_text(json.dumps({'data_dir': tmp, 'lists': lists}))
                with self.assertRaises(SyncError):
                    prepare_config(path)

    def test_unchanged_sync_refreshes_success_without_reindexing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.json'
            path.write_text(json.dumps({'data_dir': tmp, 'lists': [{'id': 'one'}]}))
            fingerprint = {'/one/git/0.git': 'a' * 40}
            state_path = root / 'sync-state.json'
            state_path.write_text(json.dumps({'generation': 8, 'last_success': 'old',
                'last_error': 'old failure', 'last_indexed_at': 'old index', 'lists': {'one': {
                    'status': 'ready', 'indexed_fingerprints': fingerprint,
                    'indexed_at': '2026-01-01T00:00:00+00:00'}}}))
            with patch('lore_mirror.sync.fetch_manifest', return_value={
                    '/one/git/0.git': {'fingerprint': 'a' * 40}}), \
                    patch('lore_mirror.sync.run_command') as command:
                state = sync_once(prepare_config(path))
            command.assert_not_called()
            self.assertEqual(state['generation'], 8)
            self.assertEqual(state['last_indexed_at'], 'old index')
            self.assertEqual(state['last_success'], state['cycle_finished_at'])
            self.assertNotIn('last_error', state)

    def test_health_checks_process_lock_failure_and_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {'data_dir': tmp, 'lists': [{'id': 'one'}], 'job_timeout_seconds': 1}
            now = dt.datetime.now(dt.timezone.utc)
            state = {'pid': os.getpid(), 'status': 'sleeping', 'last_cycle_status': 'ready',
                     'next_scheduled_at': (now + dt.timedelta(hours=1)).isoformat()}
            path = root / 'daemon-state.json'
            def write():
                path.write_text(json.dumps(state))
            write()
            with (root / 'sync.lock').open('w') as lock:
                with self.assertRaisesRegex(ValueError, 'writer lock'):
                    check_sync(config, now)
                fcntl.flock(lock, fcntl.LOCK_EX)
                check_sync(config, now)
                with patch('lore_mirror.deployment.os.kill', side_effect=ProcessLookupError):
                    with self.assertRaises(ProcessLookupError):
                        check_sync(config, now)
                for status in ('error', 'partial'):
                    state['last_cycle_status'] = status
                    write()
                    with self.assertRaisesRegex(ValueError, 'cycle failed'):
                        check_sync(config, now)
                state['last_cycle_status'] = 'ready'
                state['next_scheduled_at'] = (now - dt.timedelta(minutes=2)).isoformat()
                write()
                with self.assertRaisesRegex(ValueError, 'scheduled cycle'):
                    check_sync(config, now)
                state.update(status='syncing', cycle_started_at=now.isoformat())
                write()
                check_sync(config, now)
                with self.assertRaisesRegex(ValueError, 'job budgets'):
                    check_sync(config, now + dt.timedelta(minutes=4))
                state['status'] = 'stopped'
                write()
                with self.assertRaisesRegex(ValueError, 'not running'):
                    check_sync(config, now)

    def test_setup_orders_gates_and_never_starts_after_failure(self):
        for failure in ('', 'compose build', '--profile test run', 'sync --config /config/pilot.json --once'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                docker = root / 'docker'
                docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                                  'if [ -n "$FAIL_ON" ]; then case "$*" in *"$FAIL_ON"*) exit 9;; esac; fi\n')
                docker.chmod(0o700)
                env = os.environ | {'PATH': str(root) + ':/usr/bin:/bin',
                    'CALLS': str(root / 'calls'), 'FAIL_ON': failure, 'PYTHON': '/not/python'}
                result = subprocess.run(['bash', str(ROOT / 'scripts/setup.sh'),
                    '--config', 'config/container.json'], env=env, capture_output=True, text=True)
                calls = (root / 'calls').read_text()
                if failure:
                    self.assertEqual(result.returncode, 9)
                    self.assertNotIn('up -d', calls)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertLess(calls.index('compose build'), calls.index('--profile test run'))
                    self.assertLess(calls.index('compose stop sync'), calls.index('sync --config'))
                    self.assertLess(calls.index('sync --config'), calls.index('up -d --wait'))
