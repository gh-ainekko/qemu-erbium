"""Exercise real Kconfig and QEMU configure caching without compiling images."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / 'ext/linux'
SHARE_SYMBOLS = ('NET', 'NET_9P', 'NET_9P_VIRTIO', 'NETWORK_FILESYSTEMS', '9P_FS', 'VIRTIO_MENU',
                 'VIRTIO', 'VIRTIO_MMIO', 'NETFS_SUPPORT')


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
                for name in ('linux/.config', 'linux/include/config/auto.conf'):
                    lines = (root / 'build' / name).read_text().splitlines()
                    for symbol in ('FILE_LOCKING', *SHARE_SYMBOLS):
                        self.assertIn(f'CONFIG_{symbol}=y', lines)
                    # NET is only required for 9P, not for IP/sockets/NICs.
                    for symbol in ('INET', 'IPV6', 'UNIX', 'NETDEVICES', 'NET_9P_FD'):
                        self.assertNotIn(f'CONFIG_{symbol}=y', lines)
                        self.assertNotIn(f'CONFIG_{symbol}=m', lines)

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
            # Reproduce an existing allnoconfig-era build, in which NET and the
            # virtio menu were disabled. Also preserve an unrelated customization.
            result = subprocess.run(
                [str(KERNEL / 'scripts/config'), '--file', str(config),
                 '--disable', 'NET', '--disable', 'NET_9P',
                 '--disable', 'NET_9P_VIRTIO', '--disable', '9P_FS',
                 '--disable', 'NETWORK_FILESYSTEMS',
                 '--disable', 'VIRTIO_MENU', '--disable', 'VIRTIO',
                 '--disable', 'VIRTIO_MMIO', '--set-str', 'DEFAULT_HOSTNAME', 'custom-guest'],
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run(
                ['make', '-s', '-C', str(KERNEL), f'O={root}/build/linux',
                 'ARCH=arm64', 'CROSS_COMPILE=aarch64-linux-gnu-', 'olddefconfig', 'syncconfig'],
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('CONFIG_NET=y', config.read_text())
            self.assertNotIn('CONFIG_VIRTIO_MMIO=y', config.read_text())
            configure()
            self.assertIn('CONFIG_DEFAULT_HOSTNAME="custom-guest"', config.read_text())
            upgraded = config.read_text()
            configure()
            self.assertEqual(config.read_text(), upgraded)
            payload = root / 'user image.elf'
            payload.write_bytes(b'optional payload')
            configure(payload)
            self.assertEqual((root / 'build/host-payload.elf').read_bytes(), payload.read_bytes())
            manifest = (root / 'build/initramfs.list').read_text()
            self.assertIn('/firmware/host-payload.elf', manifest)
            self.assertNotIn('user image.elf', manifest)
            configure()
            self.assertNotIn('/firmware/host-payload.elf', (root / 'build/initramfs.list').read_text())


class QemuConfigureCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='erbium-qemu-config-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'ext/qemu/build').mkdir(parents=True)
        (self.root / 'tools').mkdir()
        shutil.copy(ROOT / 'scripts/build-all.sh', self.root / 'scripts')
        self.executable('scripts/preflight.sh', '#!/bin/bash\nexit 0\n')
        self.executable('ext/qemu/configure', '''#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$QEMU_TEST_ROOT/configure-calls"
case " $* " in *" --fake-configure-failure "*) exit 1;; esac
touch config.status
case " $* " in
  *" --disable-virtfs "*) echo '#undef CONFIG_VIRTFS' > config-host.h;;
  *) echo '#define CONFIG_VIRTFS' > config-host.h;;
esac
''')
        self.executable('tools/ninja', '''#!/bin/bash
set -eu
mkdir -p "$QEMU_TEST_ROOT/dist/share/qemu" "$QEMU_TEST_ROOT/ext/qemu/build/tests/qtest"
touch "$QEMU_TEST_ROOT/ext/qemu/build/tests/qtest/erbium-xspi-test"
''')
        self.env = dict(os.environ, J='2', QEMU_TEST_ROOT=str(self.root),
                        PATH=f'{self.root}/tools:{os.environ["PATH"]}',
                        QEMU_CONFIGURE_EXTRA='')

    def executable(self, name, text):
        path = self.root / name
        path.write_text(text)
        path.chmod(0o755)

    def build(self, extra='', success=True):
        result = subprocess.run(
            ['bash', str(self.root / 'scripts/build-all.sh'), 'qemu'], cwd='/',
            env=dict(self.env, QEMU_CONFIGURE_EXTRA=extra), text=True, capture_output=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self):
        return (self.root / 'configure-calls').read_text().splitlines()

    def test_upgrade_existing_configuration_and_cache_command(self):
        build = self.root / 'ext/qemu/build'
        # An older checkout had config.status, but neither virtfs nor a fingerprint.
        (build / 'config.status').touch()
        (build / 'config-host.h').write_text('#undef CONFIG_VIRTFS\n')
        self.build()
        self.assertEqual(len(self.calls()), 1)
        flags = self.calls()[0].split()
        self.assertIn('--enable-virtfs', flags)
        self.assertIn('--enable-attr', flags)
        self.assertNotIn('--disable-attr', flags)
        self.build()
        self.assertEqual(len(self.calls()), 1)
        self.build('--enable-debug-info')
        self.assertEqual(len(self.calls()), 2)
        self.build('--enable-debug-info')
        self.assertEqual(len(self.calls()), 2)
        # A stale/manually replaced host config must not bypass the feature check.
        (build / 'config-host.h').write_text('#undef CONFIG_VIRTFS\n')
        self.build('--enable-debug-info')
        self.assertEqual(len(self.calls()), 3)

    def test_failed_or_disabled_configuration_does_not_update_fingerprint(self):
        self.build()
        stamp = self.root / 'ext/qemu/build/.erbium-configure.sha256'
        original = stamp.read_text()
        self.build('--fake-configure-failure', success=False)
        self.assertEqual(stamp.read_text(), original)
        result = self.build('--disable-virtfs', success=False)
        self.assertIn('QEMU VirtFS support is required', result.stderr)
        self.assertEqual(stamp.read_text(), original)
        self.build()
        self.assertEqual(len(self.calls()), 4)


if __name__ == '__main__':
    unittest.main()
