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

    def launch_guest(self, qemu=None, image=None):
        env = dict(os.environ, QEMU=str(qemu or self.root / "missing-qemu"),
                   IMAGE=str(image or self.root / "missing-Image"))
        mram = self.root / "test-mram.img"
        result = subprocess.run(["bash", str(self.root / "scripts/run-linux.sh"),
                                 "--test", "--mram", str(mram)],
                                cwd="/", env=env, text=True, capture_output=True)
        return result, mram

    def test_linux_only_checkout_reports_missing_qemu(self):
        image = self.root / "Image"
        image.write_bytes(b"guest kernel")
        result, mram = self.launch_guest(image=image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or non-executable Erbium QEMU", result.stderr)
        self.assertIn("J=2 ./bootstrap.sh", result.stderr)
        self.assertFalse(mram.exists())

    def test_missing_guest_image_stops_before_launch(self):
        result, mram = self.launch_guest(qemu="/bin/true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or empty Linux guest Image", result.stderr)
        self.assertFalse(mram.exists())

    def test_binary_distribution_has_no_source_build_advice(self):
        (self.root / "bootstrap.sh").unlink()
        result, _ = self.launch_guest()
        self.assertIn("Re-extract the complete binary distribution", result.stderr)
        self.assertNotIn("run J=2 ./bootstrap.sh", result.stderr)

    def test_valid_runtime_artifacts_launch(self):
        image = self.root / "Image"
        image.write_bytes(b"guest kernel")
        result, mram = self.launch_guest(qemu="/bin/true", image=image)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mram.stat().st_size, 16 * 1024 * 1024)

    def test_bootstrap_stops_before_apt(self):
        (self.root / "linux/rootfs/init").unlink()
        result = subprocess.run(["/bin/bash", str(self.root / "bootstrap.sh")],
                                cwd="/", text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete source checkout", result.stderr)
        self.assertFalse((self.root / "ext").exists())


if __name__ == "__main__":
    unittest.main()
