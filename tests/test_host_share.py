"""Validate argv construction and rejection before emulator launch."""
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HostShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.share = self.path / 'folder with spaces'
        self.share.mkdir()
        self.qemu = self.path / 'qemu'
        self.qemu.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        self.qemu.chmod(0o755)
        self.image = self.path / 'Image'
        self.image.write_bytes(b'kernel')
        self.mram = self.path / 'mram'

    def run_guest(self, *args):
        return subprocess.run(['bash', str(ROOT / 'scripts/run-linux.sh'),
                               '--mram', str(self.mram), *map(str, args)],
                              env=dict(os.environ, QEMU=str(self.qemu), IMAGE=str(self.image)),
                              capture_output=True, text=True, timeout=5)

    def test_readonly_default_and_rw_explicit(self):
        for flag, mode, readonly in [('--share', 'ro', 'on'), ('--share-rw', 'rw', 'off')]:
            with self.subTest(mode=mode):
                p = self.run_guest(flag, self.share, '--uart-socket', '/tmp/uart')
                self.assertEqual(p.returncode, 0, p.stderr)
                args = json.loads(p.stdout)
                self.assertEqual(args[args.index('-fsdev') + 1],
                                 f'local,id=hostshare,path={self.share},security_model=mapped-xattr,readonly={readonly},multidevs=remap')
                self.assertEqual(args[args.index('-accel') + 1], 'tcg,thread=single')
                self.assertIn('virtio-9p-device,fsdev=hostshare,mount_tag=hostshare', args)
                self.assertIn(f'erbium.share={mode}', args[args.index('-append') + 1])
                self.assertIn('chardev:erb-uart', args)

    def test_no_share_is_opt_in(self):
        args = json.loads(self.run_guest().stdout)
        self.assertNotIn('-fsdev', args)
        self.assertNotIn('-accel', args)
        self.assertNotIn('erbium.share=', ' '.join(args))

    def test_symlink_is_canonicalized(self):
        link = self.path / 'link'
        link.symlink_to(self.share, target_is_directory=True)
        p = self.run_guest('--share', link)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f'path={self.share},', p.stdout)

    def test_relative_share_ignores_cdpath(self):
        alternate = self.path / 'alternate'
        alternate.mkdir()
        (alternate / self.share.name).mkdir()
        p = subprocess.run(['bash', str(ROOT / 'scripts/run-linux.sh'),
                            '--mram', str(self.mram), '--share', self.share.name],
                           cwd=self.path, env=dict(os.environ, QEMU=str(self.qemu),
                           IMAGE=str(self.image), CDPATH=str(alternate)),
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f'path={self.share},', p.stdout)

    def test_reject_invalid_before_mram_creation(self):
        comma = self.path / 'bad,readonly=off'
        comma.mkdir()
        newline = self.path / 'bad\npath'
        newline.mkdir()
        alias = self.path / 'innocent'
        alias.symlink_to(comma, target_is_directory=True)
        # A symlink whose physical target ends in newline must not silently
        # export the existing sibling whose name lacks that newline.
        trailing = self.path / 'selected\n'
        trailing.mkdir()
        (self.path / 'selected').mkdir()
        trailing_alias = self.path / 'trailing-alias'
        trailing_alias.symlink_to(trailing, target_is_directory=True)
        cases = [('--share-rw', trailing_alias), ('--share',), ('--share', ''), ('--share', self.path / 'missing'),
                 ('--share', self.image), ('--share', comma), ('--share', newline),
                 ('--share', alias), ('--share', self.share, '--share-rw', self.share)]
        for args in cases:
            with self.subTest(args=args):
                p = self.run_guest(*args)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertFalse(self.mram.exists())

    def test_uart_rejects_bad_share_before_backend_launch(self):
        for args in [('--share',), ('--share', '/nonexistent-share-directory'),
                     ('--share', self.share, '--share-rw', self.share)]:
            p = subprocess.run(['bash', str(ROOT / 'scripts/run-uart.sh'), *map(str, args)],
                               env=dict(os.environ, EMU='/missing', QEMU='/missing'),
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertNotIn('Missing erbium_emu', p.stderr)

    def test_share_test_kills_term_resistant_qemu(self):
        marker = self.path / 'qemu.pid'
        self.qemu.write_text("""#!/usr/bin/env python3
import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ['QEMU_PID_MARKER']).write_text(str(os.getpid()))
print('ERBIUM-SHARE-READY', flush=True)
while True: time.sleep(1)
""")
        timeout = self.path / 'timeout'
        timeout.write_text("""#!/usr/bin/env python3
import os,sys
args=sys.argv[1:]
if '-k' in args: args[args.index('-k')+1]='0.2'
args[args.index('90')]='0.2'
os.execv('/usr/bin/timeout', ['timeout', *args])
""")
        timeout.chmod(0o755)
        p = subprocess.Popen(['bash', str(ROOT / 'scripts/run-share-test.sh')],
                             env=dict(os.environ, PATH=str(self.path)+':'+os.environ['PATH'],
                                      QEMU=str(self.qemu), IMAGE=str(self.image),
                                      QEMU_PID_MARKER=str(marker)),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = p.communicate(timeout=5)
            self.assertNotEqual(p.returncode, 0, out+err)
            self.assertTrue(marker.exists())
            pid = int(marker.read_text())
            state = Path(f'/proc/{pid}/stat')
            for _ in range(30):
                try:
                    if state.read_text().split()[2] == 'Z': break
                except FileNotFoundError:
                    break
                time.sleep(.05)
            else: self.fail('QEMU survived timeout kill escalation')
        finally:
            if p.poll() is None:
                p.terminate()
                p.communicate(timeout=3)
            if marker.exists():
                try: os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass


if __name__ == '__main__':
    unittest.main()
