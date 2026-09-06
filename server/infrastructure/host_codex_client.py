#!/usr/bin/env python3
"""
Host Codex Socket Client (Hardened Production IPC)
--------------------------------------------------
Client for Unix Domain Socket IPC communication with the host
sidecar daemon (taktstock-codex.service).

Mandatory Security Measures:
1. Reads TAKTSTOCK_SIDECAR_TOKEN_FILE secret from protected file (read-only):
   If file is missing, empty, or token < 32 characters, fail-closed immediately
   without opening any socket connection.
2. Never log token or file contents.
3. Maximum socket response limit (MAX_RESPONSE_BYTES = 512 KB)
   to prevent memory exhaustion in client.
4. No fallback to local CLI commands or container accounts on error.
"""

import os
import json
import socket
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("HostCodexClient")

DEFAULT_SOCKET_PATH = Path(
    os.environ.get("CODEX_SOCKET_PATH") or
    os.environ.get("TAKTSTOCK_CODEX_SOCKET_PATH") or
    ("/run/taktstock-codex/codex.sock" if Path("/run/taktstock-codex/codex.sock").exists() else
     ("/run/ufficio-codex/codex.sock" if Path("/run/ufficio-codex/codex.sock").exists() else "/run/taktstock-codex/codex.sock"))
)
DEFAULT_TOKEN_FILE_PATH = Path(
    os.environ.get("TAKTSTOCK_SIDECAR_TOKEN_FILE") or
    os.environ.get("UFFICIO_SIDECAR_TOKEN_FILE") or
    ("/run/secrets/taktstock_sidecar_token" if Path("/run/secrets/taktstock_sidecar_token").exists() else "/run/secrets/ufficio_sidecar_token")
)
DEFAULT_TIMEOUT_SECONDS = 900     # 15 minutes
MAX_RESPONSE_BYTES = 512 * 1024   # 512 KB max payload in reception
MIN_TOKEN_LENGTH = 32             # Minimum token length


class CodexSidecarError(RuntimeError):
    """Exception raised when host sidecar returns an error."""
    pass


class CodexSidecarBusyError(CodexSidecarError):
    """Exception raised when host runner is busy with another job."""
    pass


def load_client_sidecar_token(explicit_token: Optional[str] = None, token_file: Optional[Path] = None) -> str:
    """Loads and validates token from secret file or explicit argument (File-Only, no env fallback)."""
    if explicit_token and len(explicit_token.strip()) >= MIN_TOKEN_LENGTH:
        return explicit_token.strip()

    tf = token_file or Path(os.environ.get("TAKTSTOCK_SIDECAR_TOKEN_FILE") or os.environ.get("UFFICIO_SIDECAR_TOKEN_FILE") or str(DEFAULT_TOKEN_FILE_PATH))
    if tf.exists() and tf.is_file():
        try:
            content = tf.read_text(encoding="utf-8").strip()
            if len(content) >= MIN_TOKEN_LENGTH:
                return content
        except Exception:
            pass

    raise CodexSidecarError(
        "Sidecar authentication failed (Autenticazione sidecar fallita): secret file TAKTSTOCK_SIDECAR_TOKEN_FILE not found, unreadable, or invalid token (minimum 32 characters). "
        "fail-closed configuration: no socket connection attempted."
    )


class HostCodexClient:
    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        auth_token: Optional[str] = None,
        token_file: Optional[Path] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS
    ):
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.token_file = token_file
        self.auth_token = auth_token

    def _get_valid_token(self) -> str:
        """Resolves valid token or raises CodexSidecarError (fail-closed)."""
        return load_client_sidecar_token(self.auth_token, self.token_file)

    def check_ready(self, timeout: float = 2.0) -> bool:
        """
        Checks whether the host Codex sidecar daemon is active and reachable via Unix socket.
        Sends a readiness message {'auth_token': token, 'action': 'ready'}
        and validates response.
        """
        try:
            token = self._get_valid_token()
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

    def send_request(
        self,
        profile: str,
        prompt: str,
        worktree: str,
        sandbox: str = "read-only",
        reasoning_effort: Optional[str] = None
    ) -> str:
        """Sends an execution request to the host sidecar daemon via Unix socket."""
        # 1. Token validation at origin (Fail-Closed before connecting socket)
        token = self._get_valid_token()

        if not self.socket_path.exists():
            raise CodexSidecarError(
                f"Host sidecar socket not found at {self.socket_path}. "
                "Verify that taktstock-codex service is active on the host."
            )

        payload: Dict[str, Any] = {
            "auth_token": token,
            "profile": profile,
            "worktree": str(worktree),
            "prompt": prompt,
            "sandbox": sandbox,
        }
        # Business rule: Luna must not receive reasoning effort override
        if profile in ["sol", "director"]:
            payload["reasoning_effort"] = reasoning_effort or "low"
        elif reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(str(self.socket_path))
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

            raw_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw_data += chunk
                if len(raw_data) > MAX_RESPONSE_BYTES:
                    sock.close()
                    sock = None
                    raise CodexSidecarError(
                        f"Host sidecar response exceeds maximum allowed size (dimensione massima consentita: {MAX_RESPONSE_BYTES} byte)."
                    )
            sock.close()
            sock = None

            if not raw_data:
                raise CodexSidecarError("Empty response received from host sidecar.")

            resp = json.loads(raw_data.decode("utf-8", errors="ignore"))

        except socket.timeout:
            raise CodexSidecarError(f"Timeout ({self.timeout}s) while communicating with host sidecar.")
        except json.JSONDecodeError as e:
            raise CodexSidecarError(f"Invalid response (malformed JSON) from host sidecar: {e}")
        except Exception as e:
            if isinstance(e, CodexSidecarError):
                raise
            raise CodexSidecarError(f"Host sidecar socket connection error ({self.socket_path}): {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        status = resp.get("status", "ERROR")
        if status == "SUCCESS":
            return resp.get("stdout", "")
        elif status == "BUSY":
            err_msg = resp.get("error", "The host runner is currently busy (occupato) with another job. Please retry later.")
            raise CodexSidecarBusyError(err_msg)
        else:
            err_msg = resp.get("error") or resp.get("stderr") or "Unspecified error from host sidecar."
            raise CodexSidecarError(f"Codex execution failed on host: {err_msg}")
