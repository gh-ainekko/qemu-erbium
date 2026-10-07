#!/usr/bin/python3
"""Local-only control client: status, reset, disconnect, reconnect."""
import argparse
import socket

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["status", "reset", "disconnect", "reconnect"])
parser.add_argument("--socket", default="/run/smallvm-ide/control.sock")
args = parser.parse_args()
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect(args.socket)
    client.sendall((args.command + "\n").encode())
    client.shutdown(socket.SHUT_WR)
    data = bytearray()
    while part := client.recv(65536):
        data.extend(part)
    print(data.decode(), end="")
