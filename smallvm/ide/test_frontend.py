#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Origin policy checks; proxy authentication itself is provided by exe.dev."""
import unittest
from frontend import SameOrigin
from websockify.auth_plugins import AuthenticationError


class Origins(unittest.TestCase):
    def check(self, **headers):
        SameOrigin().authenticate(headers, "127.0.0.1", 5900)

    def test_local_tools(self):
        self.check(Host="localhost:8000")

    def test_local_browser(self):
        self.check(Host="localhost:8000", Origin="http://localhost:8000")

    def test_private_proxy_browser(self):
        self.check(**{
            "Host": "127.0.0.1:8000",
            "Origin": "https://erbium-qemu.exe.xyz",
            "X-Forwarded-Host": "erbium-qemu.exe.xyz",
            "X-Forwarded-Proto": "https",
        })

    def test_untrusted_and_invalid_origins(self):
        for origin in ("https://evil.example", "null", "https://localhost:8000",
                       "http://[", "http://", "http://localhost:8000/extra",
                       "http://localhost:8000?token=anything"):
            with self.subTest(origin=origin), self.assertRaises(AuthenticationError):
                self.check(Host="localhost:8000", Origin=origin)

    def test_no_host(self):
        with self.assertRaises(AuthenticationError):
            self.check(Origin="http://")


if __name__ == "__main__":
    unittest.main()
