#!/usr/bin/env python3
"""
Host Codex Socket Client (Hardened Production IPC)
--------------------------------------------------
Client per la comunicazione IPC su Unix Domain Socket con il demone
sidecar host (taktstock-codex.service).

Misure di Sicurezza Tassative:
1. Lettura del segreto TAKTSTOCK_SIDECAR_TOKEN_FILE da file protetto (read-only):
   Se il file è assente, vuoto o con token < 32 caratteri, fail-closed immediato
   senza aprire alcuna connessione socket.
2. Mai loggare il token o il contenuto del file.
3. Limite massimo sulla risposta socket ricevuta (MAX_RESPONSE_BYTES = 512 KB)
   per prevenire saturazione di memoria nel client.
4. Nessun fallback a comandi CLI locali o account nel container in caso di errore.
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
DEFAULT_TIMEOUT_SECONDS = 900     # 15 minuti
MAX_RESPONSE_BYTES = 512 * 1024   # 512 KB max payload in ricezione
MIN_TOKEN_LENGTH = 32             # Lunghezza minima token


class CodexSidecarError(RuntimeError):
    """Eccezione sollevata quando il sidecar host restituisce un errore."""
    pass


class CodexSidecarBusyError(CodexSidecarError):
    """Eccezione sollevata quando il runner host è occupato con un altro job."""
    pass


def load_client_sidecar_token(explicit_token: Optional[str] = None, token_file: Optional[Path] = None) -> str:
    """Carica e valida il token da file segreto o argomento esplicito (File-Only, nessun fallback env)."""
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
        "Autenticazione sidecar fallita: file segreto TAKTSTOCK_SIDECAR_TOKEN_FILE non trovato, illeggibile o token non valido (minimo 32 caratteri). "
        "Configurazione a fail-closed: nessuna connessione socket tentata."
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
        """Risolve il token valido o solleva CodexSidecarError (fail closed)."""
        return load_client_sidecar_token(self.auth_token, self.token_file)

    def check_ready(self, timeout: float = 2.0) -> bool:
        """
        Verifica se il demone sidecar host Codex è attivo e raggiungibile via socket Unix.
        Invia un messaggio di readiness {'auth_token': token, 'action': 'ready'}
        e valida la risposta.
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
        """Invia una richiesta di esecuzione al demone sidecar host via Unix socket."""
        # 1. Validazione token all'origine (Fail-Closed prima di connettere il socket)
        token = self._get_valid_token()

        if not self.socket_path.exists():
            raise CodexSidecarError(
                f"Socket del sidecar host non trovato in {self.socket_path}. "
                "Verificare che il servizio host taktstock-codex sia attivo."
            )

        payload: Dict[str, Any] = {
            "auth_token": token,
            "profile": profile,
            "worktree": str(worktree),
            "prompt": prompt,
            "sandbox": sandbox,
        }
        # Regola di business: Luna non deve ricevere override di reasoning effort
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
                        f"Risposta del sidecar host supera la dimensione massima consentita ({MAX_RESPONSE_BYTES} byte)."
                    )
            sock.close()
            sock = None

            if not raw_data:
                raise CodexSidecarError("Risposta vuota ricevuta dal sidecar host.")

            resp = json.loads(raw_data.decode("utf-8", errors="ignore"))

        except socket.timeout:
            raise CodexSidecarError(f"Timeout ({self.timeout}s) durante la comunicazione con il sidecar host.")
        except json.JSONDecodeError as e:
            raise CodexSidecarError(f"Risposta non valida (JSON malformato) dal sidecar host: {e}")
        except Exception as e:
            if isinstance(e, CodexSidecarError):
                raise
            raise CodexSidecarError(f"Errore connessione socket sidecar host ({self.socket_path}): {e}")
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
            err_msg = resp.get("error", "Il runner host è attualmente occupato con un altro job. Riprova più tardi.")
            raise CodexSidecarBusyError(err_msg)
        else:
            err_msg = resp.get("error") or resp.get("stderr") or "Errore non specificato dal sidecar host."
            raise CodexSidecarError(f"Esecuzione Codex fallita su host: {err_msg}")
