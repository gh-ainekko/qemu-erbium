"""Fast, offline checks; no downloads, apt operations, or builds."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.root.mkdir()
        shutil.copy(ROOT / "bootstrap.sh", self.root)
        for directory in ("scripts", "linux", "qemu-patches", "sysemu-patches"):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns("busybox"))

    def run_check(self, mode="checkout", env=None):
        return subprocess.run(["/bin/bash", str(self.root / "scripts/preflight.sh"), mode],
                              cwd="/", env=env, text=True, capture_output=True)

    def test_checkout_from_unrelated_directory(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_inputs_reported_together(self):
        (self.root / "linux/tools/erbctl.c").unlink()
        shutil.rmtree(self.root / "linux/patches")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("linux/tools/erbctl.c", result.stderr)
        self.assertIn("No patch files", result.stderr)
        self.assertIn("Incomplete source checkout", result.stderr)

    def test_missing_tools(self):
        tools = Path(self.temp.name) / "bin"
        tools.mkdir()
        (tools / "dirname").symlink_to(shutil.which("dirname"))
        result = self.run_check("deps", dict(os.environ, PATH=str(tools)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing command: git", result.stderr)
        self.assertIn("Missing command: aarch64-linux-gnu-gcc", result.stderr)
        self.assertIn("no build was started", result.stderr)

    def test_bootstrap_stops_before_apt(self):
        (self.root / "linux/rootfs/init").unlink()
        result = subprocess.run(["/bin/bash", str(self.root / "bootstrap.sh")],
                                cwd="/", text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete source checkout", result.stderr)
        self.assertFalse((self.root / "ext").exists())


if __name__ == "__main__":
    unittest.main()
