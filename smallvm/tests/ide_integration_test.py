#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fast tests of IDE harness helpers; NOT a substitute for the real GP run."""
import tempfile
import unittest
from pathlib import Path

from ide_integration import assert_clean_gp_log, frames, generate_project


class IDEHarnessTests(unittest.TestCase):
    def test_fragmented_frames(self):
        data = bytes([250, 26, 0, 251, 21, 2, 5, 0, 1, 42, 0, 0, 0])
        for size in range(len(data) + 1):
            parsed, tail = frames(data[:size])
            self.assertEqual(len(parsed), int(size >= 3) + int(size >= 13))
            consumed = 13 if size == 13 else (3 if size >= 3 else 0)
            self.assertEqual(tail, size - consumed)
        self.assertEqual(frames(data)[0][1], (21, 2, bytes([1, 42, 0, 0, 0])))

    def test_unframed_uart_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "unframed UART"):
            frames(b"junk")

    def test_gp_errors_reject_later_success(self):
        assert_clean_gp_log("SDL2 headers: 2.0.12\nIDE_TEST_PASS test\nGoodbye!\n")
        for error in (
            "Undefined function: browserElectronOS",
            "/test/driver.gp:56 Operator must be a string",
            "Stopped at (<parse>:20)",
            "-------------",
            "Serial error, length: 1024",
            "File not found: img/rounded-corner.svg",
            "Welcome to GP!",
            "gp> Goodbye!",
        ):
            with self.subTest(error=error), self.assertRaisesRegex(AssertionError, "error in log"):
                assert_clean_gp_log(error + "\nIDE_TEST_PASS test\n")

    def test_generator_deterministic_and_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.ubp"
            second = Path(directory) / "second.ubp"
            generate_project(first)
            generate_project(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            text = first.read_text()
            self.assertEqual(text.count("UART-STRESS-"), 24)
            self.assertIn("waitMillis 5", text)
            self.assertEqual(text.count("script "), 26)
            edited = first.with_name("edited.ubp").read_text()
            self.assertEqual(edited, text.replace("return ((n * 2) + 1)",
                                                 "return ((n * 2) + 2)"))


if __name__ == "__main__":
    unittest.main()
