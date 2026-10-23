"""Operator-only resolution of local issues from the host CLI."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from lore_mirror.__main__ import main
from lore_mirror.errors import MirrorError
from lore_mirror.service import MirrorService


class IssueResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'data_dir': str(self.root), 'backend': 'fixture'}))
        self.service = MirrorService({'data_dir': str(self.root), 'backend': 'fixture'})
        self.issue = self.service.issue_report('Sync stuck', 'DNS error never clears', idempotency_key='k' * 16)

    def cli(self, *argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch('sys.stdin', io.StringIO(stdin)):
            code = main(['issue-resolve', '--config', str(self.config), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_cli_resolves_with_evidence_and_listing_reflects_it(self):
        evidence = self.root / 'evidence.txt'
        evidence.write_text('Fixed by sliced imports; u-boot refreshed at T.\n')
        code, out, _ = self.cli(self.issue['id'], '--resolution-file', str(evidence))
        self.assertEqual(code, 0)
        resolved = json.loads(out)
        self.assertEqual(resolved['status'], 'resolved')
        self.assertEqual(resolved['resolution'], 'Fixed by sliced imports; u-boot refreshed at T.')
        self.assertIn('resolved_at', resolved)
        self.assertEqual((resolved['title'], resolved['description']), ('Sync stuck', 'DNS error never clears'))
        fresh = MirrorService({'data_dir': str(self.root), 'backend': 'fixture'})
        self.assertEqual(fresh.invoke('issue.get', {'issue_id': self.issue['id']})['status'], 'resolved')
        self.assertEqual(fresh.issue_list()['items'][0]['status'], 'resolved')

    def test_second_resolution_and_bad_input_are_rejected(self):
        self.assertEqual(self.cli(self.issue['id'], '--resolution-file', '-', stdin='done')[0], 0)
        code, _, err = self.cli(self.issue['id'], '--resolution-file', '-', stdin='again')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)['error']['code'], 'issue_not_open')
        for issue_id, text, error in ((self.issue['id'], '', 'invalid_resolution'),
                (self.issue['id'], 'x' * 4001, 'invalid_resolution'), ('nothex', 'ok', 'invalid_issue_id'),
                ('0' * 32, 'ok', 'not_found')):
            with self.subTest(error=error), self.assertRaises(MirrorError) as raised:
                self.service.issue_resolve(issue_id, text)
            self.assertEqual(raised.exception.code, error)

    def test_concurrent_resolution_changes_only_open_rows(self):
        with sqlite3.connect(self.service.issue_db) as db:
            context = json.loads(db.execute('SELECT context FROM issues').fetchone()[0])
            context['status'] = 'resolved'
            # Another process resolved it after this one read the issue.
            with mock.patch.object(MirrorService, 'issue_get', side_effect=[self.issue, MirrorError('x', 'y')]):
                db.execute('UPDATE issues SET context=?', (json.dumps(context),))
                db.commit()
                with self.assertRaises(MirrorError) as raised:
                    self.service.issue_resolve(self.issue['id'], 'late')
        self.assertEqual(raised.exception.code, 'issue_not_open')

    def test_issue_tools_are_unchanged(self):
        self.assertNotIn('issue.resolve', self.service.tools)
        self.assertEqual([n for n in self.service.tools if n.startswith('issue.')],
                         ['issue.report', 'issue.get', 'issue.list'])


if __name__ == '__main__':
    unittest.main()
