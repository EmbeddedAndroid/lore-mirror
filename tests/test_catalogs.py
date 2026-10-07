"""Offline integrity and project-specific classification of pinned text catalogs."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from lore_mirror.backend import FixtureBackend
from lore_mirror.service import MirrorService
from lore_mirror.config import load_config
from lore_mirror.errors import MirrorError
from lore_mirror.semantics import classify_subsystems
ROOT=Path(__file__).resolve().parents[1]
SOURCES=json.loads((ROOT/'config/maintainers/sources.json').read_text())

class CatalogTests(unittest.TestCase):
    def test_every_pinned_file_matches_size_and_hash(self):
        for project in SOURCES['projects']:
            for item in [project['catalog'],project['commit_metadata'],*project['supporting_files']]:
                with self.subTest(path=item['path']):
                    data=(ROOT/item['path']).read_bytes()
                    self.assertEqual(len(data),item['byte_length'])
                    self.assertEqual(hashlib.sha256(data).hexdigest(),item['sha256'])
                    data.decode('utf-8')
    def test_linux_and_uboot_rules_have_distinct_names(self):
        message={'paths':['drivers/net/designware.c']}
        ub=(ROOT/'config/maintainers/u-boot/MAINTAINERS').read_text()
        linux=(ROOT/'config/maintainers/linux/MAINTAINERS').read_text()
        self.assertIn('NETWORK',{x['name'] for x in classify_subsystems(message,ub)})
        self.assertNotIn('NETWORK',{x['name'] for x in classify_subsystems(message,linux)})
    def test_in_flight_uses_class_catalog_and_returns_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=load_config({'data_dir':tmp,'backend':'fixture','lists':[{'id':'u-boot-amlogic','class':'u-boot'}]})
            fixture=ROOT/'tests/fixtures/real-pilot';FixtureBackend(cfg).import_paths(fixture)
            cfg['maintainers_catalogs']={}
            for p in SOURCES['projects']:
                key='kernel' if p['project']=='linux' else p['project']
                cfg['maintainers_catalogs'][key]={'path':str(ROOT/p['catalog']['path']),'project':p['project'],
                    'commit':p['commit'],'sha256':p['catalog']['sha256']}
            service=MirrorService(cfg)
            result=service.invoke('mail.in_flight',{'paths':['drivers/net/*.c'],'subsystem':'NETWORK','since':'2026-10-01'})
            self.assertEqual(result['matched_count'],2)
            self.assertFalse(result['classification_complete'])
            self.assertEqual(set(result['maintainers_provenance']),{'u-boot'})
            for row in result['items']:
                self.assertTrue(row['subsystems'])
                self.assertTrue(all(x['source_provenance']['project']=='u-boot' for x in row['subsystems']))
            service.cfg['maintainers_catalogs']['u-boot']['sha256']='0'*64
            with self.assertRaises(MirrorError) as error:
                service.invoke('mail.in_flight',{'subsystem':'NETWORK','since':'2026-10-01'})
            self.assertEqual(error.exception.code,'catalog_integrity')

if __name__=='__main__':unittest.main()
