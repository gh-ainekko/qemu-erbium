#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Loopback noVNC frontend behind the private exe.dev HTTPS proxy.

Authentication is the documented private proxy boundary, not spoofable client
headers. Do not make the VM's proxy public or bind this listener externally.
Local tools can access localhost for testing. Cross-origin browser WebSockets
are rejected even when the caller has a proxy login session.
"""
import argparse
from urllib.parse import urlsplit
from websockify import WebSocketProxy
from websockify.auth_plugins import AuthenticationError
from websockify.websocketproxy import ProxyRequestHandler


class SameOrigin:
    def authenticate(self, headers, target_host, target_port):
        origin = headers.get('Origin')
        if not origin:
            return  # native local clients; remote requests still cross the private proxy
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise AuthenticationError(response_code=403, response_msg='Invalid desktop origin')
        host = headers.get('X-Forwarded-Host', headers.get('Host', '')).lower()
        scheme = headers.get('X-Forwarded-Proto', 'http').lower()
        if (not host or not parsed.netloc or parsed.scheme != scheme or parsed.netloc.lower() != host
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise AuthenticationError(response_code=403, response_msg='Cross-origin desktop access denied')


class DesktopHandler(ProxyRequestHandler):
    def end_headers(self):
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        super().end_headers()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web', required=True)
    args = parser.parse_args()
    WebSocketProxy(
        RequestHandlerClass=DesktopHandler,
        listen_host='127.0.0.1', listen_port=8000,
        target_host='127.0.0.1', target_port=5900,
        web=args.web, web_auth=True, auth_plugin=SameOrigin(),
        file_only=True, heartbeat=30,
    ).start_server()
