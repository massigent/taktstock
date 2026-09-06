#!/usr/bin/env python3
"""
Readiness Gate for Taktstock Container Service
----------------------------------------------
Queries the host sidecar via Unix socket with authenticated 'ready' action,
ensuring that socket and token are active before starting the container.
"""

import os
import sys
import time
import json
import socket
import argparse
from pathlib import Path


def check_ready(socket_path: Path, token_file: Path) -> bool:
    if not token_file.exists() or not os.access(str(token_file), os.R_OK):
        return False
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except Exception:
        return False
    if len(token) < 32:
        return False
    if not socket_path.exists() or not socket_path.is_socket():
        return False

    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        req = json.dumps({"auth_token": token, "action": "ready"}) + "\n"
        sock.sendall(req.encode("utf-8"))
        data = sock.recv(4096).decode("utf-8", errors="ignore").strip()
        resp = json.loads(data)
        return resp.get("status") == "SUCCESS" and resp.get("ready") is True
    except Exception:
        return False
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Wait for sidecar readiness.")
    parser.add_argument("--socket", type=Path, required=True, help="Socket path")
    parser.add_argument("--token-file", type=Path, required=True, help="Token file path")
    parser.add_argument("--timeout", type=int, default=15, help="Timeout in seconds")
    args = parser.parse_args()

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if check_ready(args.socket, args.token_file):
            sys.exit(0)
        time.sleep(0.2)

    sys.stderr.write(f"ERROR: Sidecar not ready within timeout of {args.timeout}s (Sidecar non pronto entro il timeout).\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
