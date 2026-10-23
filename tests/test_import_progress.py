"""First-import progress: sampling, rate and ETA, and status readers."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from lore_mirror.service import MirrorService, import_activity
from lore_mirror.sync import SyncError, atomic_json, import_progress
from tests import test_priority_refresh as fixtures

ROOT = Path(__file__).resolve().parents[1]


def extindex(path, rows):
    """Minimal public-inbox extindex tables: inbox key -> membership count."""
    db = Path(path) / 'ei15' / 'over.sqlite3'
    db.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.executescript('''CREATE TABLE inboxes (ibx_id INTEGER PRIMARY KEY, eidx_key TEXT);
            CREATE TABLE xref3 (docid INTEGER, ibx_id INTEGER, xnum INTEGER, oidbin BLOB);''')
        for number, (key, count) in enumerate(rows.items(), 1):
            conn.execute('INSERT INTO inboxes VALUES (?,?)', (number, key))
            conn.executemany('INSERT INTO xref3 VALUES (?,?,?,?)', [(n, number, n, b'o') for n in range(count)])
        conn.commit()
    return db


class ImportProgressQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'extindex': str(self.root / 'extindex')}
        self.item = {'newsgroup': 'org.kernel.lore.big', 'inbox_path': str(self.root / 'inboxes' / 'big')}

    def test_counts_only_this_inbox(self):
        extindex(self.config['extindex'], {'org.kernel.lore.big': 7, 'org.kernel.lore.hot': 3})
        with mock.patch('lore_mirror.sync.message_count', return_value=20):
            self.assertEqual(import_progress(self.config, self.item), (7, 20))

    def test_missing_index_is_zero_and_broken_index_is_unknown(self):
        with mock.patch('lore_mirror.sync.message_count', return_value=20):
            self.assertEqual(import_progress(self.config, self.item), (0, 20))
            db = Path(self.config['extindex']) / 'ei15' / 'over.sqlite3'
            db.parent.mkdir(parents=True)
            db.write_bytes(b'not a database' * 100)
            self.assertEqual(import_progress(self.config, self.item), (None, 20))


class ImportReportTests(fixtures.RefreshHarness):
    """Scheduler reports for a three-slice first import (1000 per slice)."""
    def setUp(self):
        super().setUp()
        self.names = ['hot', 'big', 'tail']
        self.priority = ['hot']
        self.slices = {'big': 3}
        self.done = 0
        self.progress_calls = []
        self.reports = []
        self.crash_after = None
        self.enterContext(mock.patch('lore_mirror.sync.import_progress', side_effect=self.progress))

    def progress(self, config, item):
        self.progress_calls.append(item['id'])
        return self.done, 4000

    def manifest(self, config, state):
        if self.checks:
            current = json.loads((self.root / 'sync-state.json').read_text())
            self.reports.append(current.get('importing'))
        return super().manifest(config, state)

    def command(self, argv, config, log, **kwargs):
        ident = Path(log).stem.split('-', 1)[1]
        step = argv[0].removeprefix('public-inbox-')
        result = super().command(argv, config, log, **kwargs)
        if step == 'extindex' and self.slices.get(ident):
            if self.crash_after == self.slices[ident]:
                raise KeyboardInterrupt
            self.slices[ident] -= 1
            self.now += 3600
            self.done += 1000
            return dict(result, seconds=3600, sliced=True)
        if step == 'extindex' and ident == 'big':
            self.done = 4000
        return result

    def test_rate_and_eta_between_slices_then_cleared(self):
        # warm is already indexed and has new mail: incremental full-pass work.
        self.names = ['hot', 'big', 'warm', 'tail']
        self.rows['warm'] = self.indexed_row('warm', indexed_fingerprints={'/warm/git/0.git': 'c' * 40})
        state = self.full_pass()
        self.assertIn(('extindex', 'warm'), self.commands)
        first, second, third = self.reports
        self.assertEqual((first['inbox'], first['phase'], first['done'], first['total'], first['slices']),
                         ('big', 'extindex', 1000, 4000, 1))
        self.assertEqual((first['rate_per_hour'], first['rate_basis']), (1000.0, 'last slice'))
        self.assertEqual(dt.datetime.fromisoformat(first['eta']).timestamp(), self.now_at(1) + 3 * 3600)
        self.assertEqual(first['started_at'], self.at(0))
        self.assertEqual((third['done'], third['slices'], third['started_at']), (3000, 3, self.at(0)))
        self.assertEqual(import_activity(first),
            f"importing big 1000/4000 at 1000/h, ETA {first['eta']} as of {first['sampled_at']}")
        self.assertNotIn('importing', state)
        # Incremental (already indexed) inboxes are never sampled.
        self.assertEqual(set(self.progress_calls), {'big', 'tail'})

    def test_restart_keeps_start_and_previous_rate(self):
        self.crash_after = 1
        interrupted = self.full_pass()
        self.assertEqual(interrupted['importing']['rate_per_hour'], 1000.0)
        self.crash_after = None
        self.snapshots.clear()
        self.slices['big'] = 1
        self.full_pass()
        resumed = next(s[2]['importing'] for s in self.snapshots if s[:2] == ('extindex', 'big'))
        self.assertEqual((resumed['rate_per_hour'], resumed['rate_basis']), (1000.0, 'earlier slice'))
        self.assertEqual(resumed['started_at'], self.at(0))

    def test_failed_import_is_not_reported(self):
        self.names = ['hot', 'big']
        self.failure = ('extindex', 'big', SyncError('extindex exited 2'))
        self.slices = {}
        failed = self.full_pass()
        self.assertNotIn('importing', failed)
        self.assertEqual(failed['lists']['big']['last_error'], 'extindex exited 2')

    def test_deselected_import_is_dropped_by_a_full_pass_only(self):
        self.names = ['hot']
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state['importing'] = {'inbox': 'gone', 'phase': 'extindex'}
        fixtures.atomic_json(self.root / 'sync-state.json', state)
        self.checks.append(0)
        from lore_mirror.sync import prepare_config, sync_once
        self.assertEqual(sync_once(prepare_config(self.config()), full=False)['importing']['inbox'], 'gone')
        self.assertNotIn('importing', self.full_pass())

    def test_priority_only_cycle_keeps_non_priority_import(self):
        self.names = ['hot', 'big']
        self.config()
        state = json.loads((self.root / 'sync-state.json').read_text())
        state['importing'] = {'inbox': 'big', 'phase': 'extindex', 'rates': {'extindex': 900.0}}
        fixtures.atomic_json(self.root / 'sync-state.json', state)
        self.checks.append(0)
        from lore_mirror.sync import prepare_config, sync_once
        config = prepare_config(self.config())
        config['lists'] = [item for item in config['lists'] if item['id'] in config['priority_lists']]
        kept = sync_once(config, full=False)['importing']
        self.assertEqual((kept['inbox'], kept['rates']), ('big', {'extindex': 900.0}))

    def test_basic_phase_reports_message_count_without_total(self):
        self.slices = {}
        self.full_pass()
        basic = next(s[2]['importing'] for s in self.snapshots if s[:2] == ('index', 'big'))
        self.assertEqual((basic['phase'], basic['done'], basic['total']), ('basic_index', 2, None))
        self.assertTrue(import_activity(basic).startswith('importing big 2/? (basic_index) as of '))

    def at(self, hours):
        return dt.datetime.fromtimestamp(self.started + hours * 3600, dt.timezone.utc).isoformat()

    def now_at(self, hours):
        return self.started + hours * 3600


class ImportStatusTests(unittest.TestCase):
    def test_status_reports_sanitized_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MirrorService({'data_dir': tmp, 'backend': 'fixture'})
            atomic_json(Path(tmp) / 'sync-state.json', {'importing': {'inbox': 'big', 'phase': 'extindex',
                'done': 5, 'total': 9, 'rate_per_hour': 2.4, 'eta': 'E', 'sampled_at': 'S', 'path': '/secret'}})
            sync = service.status()['sync']
            self.assertEqual(sync['activity'], 'importing big 5/9 at 2/h, ETA E as of S')
            self.assertNotIn('path', sync['importing'])
            for junk in ('x', {'inbox': 3}, None):
                atomic_json(Path(tmp) / 'sync-state.json', {'importing': junk})
                self.assertNotIn('activity', service.status()['sync'])

    def test_progress_script_reports_sampled_and_live_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extindex(root / 'extindex', {'org.kernel.lore.big': 6})
            config = root / 'config.json'
            config.write_text(json.dumps({'extindex': str(root / 'extindex'),
                'lists': [{'id': 'big', 'newsgroup': 'org.kernel.lore.big'}]}))
            atomic_json(root / 'sync-state.json', {'lists': {'big': {'status': 'indexing'}},
                'importing': {'inbox': 'big', 'phase': 'extindex', 'done': 5, 'total': 9, 'sampled_at': 'S'},
                'error_history': [{'error': 'dns', 'cleared_by': 'manifest fetch'}]})
            out = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'progress.py'), str(root), str(config)],
                capture_output=True, text=True, check=True,
                env=dict(os.environ, PYTHONPATH=str(ROOT / 'src'))).stdout
        report = json.loads(out)
        self.assertEqual(report['importing']['live_done'], 6)
        self.assertEqual(report['activity'], 'importing big 5/9 as of S')
        self.assertEqual(report['error_history'][0]['cleared_by'], 'manifest fetch')


if __name__ == '__main__':
    unittest.main()
