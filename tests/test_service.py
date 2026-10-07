"""Offline service invariants over committed real-mail fixtures and synthetic edges."""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from lore_mirror.config import load_config
from lore_mirror.backend import FixtureBackend, bounded_run
from lore_mirror.service import MirrorService, encoded
from lore_mirror.errors import MirrorError

FIXTURES=Path(__file__).parent/'fixtures'/'real-pilot'
MID='20261005135406.509901-1-sanderson@brivo.com'

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.cfg=load_config({'data_dir':str(root/'mail'),'runtime_dir':str(root/'runtime'),'issue_dir':str(root/'issues'),
            'backend':'fixture','lists':[{'id':'u-boot-amlogic','class':'u-boot','aliases':['amlogic']}]})
        FixtureBackend(self.cfg).import_paths(FIXTURES)
        self.service=MirrorService(self.cfg)
    def tearDown(self):self.tmp.cleanup()
    def collect(self,tool,args):
        rows=[];cursor=None
        for _ in range(1000):
            result=self.service.invoke(tool,args|({'cursor':cursor} if cursor else {}))
            self.assertLess(len(encoded(result)),self.cfg['limits']['response_bytes'])
            rows.extend(result['items']);cursor=result.get('next_cursor')
            if not cursor:return rows,result
        self.fail('continuation did not terminate')
    def test_real_fixture_provenance_hashes(self):
        for entry in json.loads((FIXTURES/'manifest.json').read_text())['messages']:
            self.assertEqual(hashlib.sha256((FIXTURES/entry['file']).read_bytes()).hexdigest(),entry['sha256'])
    def test_search_scope_and_pagination(self):
        rows,_=self.collect('mail.search',{'query':'f:sanderson@brivo.com','lists':['amlogic'],'limit':1})
        self.assertEqual(len(rows),3);self.assertEqual(len({r['message_id'] for r in rows}),3)
        self.assertEqual(self.service.invoke('mail.search',{'query':'','class_filter':'u-boot'})['matched_count'],3)
        with self.assertRaises(MirrorError):self.service.invoke('mail.search',{'lists':['unmirrored']})
    def test_thread_continuation_and_unknown_id(self):
        rows,_=self.collect('mail.get_thread',{'message_id':MID,'limit':1})
        self.assertEqual(len(rows),3)
        with self.assertRaises(MirrorError) as error:self.service.invoke('mail.get_thread',{'message_id':'absent@example.invalid'})
        self.assertEqual(error.exception.code,'not_found')
    def test_raw_continuation_reassembles_exact_bytes(self):
        chunks,result=self.collect('mail.get_message',{'message_id':MID,'part':'raw','limit':127})
        raw=b''.join(base64.b64decode(x['raw_base64']) for x in chunks)
        self.assertEqual(raw,(FIXTURES/'01.eml').read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),result['raw_sha256'])
    def test_unicode_message_chunks_advance_without_corruption(self):
        path=Path(self.tmp.name)/'unicode.eml'
        path.write_text('Message-ID: <unicode@example.invalid>\nFrom: A <a@example.invalid>\nSubject: Unicode\nContent-Type: text/plain; charset=utf-8\n\n🙂日本語abc\n',encoding='utf-8')
        FixtureBackend(self.cfg).import_paths(path)
        rows,_=self.collect('mail.get_message',{'message_id':'unicode@example.invalid','limit':4})
        self.assertEqual(''.join(x['text'] for x in rows),'🙂日本語abc\n')
        self.assertTrue(all(len(x['text'].encode())<=4 for x in rows))
    def test_message_cursor_is_bound_and_tamper_safe(self):
        args={'message_id':MID,'limit':20}
        first=self.service.invoke('mail.get_message',args)
        token=first['next_cursor'];self.assertTrue(token)
        for changed in ({'part':'headers'},{'limit':21},{'cursor':token[:-1]+'Z'}):
            with self.assertRaises(MirrorError):self.service.invoke('mail.get_message',args|{'cursor':token}|changed)
        state=Path(self.cfg['data_dir'])/'sync-state.json';state.write_text('{"generation":999}')
        with self.assertRaises(MirrorError) as error:self.service.invoke('mail.get_message',args|{'cursor':token})
        self.assertEqual(error.exception.code,'stale_cursor')
    def test_series_is_flat_pageable_and_has_trailer_provenance(self):
        rows,result=self.collect('mail.get_series',{'message_id':MID,'limit':1})
        self.assertEqual(len([x for x in rows if x['kind']=='cover']),1)
        self.assertEqual(len([x for x in rows if x['kind']=='patch']),2)
        self.assertTrue(any(x['kind']=='trailer' for x in rows))
        self.assertFalse(result['version_discovery']['complete'])
        self.assertEqual(result['applied_status'],'unknown')
    def test_posted_patch_and_invalid_diff_do_not_claim_applied(self):
        patchmid=MID.replace('-1-','-2-')
        msg,_,_=self.service._message(patchmid)
        out=self.service.invoke('mail.was_posted',{'patch_id':msg['patch_id']})
        self.assertEqual(out['matched_count'],1);self.assertEqual(out['applied_status'],'unknown')
        with self.assertRaises(MirrorError) as error:self.service.invoke('mail.was_posted',{'diff':'not a patch'})
        self.assertEqual(error.exception.code,'patch_id_unavailable')
    def test_in_flight_matches_exact_path(self):
        rows,_=self.collect('mail.in_flight',{'paths':['drivers/net/designware.c'],'since':'2026-10-01','limit':1})
        self.assertEqual(len(rows),2)
        self.assertTrue(all(x['applied_status']=='unknown' for x in rows))
    def test_limit_rejected_before_backend_scan(self):
        with patch.object(self.service.backend,'search') as backend:
            with self.assertRaises(MirrorError):self.service.invoke('mail.search',{'limit':-1})
            backend.assert_not_called()
    def test_process_timeout_includes_blocked_stdin(self):
        started=time.monotonic()
        with self.assertRaises(MirrorError) as error:
            bounded_run([sys.executable,'-c','import time;time.sleep(20)'],input_bytes=b'x'*65536,timeout=.1)
        self.assertEqual(error.exception.code,'timeout');self.assertLess(time.monotonic()-started,3)
    def test_no_archive_mutation_from_reads(self):
        db=Path(self.cfg['data_dir'])/'fixtures.sqlite3';before=hashlib.sha256(db.read_bytes()).digest()
        self.service.invoke('mail.search',{'query':'s:designware'})
        self.service.invoke('mail.get_message',{'message_id':MID})
        self.service.invoke('mail.get_series',{'message_id':MID})
        self.assertEqual(hashlib.sha256(db.read_bytes()).digest(),before)

if __name__=='__main__':unittest.main()
