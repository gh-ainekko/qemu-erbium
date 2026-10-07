#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The pinned fetcher must refuse to overwrite unrelated or modified checkouts."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('SMALLVM_SRC', ROOT/'ext/smallvm'))


class FetchSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='smallvm-fetch-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'scripts').mkdir()
        shutil.copy2(ROOT/'scripts/fetch-smallvm.sh', self.root/'scripts')
        shutil.copytree(ROOT/'smallvm/patches', self.root/'smallvm/patches')
        self.src = self.root/'checkout'
        subprocess.run(['git', 'clone', '-q', '--shared', str(SOURCE), str(self.src)], check=True)
        shutil.copy2(SOURCE/'.git/erbium-source-lock', self.src/'.git/erbium-source-lock')

    def fetch(self):
        return subprocess.run(['bash', str(self.root/'scripts/fetch-smallvm.sh')],
            env={**os.environ, 'SMALLVM_SRC': str(self.src)}, text=True, capture_output=True)

    def test_clean_checkout_is_idempotent(self):
        self.assertEqual(self.fetch().returncode, 0)
        self.assertEqual(self.fetch().returncode, 0)

    def test_untracked_work_is_preserved(self):
        work = self.src/'untracked-owner-work'
        work.write_text('do not overwrite')
        result = self.fetch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Refusing to overwrite', result.stderr)
        self.assertEqual(work.read_text(), 'do not overwrite')

    def test_committed_tree_change_is_detected(self):
        work = self.src/'owner-work'
        work.write_text('committed work')
        subprocess.run(['git','-C',str(self.src),'add','owner-work'],check=True)
        subprocess.run(['git','-C',str(self.src),'-c','user.name=test','-c','user.email=test@example.invalid',
                        'commit','-qm','Owner change'],check=True)
        self.assertNotEqual(self.fetch().returncode, 0)
        self.assertEqual(work.read_text(), 'committed work')

    def test_changed_patch_series_is_detected(self):
        patch = sorted((self.root/'smallvm/patches').glob('*.patch'))[0]
        patch.write_bytes(patch.read_bytes()+b'\n')
        self.assertNotEqual(self.fetch().returncode, 0)


if __name__ == '__main__':
    unittest.main()
