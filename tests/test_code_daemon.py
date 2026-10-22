"""Coordinator integration must not compromise mail checkpoints or priority."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from lore_mirror.sync import run_sync
from lore_mirror.__main__ import main
from lore_mirror.code_sync import CodeSyncError


class CodeDaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {'data_dir': str(self.root), 'lists': [{'id':'u-boot'}],
                    'code': {'enabled': True}, 'priority_lists': [], 'interval_seconds': 3600}
        self.mail = {'status':'ready','pending_lists':[], 'cycle_finished_at':'2026-10-21T00:00:00+00:00'}

    def test_code_failure_does_not_rewrite_mail_success(self):
        with patch('lore_mirror.sync.prepare_config', return_value=self.cfg), \
             patch('lore_mirror.sync.sync_once', return_value=self.mail), \
             patch('lore_mirror.code_sync.sync_code_once', side_effect=RuntimeError('fixture failure')), \
             patch('builtins.print'):
            self.assertIs(run_sync('unused', once=True), self.mail)
        self.assertFalse((self.root / 'sync-state.json').exists())

    def test_daemon_clears_a_code_error_after_a_successful_unit(self):
        self.cfg['priority_interval_seconds'] = 300
        with patch('lore_mirror.sync.prepare_config', return_value=self.cfg), \
             patch('lore_mirror.sync.sync_once', return_value=self.mail), \
             patch('lore_mirror.code_sync.sync_code_once', side_effect=[RuntimeError('fixture failure'), {'status': 'ready'}]), \
             patch('lore_mirror.sync.time.sleep', side_effect=[None, KeyboardInterrupt]), \
             patch('builtins.print'):
            with self.assertRaises(KeyboardInterrupt):
                run_sync('unused', once=False)
        daemon = json.loads((self.root / 'daemon-state.json').read_text())
        self.assertEqual(daemon['code_status'], 'ready')
        self.assertNotIn('code_error', daemon)

    def test_due_priority_prevents_admission_callback(self):
        self.mail['next_priority_refresh_at'] = '2000-01-01T00:00:00+00:00'
        checks=[]
        def run(config, should_yield):
            checks.append(should_yield())
            return {'status':'yielded'}
        with patch('lore_mirror.sync.prepare_config', return_value=self.cfg), \
             patch('lore_mirror.sync.sync_once', return_value=self.mail), \
             patch('lore_mirror.code_sync.sync_code_once', side_effect=run), patch('builtins.print'):
            run_sync('unused', once=True)
        self.assertEqual(checks, [True])

    def test_disabled_mail_config_does_not_call_code(self):
        self.cfg['code']['enabled'] = False
        with patch('lore_mirror.sync.prepare_config', return_value=self.cfg), \
             patch('lore_mirror.sync.sync_once', return_value=self.mail), \
             patch('lore_mirror.code_sync.sync_code_once') as code, patch('builtins.print'):
            run_sync('unused', once=True)
        code.assert_not_called()

    def test_disabled_code_cli_creates_no_mirror(self):
        config=self.root/'config.json'
        config.write_text(json.dumps({'data_dir': str(self.root/'data'), 'backend':'fixture'}))
        with patch('builtins.print'):
            self.assertEqual(main(['code-sync','--config',str(config)]),0)
        self.assertFalse((self.root/'data').exists())

    def test_code_cli_reports_a_busy_coordinator_as_an_error(self):
        config=self.root/'config.json'
        config.write_text(json.dumps({'data_dir': str(self.root/'data'), 'backend':'fixture'}))
        stderr=io.StringIO()
        with patch('lore_mirror.code_sync.sync_code_once', side_effect=CodeSyncError('another code coordinator is active')), \
             patch('sys.stderr', stderr):
            self.assertEqual(main(['code-sync','--config',str(config)]),1)
        self.assertEqual(json.loads(stderr.getvalue())['error']['code'],'code_sync_error')
