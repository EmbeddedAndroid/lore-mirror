"""Prove local exact-ID aliases without a query-to-whole-inbox fallback."""
import hashlib
import json
from pathlib import Path
import unittest
import test_native_e2e as _native
from test_native_e2e import IMPORT_PERL
from lore_mirror.errors import MirrorError
FIXTURE=Path(__file__).parent/'fixtures/alias-reproduction'
MANIFEST=json.loads((FIXTURE/'manifest.json').read_text())

class NativeAliasTests(unittest.TestCase):
    run_native=classmethod(_native.NativePublicInboxE2ETests.run_native.__func__)
    @classmethod
    def setUpClass(cls):
        _native.NativePublicInboxE2ETests.setUpClass.__func__(cls)
        for group,name in [('u-boot-amlogic','01.eml'),('devicetree-spec','02.eml')]:
            cls.run_native('perl','-e',IMPORT_PERL,group,str(FIXTURE/name))
            inbox=str(cls.root/'inboxes'/group)
            cls.run_native('public-inbox-index','-L','basic','--jobs=0',inbox)
            cls.run_native('public-inbox-extindex','-L','full','--jobs=0',str(cls.extindex),inbox)
        cls.alias=MANIFEST['alternate_message_id'];cls.canonical=MANIFEST['canonical_message_id']
    def test_alias_exact_read_uses_local_overview_and_raw_header_evidence(self):
        native,_=self.service.backend.search('mid:'+self.alias,10)
        self.assertEqual(native,[],'Fixture should reproduce an alias absent from native full-text mid terms')
        result=self.service.invoke('mail.get_message',{'message_id':self.alias,'part':'raw'})
        self.assertEqual(result['raw_sha256'],MANIFEST['messages'][1]['sha256'])
        self.assertEqual(result['indexed_primary_message_id'],self.canonical)
        self.assertEqual(set(result['raw_message_ids']),{self.canonical,self.alias})
        self.assertIn('conflicting_message_id_headers',result['parse_warnings'])
        self.assertEqual(result['message']['message_id'],self.alias)
    def test_alias_thread_and_series_resolution_is_explicit(self):
        thread=self.service.invoke('mail.get_thread',{'message_id':self.alias})
        self.assertTrue(thread['items']);self.assertTrue(thread['partial'])
        self.assertEqual(thread['resolved_thread_anchor'],self.canonical)
        series=self.service.invoke('mail.get_series',{'message_id':self.alias})
        self.assertTrue(series['items']);self.assertTrue(series['partial'])
        self.assertEqual(series['resolved_series_anchor'],self.canonical)
    def test_alias_posted_evidence_is_not_false_negative(self):
        result=self.service.invoke('mail.was_posted',{'message_id':self.alias})
        self.assertEqual(result['posted_result'],'matches_found')
        self.assertEqual(result['items'][0]['evidence_type'],'exact_archive_message_id_alias')
        self.assertEqual(result['applied_status'],'unknown')
    def test_truly_unknown_id_stays_unknown(self):
        with self.assertRaises(MirrorError) as error:self.service.invoke('mail.get_message',{'message_id':'never-present@fixture.invalid'})
        self.assertEqual(error.exception.code,'not_found')

if __name__=='__main__':unittest.main()
