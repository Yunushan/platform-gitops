"""Test-only byte relay for one Git SSH connection in the isolated VIP drill."""

import os
from pathlib import Path
import select
import socket
import sys
import time


def until_exists(path, deadline):
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("SSH fixture barrier expired")
        time.sleep(0.025)


def run_hook(root, phase):
    # Git invokes pre-push after receiving the remote ref advertisement.
    sys.stdin.buffer.read(65536)
    (root / "hook-ready").touch()
    until_exists(root / ("release" if phase == "request" else "reply-held"), time.monotonic() + 20)


def relay(root, phase):
    deadline = time.monotonic() + 25
    held = False
    with socket.create_connection(("192.0.2.50", 29418), timeout=4) as upstream:
        while time.monotonic() < deadline:
            if (root / "release").exists():
                return 1
            if phase == "response" and not held and (root / "hook-ready").exists():
                held = True
                (root / "reply-held").touch()
            ready, _, _ = select.select([0, upstream], [], [], 0.025)
            if 0 in ready:
                payload = os.read(0, 65536)
                if not payload:
                    return 1
                upstream.sendall(payload)
            if upstream in ready:
                try:
                    payload = upstream.recv(65536)
                except ConnectionResetError:
                    payload = b""
                if not payload:
                    (root / "router-disconnected").touch()
                    return 1
                if not held:
                    sys.stdout.buffer.write(payload)
                    sys.stdout.buffer.flush()
    raise TimeoutError("SSH fixture relay expired")


if __name__ == "__main__":
    operation, directory, phase = sys.argv[1:]
    root = Path(directory)
    try:
        if operation == "hook":
            run_hook(root, phase)
        elif operation == "relay":
            sys.exit(relay(root, phase))
        else:
            sys.exit(2)
    except OSError:
        sys.exit(1)
