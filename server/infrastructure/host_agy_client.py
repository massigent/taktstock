#!/usr/bin/env python3
"""
Host AGY Client (Container-Side Socket Client)
---------------------------------------------
Client Python per la comunicazione sicura tra il container Docker e il sidecar AGY host.
Caratteristiche:
- Si connette al socket Unix dedicato (/run/taktstock-agy/agy.sock).
- Carica il token segreto da file read-only montato nel container (/run/taktstock-agy/token).
- Fail-Closed: se il file token non esiste, è vuoto o < 32 caratteri, blocca la chiamata senza contattare il socket.
- Riconosce e gestisce lo stato BUSY (concorrenza a singolo task).
- Non esegue alcun fallback alla CLI locale o ad account container.
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
    """Errore durante la comunicazione o esecuzione con il sidecar host AGY."""
    pass


class AgySidecarBusyError(AgySidecarError):
    """Il runner host AGY è occupato con un altro task."""
    pass


class AgySidecarTimeoutError(AgySidecarError):
    """Timeout superato per l'esecuzione di AGY (time budget superato)."""
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
        """Carica il token da file segreto protetto (Fail-Closed)."""
        if not self.token_file_path.exists():
            raise AgySidecarError(
                f"File token sidecar AGY non trovato in '{self.token_file_path}'. "
                "Assicurarsi che il volume del segreto sia montato in sola lettura nel container."
            )
        try:
            token = self.token_file_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            raise AgySidecarError(f"Impossibile leggere il file token sidecar AGY '{self.token_file_path}': {e}")

        if len(token) < MIN_TOKEN_LENGTH:
            raise AgySidecarError(
                f"Token sidecar AGY non valido o troppo debole (lunghezza {len(token)}, minima richiesta {MIN_TOKEN_LENGTH})."
            )
        return token

    def check_ready(self, timeout: float = 2.0) -> bool:
        """
        Verifica se il sidecar host AGY è attivo e raggiungibile via socket Unix.
        Invia un messaggio di readiness {'auth_token': token, 'action': 'ready'}
        e valida la risposta.
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
        Invia una richiesta di esecuzione AGY al sidecar host.
        Ritorna l'intero envelope di risposta strutturato (dict con stdout, usage, code, status, duration_sec, ecc.)
        o solleva AgySidecarError / AgySidecarBusyError.
        """
        token = self._load_token()

        if not self.socket_path.exists():
            raise AgySidecarError(
                f"Socket sidecar AGY non disponibile su '{self.socket_path}'. "
                "Verificare che il demone taktstock-agy sia attivo sull'host e la directory montata."
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
            raise AgySidecarError(f"Connessione al socket sidecar AGY fallita: {e}")

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
                raise AgySidecarError("Risposta vuota ricevuta dal socket sidecar AGY.")

            try:
                resp = json.loads(raw_resp)
            except Exception as e:
                raise AgySidecarError(f"Risposta non valida dal sidecar AGY (JSON non conforme): {raw_resp[:200]}")

            status = resp.get("status")
            if status == "BUSY":
                raise AgySidecarBusyError(resp.get("error", "Il runner host AGY è occupato."))

            if status != "SUCCESS":
                err_msg = resp.get("stderr") or resp.get("error") or "Errore sconosciuto durante l'esecuzione di AGY."
                if resp.get("code") == 124 or "timeout" in err_msg.lower():
                    raise AgySidecarTimeoutError(f"Timeout esecuzione AGY ({timeout}s): {err_msg}")
                raise AgySidecarError(f"Esecuzione AGY fallita (codice {resp.get('code', 1)}): {err_msg}")

            return resp

        except (AgySidecarTimeoutError, AgySidecarBusyError):
            raise
        except AgySidecarError:
            raise
        except socket.timeout:
            raise AgySidecarTimeoutError(f"Timeout di comunicazione socket con il sidecar AGY ({timeout + 30}s).")
        except Exception as e:
            raise AgySidecarError(f"Errore IPC con il sidecar host AGY: {e}")
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
        Invia una richiesta di esecuzione AGY al sidecar host.
        Ritorna lo stdout dell'agente o solleva AgySidecarError / AgySidecarBusyError.
        """
        resp = self.send_request_envelope(
            prompt=prompt,
            worktree=worktree,
            sandbox=sandbox,
            timeout=timeout,
            model=model
        )
        return resp.get("stdout", "")
