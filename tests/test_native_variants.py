"""Raw transport variants must survive public-inbox content deduplication."""
from __future__ import annotations
import hashlib
from pathlib import Path
import unittest
import test_native_e2e as _native
from test_native_e2e import FIXTURE, IMPORT_PERL

class NativeVariantTests(unittest.TestCase):
    run_native=classmethod(_native.NativePublicInboxE2ETests.run_native.__func__)
    @classmethod
    def setUpClass(cls):
        _native.NativePublicInboxE2ETests.setUpClass.__func__(cls)
        cls.original=(FIXTURE/'01.eml').read_bytes()
        cls.alternate=b'X-Audit-Transport: alternate-list-copy\n'+cls.original
        path=cls.root/'alternate.eml';path.write_bytes(cls.alternate)
        cls.run_native('perl','-e',IMPORT_PERL,'devicetree-spec',str(path))
        inbox=cls.lists[1]['inbox_path']
        cls.run_native('public-inbox-index','-L','basic','--jobs=0',inbox)
        cls.run_native('public-inbox-extindex','-L','full','--jobs=0',str(cls.extindex),inbox)
    def test_same_content_different_transport_headers_expose_both_raw_variants(self):
        rows=self.service.backend.get(self.mids[0])
        self.assertEqual(len(rows),2)
        actual={hashlib.sha256(self.service.backend.raw(r)).hexdigest():r for r in rows}
        self.assertEqual(set(actual),{hashlib.sha256(self.original).hexdigest(),hashlib.sha256(self.alternate).hexdigest()})
        self.assertEqual(actual[hashlib.sha256(self.original).hexdigest()]['list_ids'],['u-boot-amlogic'])
        self.assertEqual(actual[hashlib.sha256(self.alternate).hexdigest()]['list_ids'],['devicetree-spec'])
        for row in rows:
            read=self.service.invoke('mail.get_message',{'message_id':self.mids[0],'variant':row['blob'],'part':'raw'})
            self.assertEqual(read['variant_count'],2)
            self.assertFalse(read['variants_partial'])
            self.assertEqual(read['message']['list_ids'],row['list_ids'])
    def test_scoped_search_selects_raw_blob_in_requested_list(self):
        result=self.service.invoke('mail.search',{'query':'mid:'+self.mids[0],'lists':['devicetree-spec'],'limit':1})
        self.assertEqual(len(result['items']),1)
        row=result['items'][0]
        expected=hashlib.sha1(b'blob '+str(len(self.alternate)).encode()+b'\0'+self.alternate).hexdigest()
        self.assertEqual(row['variant'],expected)
        message=self.service.invoke('mail.get_message',{'message_id':self.mids[0],'variant':row['variant'],'part':'raw'})
        self.assertEqual(message['message']['list_ids'],['devicetree-spec'])

    def test_unknown_variant_is_not_another_message(self):
        from lore_mirror.errors import MirrorError
        with self.assertRaises(MirrorError) as error:
            self.service.invoke('mail.get_message',{'message_id':self.mids[0],'variant':'f'*40})
        self.assertEqual(error.exception.code,'variant_not_found')

if __name__=='__main__':unittest.main()
