import os
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class VerificationEntrypointTests(unittest.TestCase):
    def test_docker_mode_does_not_require_host_application_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);log=root/'calls';docker=root/'docker'
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_TEST_LOG"\n')
            docker.chmod(0o700)
            env=os.environ|{'PATH':str(root)+':/usr/bin:/bin','PYTHON':'/definitely/not/a/python','DOCKER_TEST_LOG':str(log)}
            result=subprocess.run(['bash',str(ROOT/'scripts/verify.sh'),'--docker'],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            calls=log.read_text();self.assertIn('compose build',calls)
            self.assertIn('LORE_NATIVE_E2E_REQUIRED=1',calls)
            self.assertIn('unittest discover',calls)
    def test_unknown_option_rejected_before_tests(self):
        result=subprocess.run(['bash',str(ROOT/'scripts/verify.sh'),'--unknown'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertIn('Usage:',result.stderr)
