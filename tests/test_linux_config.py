"""Exercise real Kconfig in an isolated checkout (no kernel Image compilation)."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / 'ext/linux'


@unittest.skipUnless((KERNEL / 'Makefile').exists(), 'fetch kernel sources first')
class LinuxConfigTests(unittest.TestCase):
    def test_fresh_configuration_and_repair_cached_placeholder(self):
        with tempfile.TemporaryDirectory(prefix='erbium-config-') as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'linux').mkdir()
            (root / 'ext').mkdir()
            (root / 'ext/linux').symlink_to(KERNEL)
            shutil.copy(ROOT / 'scripts/configure-linux.sh', root / 'scripts')
            for name in ('erbium.config', 'initramfs.list'):
                shutil.copy(ROOT / 'linux' / name, root / 'linux')

            def configure(payload=""):
                env = dict(os.environ, ERBIUM_ELF=str(payload))
                result = subprocess.run(['bash', str(root / 'scripts/configure-linux.sh')],
                                        cwd='/', env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for name in ('erbium.config', 'initramfs.list', 'linux/.config',
                             'linux/include/config/auto.conf'):
                    text = (root / 'build' / name).read_text()
                    self.assertNotIn('@R@', text)
                    self.assertIn(str(root), text)
                self.assertIn("CONFIG_FILE_LOCKING=y", (root / "build/linux/.config").read_text())

            configure()
            config = root / 'build/linux/.config'
            # Reproduce the bad input, including Kconfig's generated cache.
            config.write_text(config.read_text().replace(str(root), '@R@').replace(
                "CONFIG_FILE_LOCKING=y", "# CONFIG_FILE_LOCKING is not set"))
            result = subprocess.run(['make', '-s', '-C', str(KERNEL),
                                     f'O={root}/build/linux', 'ARCH=arm64',
                                     'CROSS_COMPILE=aarch64-linux-gnu-', 'syncconfig'],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('@R@', (root / 'build/linux/include/config/auto.conf').read_text())
            configure()
            payload = root / 'user image.elf'
            payload.write_bytes(b'optional payload')
            configure(payload)
            self.assertEqual((root / 'build/host-payload.elf').read_bytes(), payload.read_bytes())
            manifest = (root / 'build/initramfs.list').read_text()
            self.assertIn('/firmware/host-payload.elf', manifest)
            self.assertNotIn('user image.elf', manifest)
            configure()
            self.assertNotIn('/firmware/host-payload.elf', (root / 'build/initramfs.list').read_text())


if __name__ == '__main__':
    unittest.main()
