#!/usr/bin/env python3
"""
Host AGY Client (Container-Side Socket Client)
---------------------------------------------
Python client for secure communication between Docker container and host AGY sidecar.
Key features:
- Connects to dedicated Unix socket (/run/taktstock-agy/agy.sock).
- Loads secret token from read-only file mounted in container (/run/taktstock-agy/token).
- Fail-Closed: if token file is missing, empty, or < 32 characters, blocks the call without contacting socket.
- Recognizes and handles BUSY state (single-task concurrency).
- Performs no fallback to local CLI or container accounts.
"""

import os
import json
import socket
import logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("HostAgyClient")

DEFAULT_SOCKET_PATH = Path(
    os.environ.get("AGY_SOCKET_PATH") or
    os.environ.get("TAKTSTOCK_AGY_SOCKET_PATH") or
    ("/run/taktstock-agy/agy.sock" if Path("/run/taktstock-agy/agy.sock").exists() else
     ("/run/ufficio-agy/agy.sock" if Path("/run/ufficio-agy/agy.sock").exists() else "/run/taktstock-agy/agy.sock"))
)
DEFAULT_TOKEN_FILE_PATH = Path(
    os.environ.get("TAKTSTOCK_AGY_SIDECAR_TOKEN_FILE") or
    os.environ.get("UFFICIO_AGY_SIDECAR_TOKEN_FILE") or
    ("/run/taktstock-agy/token" if Path("/run/taktstock-agy/token").exists() else
     ("/run/ufficio-agy/token" if Path("/run/ufficio-agy/token").exists() else "/run/taktstock-agy/token"))
)
MIN_TOKEN_LENGTH = 32


class AgySidecarError(Exception):
    """Error during communication or execution with host AGY sidecar."""
    pass


class AgySidecarBusyError(AgySidecarError):
    """The host AGY runner is busy with another task."""
    pass


class AgySidecarTimeoutError(AgySidecarError):
    """Timeout exceeded for AGY execution (time budget exceeded)."""
    pass


class HostAgyClient:
    def __init__(
        self,
        socket_path: Optional[Path] = None,
        token_file_path: Optional[Path] = None
    ):
        self.socket_path = socket_path or DEFAULT_SOCKET_PATH
        self.token_file_path = token_file_path or DEFAULT_TOKEN_FILE_PATH

    def _load_token(self) -> str:
        """Loads token from protected secret file (Fail-Closed)."""
        if not self.token_file_path.exists():
            raise AgySidecarError(
                f"AGY sidecar token file not found (non trovato) in '{self.token_file_path}'. "
                "Ensure the secret volume is mounted read-only in the container."
            )
        try:
            token = self.token_file_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            raise AgySidecarError(f"Unable to read AGY sidecar token file '{self.token_file_path}': {e}")

        if len(token) < MIN_TOKEN_LENGTH:
            raise AgySidecarError(
                f"AGY sidecar token invalid or too weak (troppo debole): length {len(token)}, minimum required {MIN_TOKEN_LENGTH}."
            )
        return token

    def check_ready(self, timeout: float = 2.0) -> bool:
        """
        Checks whether the host AGY sidecar is active and reachable via Unix socket.
        Sends a readiness message {'auth_token': token, 'action': 'ready'}
        and validates the response.
        """
        if not self.token_file_path.exists() or not os.access(str(self.token_file_path), os.R_OK):
            return False
        try:
            token = self._load_token()
        except Exception:
            return False

        if not self.socket_path.exists() or not self.socket_path.is_socket():
            return False

        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(self.socket_path))
            req = json.dumps({"auth_token": token, "action": "ready"}, ensure_ascii=False) + "\n"
            sock.sendall(req.encode("utf-8"))
            data = sock.recv(4096).decode("utf-8", errors="ignore").strip()
            if not data:
                return False
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

    def send_request_envelope(
        self,
        prompt: str,
        worktree: str,
        sandbox: str = "workspace-write",
        timeout: int = 900,
        model: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends an AGY execution request to the host sidecar.
        Returns the full structured response envelope (dict with stdout, usage, code, status, duration_sec, etc.)
        or raises AgySidecarError / AgySidecarBusyError.
        """
        token = self._load_token()

        if not self.socket_path.exists():
            raise AgySidecarError(
                f"AGY sidecar socket not available at '{self.socket_path}'. "
                "Verify that the taktstock-agy daemon is active on the host and the directory is mounted."
            )

        payload = {
            "auth_token": token,
            "action": "execute",
            "prompt": prompt,
            "worktree": str(worktree),
            "sandbox": sandbox,
            "timeout": timeout,
            "model": model
        }

        try:
            client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_sock.settimeout(float(timeout + 30))
            client_sock.connect(str(self.socket_path))
        except Exception as e:
            raise AgySidecarError(f"Connection to AGY sidecar socket failed: {e}")

        try:
            msg = json.dumps(payload, ensure_ascii=False) + "\n"
            client_sock.sendall(msg.encode("utf-8"))

            response_chunks = []
            while True:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                response_chunks.append(chunk)

            raw_resp = b"".join(response_chunks).decode("utf-8", errors="replace").strip()
            if not raw_resp:
                raise AgySidecarError("Empty response received from AGY sidecar socket.")

            try:
                resp = json.loads(raw_resp)
            except Exception as e:
                raise AgySidecarError(f"Invalid response from AGY sidecar (malformed JSON): {raw_resp[:200]}")

            status = resp.get("status")
            if status == "BUSY":
                raise AgySidecarBusyError(resp.get("error", "The host AGY runner is currently busy (occupato)."))

            if status != "SUCCESS":
                err_msg = resp.get("stderr") or resp.get("error") or "Unknown error during AGY execution."
                if resp.get("code") == 124 or "timeout" in err_msg.lower():
                    raise AgySidecarTimeoutError(f"AGY execution timeout ({timeout}s): {err_msg}")
                raise AgySidecarError(f"AGY execution failed (code {resp.get('code', 1)}): {err_msg}")

            return resp

        except (AgySidecarTimeoutError, AgySidecarBusyError):
            raise
        except AgySidecarError:
            raise
        except socket.timeout:
            raise AgySidecarTimeoutError(f"Socket communication timeout with AGY sidecar ({timeout + 30}s).")
        except Exception as e:
            raise AgySidecarError(f"IPC error with host AGY sidecar: {e}")
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def send_request(
        self,
        prompt: str,
        worktree: str,
        sandbox: str = "workspace-write",
        timeout: int = 900,
        model: Optional[str] = None
    ) -> str:
        """
        Sends an AGY execution request to the host sidecar.
        Returns the agent's stdout or raises AgySidecarError / AgySidecarBusyError.
        """
        resp = self.send_request_envelope(
            prompt=prompt,
            worktree=worktree,
            sandbox=sandbox,
            timeout=timeout,
            model=model
        )
        return resp.get("stdout", "")
