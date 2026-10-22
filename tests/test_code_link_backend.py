"""Exact-snapshot evidence read and cross-domain pagination checks."""
import copy
import json
import os
import subprocess
import time
import unittest
from unittest.mock import patch
from tests import test_code_backend as fixtures
from lore_mirror.code_link_backend import CodeLinkBackend, MAX_ANCESTRY_FALLBACKS
from lore_mirror.code_backend import GitResult
from lore_mirror.errors import MirrorError


class FakeStore:
    def __init__(self, rows):
        self.rows=rows
        self.gen="1"
    def generation(self): return self.gen
    def coverage(self,tree,ref,snapshot_oid):
        return {"complete":False,"scan":{"status":"not_scanned"},"catalog_fingerprint":None}
    def related_mail(self, *args, **kwargs):
        return {"items":copy.deepcopy(self.rows),"partial":False,"metadata":{"coverage":"partial"}}
    related_commits=related_mail


class LinkReaderTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CodeReaderTests(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.cfg['code']['links']={'enabled':True,'patch_since':'2026-01-01'}
        self.rows=[{'tree':'tiny','commit':self.f.old,'message_id':'p@example.org',
                    'variant':'a'*40,'raw_sha256':'b'*64,'source_blob':'a'*40,
                    'catalog_fingerprint':self.f.state['trees']['tiny']['config_fingerprint'],
                    'evidence_level':'linked','evidence_method':'trailer','evidence_version':'1'}]
        self.store=FakeStore(self.rows)
        self.reader=CodeLinkBackend(self.f.cfg,self.f.backend,store=self.store)
    def test_reachability_not_application(self):
        r=self.reader.related_mail('tiny',self.f.old)
        self.assertEqual(r['items'][0]['reachability'],'reachable')
        self.assertEqual(r['items'][0]['application_status'],'unknown')
        self.assertEqual(r['items'][0]['effective_change'],'unknown')
        self.assertEqual(r['metadata']['target_commit'],self.f.tip)
    def test_active_links_reader_never_blocks_mail_indexer(self):
        for query in (lambda: self.reader.related_mail('tiny', self.f.old),
                      lambda: self.reader.related_commits('p@example.org', 'tiny')):
            fixtures.assert_mail_writer_proceeds(self, self.f.data, query)
    def test_direct_reader_rejects_evidence_publication_during_lookup(self):
        original = self.store.related_mail
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.gen = '2'
            return result
        with patch.object(self.store, 'related_mail', side_effect=changed):
            with self.assertRaises(MirrorError) as raised:
                self.reader.related_mail('tiny', self.f.old)
        self.assertEqual(raised.exception.code, 'stale_links_generation')
    def test_newer_commit_not_reachable_at_old_tag(self):
        self.rows[0]['commit']=self.f.tip
        r=self.reader.related_mail('tiny',self.f.tip,'refs/tags/next-20261020')
        self.assertEqual(r['items'][0]['reachability'],'not_reachable')
        self.assertEqual(r['metadata']['target_commit'],self.f.old)

    def test_many_candidates_share_one_exact_snapshot_graph_call(self):
        self.rows.append(dict(self.rows[0], commit=self.f.tip))
        self.rows.append(dict(self.rows[0]))
        original = self.f.backend._run
        with patch.object(self.f.backend, '_run', wraps=original) as run:
            result = self.reader.related_commits('p@example.org', 'tiny', 'refs/tags/next-20261020')
        graph_calls = [call for call in run.call_args_list if call.args[1][0] in ('merge-base', 'rev-list')]
        self.assertEqual(len(graph_calls), 1, graph_calls)
        self.assertEqual(graph_calls[0].args[1][0], 'rev-list')
        self.assertEqual([row['reachability'] for row in result['items']], ['reachable', 'not_reachable', 'reachable'])
        self.assertEqual(result['metadata']['target_commit'], self.f.old)

    def make_deep_dag(self):
        """Real merge DAG, old reachable candidate, and an unrelated root."""
        tree = self.f.git('rev-parse', self.f.old + '^{tree}').strip()
        def commit(day, *parents):
            date = f'2020-01-{day:02}T00:00:00+00:00'
            env = dict(os.environ, GIT_AUTHOR_NAME='Offline Author',
                       GIT_AUTHOR_EMAIL='offline@example.invalid',
                       GIT_COMMITTER_NAME='Offline Author',
                       GIT_COMMITTER_EMAIL='offline@example.invalid',
                       GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
            args = ['git', '--git-dir=' + str(self.f.repo), 'commit-tree', tree,
                    '-m', f'ancestry fixture {day}']
            for parent in parents:
                args.extend(['-p', parent])
            return subprocess.check_output(args, env=env, stderr=subprocess.PIPE).decode().strip()
        root = commit(1)
        ancestor = commit(2, root)
        side = commit(3, root)
        trunk = ancestor
        for day in range(4, 14):
            trunk = commit(day, trunk)
        tip = commit(14, trunk, side)
        unrelated = commit(2)
        self.f.tip = tip
        self.f.publish()
        self.f.backend.limits['scan_limit'] = 2
        self.rows[0]['commit'] = ancestor
        self.rows.append(dict(self.rows[0], commit=unrelated))
        return ancestor, unrelated, tip

    def test_real_dag_candidates_beyond_graph_limit_have_exact_reachability(self):
        ancestor, unrelated, tip = self.make_deep_dag()
        original = self.f.backend._run
        calls = []
        def record(tree, args, **kwargs):
            result = original(tree, args, **kwargs)
            calls.append((args, result))
            return result
        with patch.object(self.f.backend, '_run', side_effect=record):
            result = self.reader.related_commits('p@example.org', 'tiny')
        graph_args, graph = calls[0]
        self.assertEqual(graph_args[:3], ['rev-list', '--parents', '--max-count=3'])
        self.assertEqual(len(graph.output.splitlines()), 3)
        # Neither candidate can be inferred even from the boundary parents.
        self.assertNotIn(ancestor.encode(), graph.output.split())
        self.assertNotIn(unrelated.encode(), graph.output.split())
        self.assertEqual([row['reachability'] for row in result['items']],
                         ['reachable', 'not_reachable'])
        self.assertEqual([args for args, _ in calls[1:]],
                         [['merge-base', '--is-ancestor', ancestor, tip],
                          ['merge-base', '--is-ancestor', unrelated, tip]])
        self.assertEqual(result['metadata']['target_commit'], tip)
        self.assertNotIn('ancestry_scan_limit', result['metadata']['partial_reasons'])
        self.assertTrue(all(row['application_status'] == 'unknown' for row in result['items']))

    def test_bounded_graph_unknown_uses_only_unresolved_distinct_candidates(self):
        self.f.backend.limits['scan_limit'] = 1
        intermediate, unseen = 'a' * 40, 'b' * 40
        graph = f'{self.f.tip} {intermediate}\n{intermediate} {unseen}\n'.encode()
        with patch.object(self.f.backend, '_run',
                          side_effect=[GitResult(graph, 0), GitResult(b'', 0)]) as run:
            checked, reasons = self.reader._reachability(
                'tiny', self.f.tip, [self.f.old, intermediate, self.f.old])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[1],
                         ['merge-base', '--is-ancestor', self.f.old, self.f.tip])
        self.assertEqual(checked, {self.f.old: 'reachable', intermediate: 'reachable'})
        self.assertEqual(reasons, [])

    def test_truncated_graph_retains_positive_paths_but_not_negative_claims(self):
        graph = f'{self.f.tip} {self.f.old}\n{self.f.old}'.encode()
        with patch.object(self.f.backend, '_run',
                          return_value=GitResult(graph, -1, 'query_output_limit')) as run:
            checked, reasons = self.reader._reachability('tiny', self.f.tip, [self.f.old, 'a' * 40])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(checked, {self.f.old: 'reachable', 'a' * 40: 'unknown'})
        self.assertEqual(reasons, ['query_output_limit'])

    def test_fallback_cap_leaves_remaining_candidates_unknown(self):
        candidates = [f'{i:040x}' for i in range(1, MAX_ANCESTRY_FALLBACKS + 3)]
        graph = f'{self.f.tip} {"a" * 40}\n'.encode()
        with patch.object(self.f.backend, '_run',
                          side_effect=[GitResult(graph, 0)] +
                          [GitResult(b'', 1)] * MAX_ANCESTRY_FALLBACKS) as run:
            checked, reasons = self.reader._reachability('tiny', self.f.tip, candidates)
        self.assertEqual(run.call_count, 1 + MAX_ANCESTRY_FALLBACKS)
        self.assertEqual(list(checked.values()), ['not_reachable'] * MAX_ANCESTRY_FALLBACKS + ['unknown'] * 2)
        self.assertEqual(reasons, ['ancestry_scan_limit', 'ancestry_fallback_limit'])
        for call in run.call_args_list[1:]:
            self.assertTrue(call.kwargs['allow_one'])
            self.assertTrue(call.kwargs['allow_partial'])
            self.assertEqual(call.kwargs['accepted_returncodes'], (0, 1))

    def test_missing_candidate_does_not_hide_valid_candidate(self):
        missing = '0' * 40
        with patch.object(self.f.backend, '_run', wraps=self.f.backend._run) as run:
            with self.f.backend.read_guard():
                checked, reasons = self.reader._reachability('tiny', self.f.tip, [missing, self.f.old])
        self.assertEqual(checked, {missing: 'unknown', self.f.old: 'reachable'})
        self.assertEqual(run.call_count, 3)
        self.assertIn('code_backend_error', reasons)

    def test_fallback_errors_are_unknown_not_false(self):
        graph = f'{self.f.tip} {"a" * 40}\n'.encode()
        for failure in (MirrorError('code_backend_error', 'missing object', 503),
                        GitResult(b'', 128), GitResult(b'unexpected', 1),
                        GitResult(b'', -1, 'query_memory_limit')):
            with self.subTest(failure=failure):
                with patch.object(self.f.backend, '_run', side_effect=[GitResult(graph, 0), failure]):
                    checked, reasons = self.reader._reachability('tiny', self.f.tip, [self.f.old])
                self.assertEqual(checked, {self.f.old: 'unknown'})
                self.assertTrue(reasons)

    def test_cleanup_timeout_propagates_from_graph_and_fallback(self):
        graph = GitResult(f'{self.f.tip} {"a" * 40}\n'.encode(), 0)
        error = MirrorError('code_cleanup_timeout', 'process cleanup did not finish', 503)
        for answers in ([error], [graph, error]):
            with self.subTest(stage=len(answers)):
                with patch.object(self.f.backend, '_run', side_effect=answers):
                    with self.assertRaises(MirrorError) as raised:
                        self.reader.related_mail('tiny', self.f.old)
                self.assertEqual(raised.exception.code, 'code_cleanup_timeout')
                self.assertEqual(raised.exception.status, 503)

    def test_fallback_uses_remaining_request_output_not_a_new_budget(self):
        graph = f'{self.f.tip} {"a" * 40}\n'.encode()
        self.f.backend.limits['query_output_bytes'] = len(graph)
        with patch('lore_mirror.code_backend.bounded_git_run', return_value=GitResult(graph, 0)) as runner:
            with self.f.backend.read_guard():
                checked, reasons = self.reader._reachability('tiny', self.f.tip, [self.f.old, 'b' * 40])
                self.assertEqual(self.f.backend.local.output_remaining, 0)
        self.assertEqual(runner.call_count, 1, 'No fallback process may receive a fresh output budget')
        self.assertEqual(set(checked.values()), {'unknown'})
        self.assertIn('query_output_limit', reasons)

    def test_fallback_uses_remaining_request_deadline_not_a_new_timeout(self):
        graph = f'{self.f.tip} {"a" * 40}\n'.encode()
        def exhaust_deadline(*args, **kwargs):
            self.f.backend.local.deadline = time.monotonic() - 1
            return GitResult(graph, 0)
        with patch('lore_mirror.code_backend.bounded_git_run', side_effect=exhaust_deadline) as runner:
            with self.assertRaises(MirrorError) as raised:
                with self.f.backend.read_guard():
                    checked, reasons = self.reader._reachability('tiny', self.f.tip, [self.f.old, 'b' * 40])
        self.assertEqual(runner.call_count, 1, 'No fallback process may receive a fresh deadline')
        self.assertEqual(set(checked.values()), {'unknown'})
        self.assertIn('query_timeout', reasons)
        self.assertEqual(raised.exception.code, 'query_timeout')

    def test_empty_evidence_does_not_spawn_ancestry_process(self):
        with patch.object(self.f.backend, '_run', side_effect=AssertionError('unnecessary graph walk')):
            self.assertEqual(self.reader._reachability('tiny', self.f.tip, []), ({}, []))
    def test_invalid_or_missing_ancestry_is_unknown(self):
        self.rows[0]['commit']='0'*40
        r=self.reader.related_mail('tiny','0'*40)
        self.assertTrue(r['partial'])
        self.assertEqual(r['items'][0]['reachability'],'unknown')
    def test_exact_variant_accepts_existing_blob_and_raw_identity(self):
        for v in ('a'*40,'b'*64):
            self.reader.related_commits('p@example.org','tiny',variant=v)
        for v in ('HEAD','-h','a'*41,True):
            with self.assertRaises(MirrorError):self.reader.related_commits('p@example.org','tiny',variant=v)
    def test_malformed_evidence_rejected(self):
        self.rows[0]['evidence_level']='applied'
        with self.assertRaises(MirrorError):self.reader.related_mail('tiny',self.f.old)
    def test_unknown_scope_and_revision_expression_rejected(self):
        for args in [('tiny','HEAD'),('other',self.f.old),('tiny',self.f.old,'main')]:
            with self.assertRaises(MirrorError):self.reader.related_mail(*args)
    def test_old_catalog_evidence_rejected(self):
        self.rows[0]['catalog_fingerprint']='old'
        with self.assertRaises(MirrorError) as e:self.reader.related_mail('tiny',self.f.old)
        self.assertEqual(e.exception.code,'links_catalog_changed')
    def test_empty_is_qualified(self):
        self.store.rows=[]
        r=self.reader.related_mail('tiny',self.f.old)
        self.assertEqual(r['items'],[])
        self.assertIn('not proof',r['metadata']['negative_result'])
        self.assertTrue(r['partial'])
        self.assertEqual(r['metadata']['snapshot_coverage']['scan']['status'],'not_scanned')
    def test_disabled_never_reads_store(self):
        self.reader.enabled=False
        with self.assertRaises(MirrorError) as e:self.reader.related_mail('tiny',self.f.old)
        self.assertEqual(e.exception.code,'code_links_disabled')
    def test_service_cursors_bind_all_three_generations(self):
        from lore_mirror.service import MirrorService
        from lore_mirror.config import load_config
        cfg=copy.deepcopy(self.f.cfg)
        cfg.update(backend='fixture',runtime_dir=str(self.f.root/'runtime'),issue_dir=str(self.f.root/'issues'),lists=[])
        svc=MirrorService(cfg)
        svc.code_links._store=self.store
        self.store.rows=self.rows*3
        for component in ('links','mail','code'):
            with self.subTest(component=component):
                self.store.gen='1'
                self.f.publish(1)
                (self.f.data/'sync-state.json').write_text(json.dumps({'generation':1}))
                first=svc.invoke('mail.related_commits',{'message_id':'p@example.org','tree':'tiny','limit':1})
                token=first['next_cursor']
                self.assertTrue(token)
                if component=='links':self.store.gen='2'
                if component=='mail':(self.f.data/'sync-state.json').write_text(json.dumps({'generation':2}))
                if component=='code':self.f.publish(2)
                with self.assertRaises(MirrorError) as e:
                    svc.invoke('mail.related_commits',{'message_id':'p@example.org','tree':'tiny','limit':1,'cursor':token})
                self.assertEqual(e.exception.code,'stale_cursor')

    def test_publication_between_lookup_and_annotation_is_rejected(self):
        from lore_mirror.service import MirrorService
        cfg=copy.deepcopy(self.f.cfg)
        cfg.update(backend='fixture',runtime_dir=str(self.f.root/'runtime'),issue_dir=str(self.f.root/'issues'),lists=[])
        svc=MirrorService(cfg)
        svc.code_links._store=self.store
        original=self.store.related_commits
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            self.store.gen='2'
            return result
        with patch.object(self.store,'related_commits',side_effect=changed):
            with self.assertRaises(MirrorError) as e:
                svc.invoke('mail.related_commits',{'message_id':'p@example.org','tree':'tiny'})
        self.assertEqual(e.exception.code,'stale_links_generation')

    def test_mcp_tools_opt_in_and_read_only(self):
        import asyncio
        from lore_mirror.service import MirrorService
        from lore_mirror.server import make_mcp
        cfg=copy.deepcopy(self.f.cfg)
        cfg.update(backend='fixture',runtime_dir=str(self.f.root/'runtime'),issue_dir=str(self.f.root/'issues'),lists=[])
        svc=MirrorService(cfg)
        listed={t.name:t for t in asyncio.run(make_mcp(svc).list_tools())}
        for name in ('code.related_mail','mail.related_commits'):
            self.assertIn(name,listed)
            self.assertTrue(listed[name].annotations.readOnlyHint)
            self.assertFalse(listed[name].annotations.openWorldHint)
        cfg['code']['links']['enabled']=False
        listed={t.name for t in asyncio.run(make_mcp(MirrorService(cfg)).list_tools())}
        self.assertNotIn('code.related_mail',listed)
        self.assertNotIn('mail.related_commits',listed)

    def test_same_heavy_io_lock_blocks_both_link_directions(self):
        import fcntl
        with (self.f.data/'index.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            for fn,args in ((self.reader.related_mail,('tiny',self.f.old)),
                            (self.reader.related_commits,('p@example.org','tiny'))):
                with self.assertRaises(MirrorError) as e:fn(*args)
                self.assertEqual(e.exception.code,'busy')
    def test_wrong_identity_rows_fail_closed(self):
        self.rows[0]['message_id']='other@example.org'
        with self.assertRaises(MirrorError):self.reader.related_commits('p@example.org','tiny')
        self.rows[0]['commit']=self.f.tip
        with self.assertRaises(MirrorError):self.reader.related_mail('tiny',self.f.old)
    def test_unknown_evidence_shape_fails_closed(self):
        self.store.rows=[None]
        with self.assertRaises(MirrorError):self.reader.related_mail('tiny',self.f.old)

class LinkServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests import test_code_links as builder_fixtures
        self.f=builder_fixtures.LinkTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
    def test_real_database_both_tools_exact_snapshot_and_equivalence(self):
        from lore_mirror.service import MirrorService
        result=self.f.run_once()
        self.assertEqual(result['status'],'ready',result)
        cfg=copy.deepcopy(self.f.config)
        cfg.update(backend='fixture',runtime_dir=str(self.f.root/'runtime'),issue_dir=str(self.f.root/'issues'),lists=[])
        svc=MirrorService(cfg)
        for name,args in (
            ('code.related_mail',{'tree':'boot','commit':self.f.oid}),
            ('mail.related_commits',{'tree':'boot','message_id':'patch@example.invalid'})):
            with self.subTest(tool=name):
                result=svc.invoke(name,args)
                self.assertEqual({x['evidence_level'] for x in result['items']},{'linked','patch_equivalent'})
                self.assertTrue(result['partial'])
                self.assertEqual(result['target_commit'],self.f.oid)
                self.assertTrue(result['snapshot_coverage']['complete'])
                for row in result['items']:
                    self.assertEqual(row['reachability'],'reachable')
                    self.assertEqual(row['application_status'],'unknown')
                    self.assertEqual(row['current_mail_membership'],'not_verified')
    def test_missing_database_does_not_break_mail_status(self):
        from lore_mirror.service import MirrorService
        cfg=copy.deepcopy(self.f.config)
        cfg.update(backend='fixture',runtime_dir=str(self.f.root/'runtime'),issue_dir=str(self.f.root/'issues'),lists=[])
        (self.f.data/'index.lock').touch()
        svc=MirrorService(cfg)
        svc.invoke('mail.status')
        self.assertEqual(svc.invoke('code.status')['links']['status'],'unavailable')
        with self.assertRaises(MirrorError) as e:
            svc.invoke('mail.related_commits',{'tree':'boot','message_id':'patch@example.invalid'})
        self.assertEqual(e.exception.code,'links_not_ready')
