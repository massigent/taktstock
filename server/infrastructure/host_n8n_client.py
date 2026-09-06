#!/usr/bin/env python3
"""
Host n8n Socket Client (Hardened Production IPC)
------------------------------------------------
Client per la comunicazione IPC su Unix Domain Socket con il demone
sidecar host n8n (taktstock-n8n.service).

Misure di Sicurezza Tassative:
1. Autenticazione con segreto sidecar letto da file protetto o argomento (File-Only).
2. Mai loggare o propagare token o credenziali.
3. Accesso rigorosamente READ-ONLY (list, search, get).
4. Nessun segreto API n8n viene mai esposto o ricevuto: il client riceve solo il JSON del workflow.
"""

import os
import json
import socket
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger("HostN8nClient")

def resolve_default_socket_path() -> Path:
    env_p = os.environ.get("TAKTSTOCK_N8N_SOCKET_PATH", "").strip() or os.environ.get("N8N_SOCKET_PATH", "").strip()
    if env_p:
        return Path(env_p)
    if Path("/run/user/1000/taktstock-n8n/n8n.sock").exists():
        return Path("/run/user/1000/taktstock-n8n/n8n.sock")
    if Path("/run/taktstock-n8n/n8n.sock").exists():
        return Path("/run/taktstock-n8n/n8n.sock")
    if Path("/run/user/1000/ufficio-n8n/n8n.sock").exists():
        return Path("/run/user/1000/ufficio-n8n/n8n.sock")
    if Path("/run/ufficio-n8n/n8n.sock").exists():
        return Path("/run/ufficio-n8n/n8n.sock")
    return Path("/run/user/1000/taktstock-n8n/n8n.sock" if Path("/run/user/1000").exists() else "/run/taktstock-n8n/n8n.sock")


DEFAULT_SOCKET_PATH = resolve_default_socket_path()
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB max payload
MIN_TOKEN_LENGTH = 32


class N8nSidecarError(RuntimeError):
    """Eccezione sollevata quando il sidecar host n8n restituisce un errore."""
    pass


def load_client_sidecar_token(explicit_token: Optional[str] = None, token_file: Optional[Path] = None) -> str:
    """Carica e valida il token del sidecar (File-Only, nessun fallback insicuro)."""
    if explicit_token and len(explicit_token.strip()) >= MIN_TOKEN_LENGTH:
        return explicit_token.strip()

    # 1. File indicato o default
    env_file = os.environ.get("TAKTSTOCK_SIDECAR_TOKEN_FILE") or os.environ.get("UFFICIO_SIDECAR_TOKEN_FILE", "")
    candidates = [
        token_file,
        Path(env_file) if env_file else None,
        Path("/run/user/1000/taktstock-n8n/token"),
        Path("/run/taktstock-n8n/token"),
        Path("/run/user/1000/taktstock-codex/token"),
        Path("/run/taktstock-codex/token"),
        Path("/run/secrets/taktstock_sidecar_token"),
        Path.home() / ".config" / "taktstock-n8n" / "sidecar.env",
        Path.home() / ".config" / "taktstock-codex" / "sidecar.env",
        Path("/run/user/1000/ufficio-n8n/token"),
        Path("/run/ufficio-n8n/token"),
        Path("/run/user/1000/ufficio-codex/token"),
        Path("/run/ufficio-codex/token"),
        Path("/run/secrets/ufficio_sidecar_token"),
        Path.home() / ".config" / "ufficio-n8n" / "sidecar.env",
        Path.home() / ".config" / "ufficio-codex" / "sidecar.env"
    ]

    for cand in candidates:
        if cand and cand.exists() and cand.is_file():
            try:
                content = cand.read_text(encoding="utf-8").strip()
                if cand.suffix == ".env":
                    for line in content.splitlines():
                        if (line.startswith("TAKTSTOCK_SIDECAR_TOKEN=") or line.startswith("TAKTSTOCK_N8N_SIDECAR_TOKEN=") or
                            line.startswith("UFFICIO_SIDECAR_TOKEN=") or line.startswith("UFFICIO_N8N_SIDECAR_TOKEN=")):
                            t = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if len(t) >= MIN_TOKEN_LENGTH:
                                return t
                else:
                    if len(content) >= MIN_TOKEN_LENGTH:
                        return content
            except Exception:
                pass

    raise N8nSidecarError(
        "Autenticazione sidecar n8n fallita: file segreto TAKTSTOCK_SIDECAR_TOKEN_FILE non trovato o non valido (minimo 32 caratteri)."
    )


class HostN8nClient:
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
        """Risolve il token valido o solleva N8nSidecarError."""
        return load_client_sidecar_token(self.auth_token, self.token_file)

    def check_ready(self, timeout: float = 2.0) -> bool:
        """Verifica se il demone sidecar host n8n è attivo e raggiungibile via socket Unix."""
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

            resp_raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp_raw += chunk
                if b"\n" in chunk:
                    break

            data = json.loads(resp_raw.decode("utf-8").strip())
            return bool(data.get("status") == "ok" and data.get("ready") is True)
        except Exception:
            return False
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _send_ipc_request(self, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        """Invia una richiesta IPC su Unix Domain Socket."""
        token = self._get_valid_token()
        if not self.socket_path.exists():
            raise N8nSidecarError(
                f"Socket n8n host non trovato in {self.socket_path}. Verificare che taktstock-n8n.service sia attivo sull'host."
            )

        full_payload = dict(payload)
        full_payload["auth_token"] = token
        full_payload["timeout"] = timeout or self.timeout

        sock = None
        req_bytes = (json.dumps(full_payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(float(timeout or self.timeout) + 5.0)
            sock.connect(str(self.socket_path))
            sock.sendall(req_bytes)

            chunks = []
            total_read = 0
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                total_read += len(chunk)
                if total_read > MAX_RESPONSE_BYTES:
                    raise N8nSidecarError(f"Risposta socket n8n eccede il limite di {MAX_RESPONSE_BYTES} bytes.")
                if b"\n" in chunk:
                    break

            resp_raw = b"".join(chunks).decode("utf-8").strip()
            if not resp_raw:
                raise N8nSidecarError("Risposta vuota ricevuta dal sidecar n8n host.")

            resp_data = json.loads(resp_raw)
            if resp_data.get("status") != "ok":
                err_msg = resp_data.get("error", "Errore sconosciuto dal sidecar n8n")
                raise N8nSidecarError(err_msg)

            return resp_data

        except socket.timeout:
            raise N8nSidecarError(f"Timeout IPC ({self.timeout}s) durante la comunicazione con il sidecar n8n host.")
        except N8nSidecarError:
            raise
        except Exception as e:
            raise N8nSidecarError(f"Errore IPC sidecar n8n: {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def get_workflow_details(self, workflow_id: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Recupera la definizione in sola lettura di un workflow."""
        wf_id = str(workflow_id).strip()
        if not wf_id:
            raise ValueError("ID workflow obbligatorio.")
        res = self._send_ipc_request({"action": "get_workflow", "workflow_id": wf_id}, timeout=timeout)
        return res.get("workflow", {})

    def search_workflows(self, query: str = "", tags: Optional[List[str]] = None, limit: int = 50, timeout: Optional[int] = None) -> List[Dict[str, Any]]:
        """Cerca workflow per query o tag."""
        res = self._send_ipc_request({
            "action": "search_workflows",
            "query": query,
            "tags": tags or [],
            "limit": limit
        }, timeout=timeout)
        return res.get("data", [])

    def list_workflows(self, limit: int = 100, timeout: Optional[int] = None) -> List[Dict[str, Any]]:
        """Elenca i workflow live."""
        res = self._send_ipc_request({"action": "list_workflows", "limit": limit}, timeout=timeout)
        return res.get("workflows", [])
