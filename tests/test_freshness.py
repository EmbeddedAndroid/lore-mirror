import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from lore_mirror.service import MirrorService

class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.service=MirrorService({'data_dir':str(self.root),'backend':'fixture','interval_seconds':3600})
    def checkpoint(self,age):
        timestamp=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age)).isoformat()
        (self.root/'sync-state.json').write_text(json.dumps({'last_success':timestamp}))
    def test_unknown_without_success_is_not_fresh(self):
        self.assertIsNone(self.service.status()['freshness']['stale'])
    def test_age_and_staleness_use_success_not_recent_failure(self):
        self.checkpoint(8000)
        state=self.service.status()['freshness'];self.assertTrue(state['stale']);self.assertGreaterEqual(state['age_seconds'],8000)
        self.checkpoint(300);self.assertFalse(self.service.status()['freshness']['stale'])
    def test_future_checkpoint_is_unknown(self):
        self.checkpoint(-3600);self.assertIsNone(self.service.status()['freshness']['stale'])
    def test_daemon_state_is_bounded_recorded_state_not_liveness(self):
        (self.root/'daemon-state.json').write_text(json.dumps({'status':'sleeping','next_scheduled_at':'2026-10-07T17:00:00+00:00','pid':123,'private_path':'/do/not/expose'}))
        state=self.service.status()['daemon']
        self.assertEqual(state['status'],'sleeping');self.assertNotIn('private_path',state);self.assertNotIn('pid',state)
        self.assertIn('not a live process probe',state['observation'])
        (self.root/'daemon-state.json').write_text('x'*65537)
        self.assertEqual(self.service.status()['daemon'],{'error':'invalid daemon state'})
