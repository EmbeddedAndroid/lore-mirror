"""Indexing is an explicit operator command, never an MCP mutation tool."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lore_mirror.__main__ import main


class CodeIndexCLITests(unittest.TestCase):
    def config(self, root):
        path = Path(root) / 'config.json'
        path.write_text(json.dumps({'backend': 'fixture', 'data_dir': str(Path(root)/'data'),
                                   'runtime_dir': str(Path(root)/'runtime'),
                                   'issue_dir': str(Path(root)/'issues')}))
        return path

    def test_disabled_operator_call_has_no_index_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.config(temp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['code-index', '--config', str(path)]), 0)
            self.assertEqual(json.loads(out.getvalue())['status'], 'disabled')
            self.assertFalse((Path(temp)/'data/code').exists())

    def test_operator_error_is_bounded_json(self):
        from lore_mirror.code_index import CodeIndexError
        with tempfile.TemporaryDirectory() as temp:
            path = self.config(temp)
            err = io.StringIO()
            with patch('lore_mirror.code_index.index_code_once', side_effect=CodeIndexError('x'*1000)):
                with contextlib.redirect_stderr(err):
                    self.assertEqual(main(['code-index', '--config', str(path)]), 1)
            payload = json.loads(err.getvalue())
            self.assertEqual(payload['error']['code'], 'code_index_error')
            self.assertLessEqual(len(payload['error']['message']), 160)

    def test_preflight_only_checks_actual_cap_without_build(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.config(temp)
            out = io.StringIO()
            with patch('lore_mirror.code_index._enforced_memory_limit', return_value={'memory_bytes':512}) as cap:
                with patch('lore_mirror.code_index.index_code_once') as build:
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(main(['code-index', '--config', str(path), '--preflight']), 0)
                    build.assert_not_called()
            self.assertTrue(json.loads(out.getvalue())['preflight_only'])
            cap.assert_called_once()
            self.assertFalse((Path(temp)/'data/code').exists())
