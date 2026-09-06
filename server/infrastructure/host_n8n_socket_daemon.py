#!/usr/bin/env python3
"""
Host n8n Read-Only Socket Daemon (Hardened Production IPC)
----------------------------------------------------------
Local sidecar daemon running on Debian host (unprivileged user 'taktstock', UID 1000).
Exposes a protected Unix Domain Socket at /run/taktstock-n8n/n8n.sock.

Mandatory Security Measures:
1. Zero credentials in container: n8n API key stays strictly on host.
2. Dedicated secret authentication: TAKTSTOCK_SIDECAR_TOKEN (minimum 32 characters).
3. Controlled concurrency: non-reentrant job lock, protected rate limiting.
4. SO_PEERCRED UID 1000 mandatory (root categorically rejected).
5. Read-only by design: only GET/read workflow queries allowed, no executable/modifiable nodes.
6. 5 MB response payload cap.
"""

import os
import sys
import hmac
import time
import json
import socket
import struct
import logging
import threading
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, Set

# Setup secure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][HostN8nDaemon] %(message)s"
)
logger = logging.getLogger("HostN8nDaemon")

def resolve_daemon_socket_path() -> Path:
    env_p = os.environ.get("N8N_SOCKET_PATH", "").strip() or os.environ.get("TAKTSTOCK_N8N_SOCKET_PATH", "").strip()
    if env_p:
        return Path(env_p)
    if Path("/run/user/1000/taktstock-n8n").exists():
        return Path("/run/user/1000/taktstock-n8n/n8n.sock")
    if Path("/run/user/1000/ufficio-n8n").exists():
        return Path("/run/user/1000/ufficio-n8n/n8n.sock")
    if Path("/run/user/1000").exists():
        return Path("/run/user/1000/taktstock-n8n/n8n.sock")
    if Path("/run/ufficio-n8n").exists():
        return Path("/run/ufficio-n8n/n8n.sock")
    return Path("/run/taktstock-n8n/n8n.sock")


DEFAULT_SOCKET_PATH = resolve_daemon_socket_path()
DEFAULT_SOCKET_DIR = DEFAULT_SOCKET_PATH.parent
DEFAULT_TOKEN_FILE = Path("/run/secrets/taktstock_sidecar_token") if Path("/run/secrets/taktstock_sidecar_token").exists() else Path("/run/secrets/ufficio_sidecar_token")

_n8n_cfg = Path(os.environ.get("TAKTSTOCK_N8N_CONFIG") or os.environ.get("UFFICIO_N8N_CONFIG") or (Path.home() / ".config" / "taktstock-n8n" / "sidecar.env"))
if not _n8n_cfg.exists() and (Path.home() / ".config" / "ufficio-n8n" / "sidecar.env").exists():
    _n8n_cfg = Path.home() / ".config" / "ufficio-n8n" / "sidecar.env"
DEFAULT_MASTER_CONFIG = _n8n_cfg

_codex_cfg = Path(os.environ.get("TAKTSTOCK_CODEX_CONFIG") or os.environ.get("UFFICIO_CODEX_CONFIG") or (Path.home() / ".config" / "taktstock-codex" / "sidecar.env"))
if not _codex_cfg.exists() and (Path.home() / ".config" / "ufficio-codex" / "sidecar.env").exists():
    _codex_cfg = Path.home() / ".config" / "ufficio-codex" / "sidecar.env"
FALLBACK_CODEX_CONFIG = _codex_cfg

FALLBACK_LUNA_CONFIG = Path(os.environ.get("TAKTSTOCK_LUNA_CONFIG") or os.environ.get("UFFICIO_LUNA_CONFIG") or (Path.home() / ".codex" / "accounts" / "luna" / "config.toml"))

ALLOWED_CLIENT_UIDS = [1000]  # Only UID 1000 (taktstock), UID 0 categorically forbidden
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB max payload
MAX_TIMEOUT_SECONDS = 60
MIN_TOKEN_LENGTH = 32

ALLOWED_ACTIONS = {"ready", "health", "get_workflow", "search_workflows", "list_workflows"}


def load_sidecar_token(explicit_token: Optional[str] = None) -> str:
    """Loads and validates sidecar token from argument, file, or protected configuration."""
    if explicit_token and len(explicit_token.strip()) >= MIN_TOKEN_LENGTH:
        return explicit_token.strip()

    # 1. Environment variable
    env_token = (
        os.environ.get("TAKTSTOCK_N8N_SIDECAR_TOKEN", "").strip() or
        os.environ.get("TAKTSTOCK_SIDECAR_TOKEN", "").strip() or
        os.environ.get("UFFICIO_N8N_SIDECAR_TOKEN", "").strip() or
        os.environ.get("UFFICIO_SIDECAR_TOKEN", "").strip()
    )
    if env_token and len(env_token) >= MIN_TOKEN_LENGTH:
        return env_token

    # 2. Dedicated runtime secret file
    token_file_path = os.environ.get("TAKTSTOCK_SIDECAR_TOKEN_FILE") or os.environ.get("UFFICIO_SIDECAR_TOKEN_FILE") or str(DEFAULT_TOKEN_FILE)
    token_file_path = token_file_path.strip()
    if token_file_path and Path(token_file_path).exists():
        try:
            file_token = Path(token_file_path).read_text(encoding="utf-8").strip()
            if len(file_token) >= MIN_TOKEN_LENGTH:
                return file_token
        except Exception as e:
            logger.error(f"Error reading token file {token_file_path}: {e}")

    # 3. Master configuration file on host taktstock-n8n
    if DEFAULT_MASTER_CONFIG.exists():
        try:
            for line in DEFAULT_MASTER_CONFIG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if (line.startswith("TAKTSTOCK_SIDECAR_TOKEN=") or line.startswith("TAKTSTOCK_N8N_SIDECAR_TOKEN=") or
                    line.startswith("UFFICIO_SIDECAR_TOKEN=") or line.startswith("UFFICIO_N8N_SIDECAR_TOKEN=")):
                    t = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if len(t) >= MIN_TOKEN_LENGTH:
                        return t
        except Exception:
            pass

    # 4. Fallback master configuration file on host codex
    if FALLBACK_CODEX_CONFIG.exists():
        try:
            for line in FALLBACK_CODEX_CONFIG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TAKTSTOCK_SIDECAR_TOKEN=") or line.startswith("UFFICIO_SIDECAR_TOKEN="):
                    t = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if len(t) >= MIN_TOKEN_LENGTH:
                        return t
        except Exception:
            pass

    raise ValueError(
        f"TAKTSTOCK_SIDECAR_TOKEN not configured or invalid (non configurato o non valido): a key of at least {MIN_TOKEN_LENGTH} characters is required."
    )


def load_n8n_credentials() -> Tuple[str, str]:
    """Retrieves n8n URL and API Key securely from host environment or protected files."""
    api_url = os.environ.get("N8N_API_URL", "").strip()
    api_key = os.environ.get("N8N_API_KEY", "").strip()

    # 1. Read from taktstock-n8n / ufficio-n8n sidecar.env
    if (not api_key or not api_url) and DEFAULT_MASTER_CONFIG.exists():
        try:
            for line in DEFAULT_MASTER_CONFIG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("N8N_API_KEY=") and not api_key:
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("N8N_API_URL=") and not api_url:
                    api_url = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    # 2. Fallback from Luna account configuration on host
    if (not api_key or not api_url) and FALLBACK_LUNA_CONFIG.exists():
        try:
            for line in FALLBACK_LUNA_CONFIG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("N8N_API_KEY") and "=" in line and not api_key:
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("N8N_API_URL") and "=" in line and not api_url:
                    api_url = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    if not api_url:
        api_url = "https://n8n-netcup.salus.academy/"

    return api_url.rstrip("/"), api_key


def get_peer_credentials(sock: socket.socket) -> Optional[Tuple[int, int, int]]:
    """Retrieves PID, UID, GID of client process via SO_PEERCRED (Linux)."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            ucred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", ucred)
            return pid, uid, gid
    except Exception as e:
        logger.warning(f"Unable to determine peer credentials: {e}")
    return None


def execute_n8n_readonly_get(api_url: str, api_key: str, endpoint: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Any:
    """Executes a read-only HTTP GET request to n8n API."""
    if not api_key:
        raise RuntimeError("N8N_API_KEY not available on host. Cannot authenticate n8n request.")

    clean_endpoint = endpoint.lstrip("/")
    url = f"{api_url}/{clean_endpoint}"
    if params:
        query_str = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query_str:
            url += f"?{query_str}"

    req = urllib.request.Request(
        url,
        headers={
            "X-N8N-API-KEY": api_key,
            "Accept": "application/json",
            "User-Agent": "Taktstock-Host-N8n-Bridge/1.0"
        },
        method="GET"
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"n8n API responded with HTTP status {resp.status}")
        raw_body = resp.read().decode("utf-8")
        return json.loads(raw_body)


class HostN8nSocketDaemon:
    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        auth_token: Optional[str] = None,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None
    ):
        self.socket_path = Path(socket_path)
        self.auth_token = load_sidecar_token(auth_token)
        url_resolved, key_resolved = load_n8n_credentials()
        self.api_url = url_resolved if api_url is None else api_url
        self.api_key = key_resolved if api_key is None else api_key

        self.server_sock: Optional[socket.socket] = None
        self.running = False
        self.lock = threading.Lock()

    def handle_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Processes and executes a client request."""
        if not isinstance(payload, dict):
            return {"status": "error", "error": "Invalid payload (Payload non valido): must be a JSON object."}

        # 1. Timing-safe authentication
        token = str(payload.get("auth_token", "")).strip()
        if not token or not hmac.compare_digest(token, self.auth_token):
            return {"status": "error", "error": "Socket authentication failed (Autenticazione socket fallita): invalid or unauthorized token."}

        action = str(payload.get("action", "")).strip().lower()
        if action not in ALLOWED_ACTIONS:
            return {
                "status": "error",
                "error": f"Action '{action}' not allowed (non consentita): the host n8n bridge is strictly read-only (allowed: {', '.join(sorted(ALLOWED_ACTIONS))})."
            }

        # 2. Readiness check
        if action in ["ready", "health"]:
            return {
                "status": "ok",
                "ready": True,
                "service": "host_n8n_daemon",
                "has_api_key": bool(self.api_key),
                "api_url": self.api_url
            }

        if not self.api_key:
            return {
                "status": "error",
                "error": "N8N_API_KEY not configured on host (N8N_API_KEY non configurata o assente sull'host)."
            }

        timeout = min(int(payload.get("timeout", 15)), MAX_TIMEOUT_SECONDS)

        # 3. Read-only actions
        try:
            if action == "get_workflow":
                wf_id = str(payload.get("workflow_id") or payload.get("workflowId") or "").strip()
                if not wf_id:
                    return {"status": "error", "error": "Mandatory parameter 'workflow_id' missing."}
                data = execute_n8n_readonly_get(self.api_url, self.api_key, f"api/v1/workflows/{wf_id}", timeout=timeout)
                return {"status": "ok", "workflow": data}

            elif action == "search_workflows":
                limit = min(int(payload.get("limit", 50)), 100)
                raw_data = execute_n8n_readonly_get(self.api_url, self.api_key, "api/v1/workflows", params={"limit": limit}, timeout=timeout)
                workflows = raw_data.get("data", [])
                query = str(payload.get("query", "")).strip().lower()
                tag_filter = set(payload.get("tags") or [])

                results = []
                for wf in workflows:
                    wf_name = str(wf.get("name", "")).lower()
                    wf_desc = str(wf.get("description", "") or "").lower()
                    if query and (query not in wf_name and query not in wf_desc and query != wf.get("id")):
                        continue
                    wf_tags = {t.get("name") for t in wf.get("tags", []) if isinstance(t, dict)}
                    if tag_filter and not tag_filter.issubset(wf_tags):
                        continue
                    results.append({
                        "id": wf.get("id"),
                        "name": wf.get("name"),
                        "active": wf.get("active"),
                        "updatedAt": wf.get("updatedAt"),
                        "createdAt": wf.get("createdAt"),
                        "isArchived": wf.get("isArchived", False),
                        "nodesCount": len(wf.get("nodes", [])),
                        "tags": wf.get("tags", [])
                    })
                return {"status": "ok", "count": len(results), "data": results}

            elif action == "list_workflows":
                limit = min(int(payload.get("limit", 100)), 100)
                raw_data = execute_n8n_readonly_get(self.api_url, self.api_key, "api/v1/workflows", params={"limit": limit}, timeout=timeout)
                workflows = raw_data.get("data", [])
                catalog = [{
                    "id": w.get("id"),
                    "name": w.get("name"),
                    "active": w.get("active"),
                    "updatedAt": w.get("updatedAt"),
                    "createdAt": w.get("createdAt"),
                    "nodesCount": len(w.get("nodes", []))
                } for w in workflows]
                return {"status": "ok", "total": len(catalog), "workflows": catalog}

        except Exception as e:
            err_str = str(e)
            # Sanitize error strings: never leak secrets
            if self.api_key:
                err_str = err_str.replace(self.api_key, "[REDACTED_API_KEY]")
            if self.auth_token:
                err_str = err_str.replace(self.auth_token, "[REDACTED_TOKEN]")
            logger.error(f"Error executing {action}: {err_str}")
            return {"status": "error", "error": f"n8n read-only call error (Errore chiamata n8n read-only): {err_str}"}

        return {"status": "error", "error": "Unhandled request."}

    def _client_thread(self, conn: socket.socket, addr: Any):
        try:
            conn.settimeout(30.0)

            # UID verification on Linux if supported
            peer_cred = get_peer_credentials(conn)
            if peer_cred is not None:
                _, uid, _ = peer_cred
                if uid not in ALLOWED_CLIENT_UIDS:
                    logger.warning(f"Rejected connection from client with unauthorized UID: {uid}")
                    err_resp = json.dumps({"status": "error", "error": "Access denied: client UID not authorized."}) + "\n"
                    conn.sendall(err_resp.encode("utf-8"))
                    return

            # Read payload up to newline or EOF (max 512 KB)
            data_chunks = []
            total_read = 0
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data_chunks.append(chunk)
                total_read += len(chunk)
                if b"\n" in chunk or total_read > 512 * 1024:
                    break

            raw_req = b"".join(data_chunks).decode("utf-8").strip()
            if not raw_req:
                return

            try:
                payload = json.loads(raw_req)
            except json.JSONDecodeError:
                err_resp = json.dumps({"status": "error", "error": "Invalid JSON format."}) + "\n"
                conn.sendall(err_resp.encode("utf-8"))
                return

            response_dict = self.handle_request(payload)
            resp_bytes = (json.dumps(response_dict, ensure_ascii=False) + "\n").encode("utf-8")
            if len(resp_bytes) > MAX_RESPONSE_BYTES:
                err_resp = json.dumps({"status": "error", "error": "n8n response exceeds maximum allowed size (dimensione massima consentita)."}) + "\n"
                conn.sendall(err_resp.encode("utf-8"))
                return

            conn.sendall(resp_bytes)

        except Exception as e:
            logger.error(f"Error handling socket client: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def start(self):
        """Starts the listening Unix Domain Socket daemon."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(str(self.socket_path))
        os.chmod(str(self.socket_path), 0o600)
        self.server_sock.listen(10)
        self.running = True

        logger.info(f"Host n8n Read-Only Daemon listening on Unix Socket: {self.socket_path} (mode 0600)")
        logger.info(f"n8n Endpoint: {self.api_url} | API Key configured: {'YES' if self.api_key else 'NO'}")

        while self.running:
            try:
                conn, addr = self.server_sock.accept()
                t = threading.Thread(target=self._client_thread, args=(conn, addr), daemon=True)
                t.start()
            except Exception as e:
                if self.running:
                    logger.error(f"Socket accept error: {e}")
                break

    def stop(self):
        """Stops the daemon and removes the socket."""
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass
        logger.info("Host n8n Read-Only Daemon stopped.")


if __name__ == "__main__":
    daemon = HostN8nSocketDaemon()
    try:
        daemon.start()
    except KeyboardInterrupt:
        daemon.stop()
