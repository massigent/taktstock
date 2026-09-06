#!/usr/bin/env python3
"""
Host Codex Socket Daemon (Hardened Production Design)
------------------------------------------------------
Local sidecar daemon running on Debian host (non-root user 'massimo', UID 1000).
Exposes a secure Unix Domain Socket at /run/taktstock-codex/codex.sock (or /run/ufficio-codex/codex.sock).

Mandatory Security Measures:
1. Zero credentials in container: container communicates exclusively via Unix socket IPC.
2. Authentication with dedicated secret: TAKTSTOCK_SIDECAR_TOKEN (or UFFICIO_SIDECAR_TOKEN, min 32 chars).
   Daemon refuses startup and creates no socket if token is missing or weak.
3. Controlled concurrency: exactly 1 job at a time (non-reentrant job lock) with BUSY response.
4. Peer Credentials Verification (SO_PEERCRED on Linux): accepts only UID 1000 (UID 0 prohibited).
5. Real-time streaming with Output Cap on stdout/stderr (max 512 KB):
   If limit is exceeded, child process is terminated immediately (SIGKILL)
   and request fails with structured error, without exhausting memory.
6. Rigorous execution timeout (max 900s).
7. Strict allowlists:
   - Profiles: sol, director, ds-flash, ds-pro, luna
   - Worktree: canonical folders under worktrees/, workspaces/, repos/
   - Sandbox: read-only, workspace-write
8. Secure audit log: records only metadata, lengths, and SHA-256 hashes of prompts;
   no full prompt, token, or secret is ever logged.
"""

import os
import sys
import hmac
import time
import json
import fcntl
import socket
import struct
import hashlib
import logging
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List

# Secure logging setup
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][CodexHostDaemon] %(message)s"
)
logger = logging.getLogger("CodexHostDaemon")

# Configuration and security constants
ALLOWED_PROFILES = {"sol", "director", "ds-flash", "ds-pro", "luna"}
ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write"}
ALLOWED_REASONING_EFFORTS = {"low", "high"}

DEFAULT_SOCKET_PATH = Path(os.environ.get("TAKTSTOCK_CODEX_SOCKET_PATH") or os.environ.get("CODEX_SOCKET_PATH") or "/run/taktstock-codex/codex.sock")
DEFAULT_SOCKET_DIR = DEFAULT_SOCKET_PATH.parent
DEFAULT_CODEX_BIN = Path(os.environ.get("CODEX_BIN") or (Path.home() / "taktstock/bin/codex") if (Path.home() / "taktstock/bin/codex").exists() else (Path.home() / ".local/bin/codex"))
CANONICAL_CODEX_DIR = Path(os.environ.get("CODEX_ACCOUNTS_DIR") or (Path.home() / ".codex/accounts"))

TAKTSTOCK_DIR = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or (Path.home() / "taktstock")).resolve()
DEFAULT_WORKTREE_ROOTS = [
    (TAKTSTOCK_DIR / "worktrees").resolve(),
    (TAKTSTOCK_DIR / "workspaces").resolve(),
    (TAKTSTOCK_DIR / "repos").resolve(),
]

ALLOWED_CLIENT_UIDS = [1000]  # Only UID 1000 (massimo), UID 0 categorically prohibited

MAX_OUTPUT_BYTES = 512 * 1024  # 512 KB max output cap per stream
MAX_TIMEOUT_SECONDS = 900      # 15 minutes max execution timeout
MIN_TOKEN_LENGTH = 32          # Minimum token length to prevent weak secrets


def load_sidecar_token(explicit_token: Optional[str] = None) -> str:
    """Loads and validates sidecar token from argument, file, or environment variable (without fallback)."""
    if explicit_token and len(explicit_token.strip()) >= MIN_TOKEN_LENGTH:
        return explicit_token.strip()

    # 1. Attempt from environment variable
    env_token = (os.environ.get("TAKTSTOCK_SIDECAR_TOKEN") or os.environ.get("UFFICIO_SIDECAR_TOKEN") or "").strip()
    if env_token and len(env_token) >= MIN_TOKEN_LENGTH:
        return env_token

    # 2. Attempt from dedicated secret file
    token_file_path = (os.environ.get("TAKTSTOCK_SIDECAR_TOKEN_FILE") or os.environ.get("UFFICIO_SIDECAR_TOKEN_FILE") or "").strip()
    if token_file_path and Path(token_file_path).exists():
        try:
            file_token = Path(token_file_path).read_text(encoding="utf-8").strip()
            if len(file_token) >= MIN_TOKEN_LENGTH:
                return file_token
        except Exception as e:
            logger.error(f"Error reading token file {token_file_path}: {e}")

    # 3. Attempt from single persistent master configuration source
    for p in [Path.home() / ".config/taktstock-codex/sidecar.env", Path.home() / ".config/ufficio-codex/sidecar.env"]:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.startswith("TAKTSTOCK_SIDECAR_TOKEN=") or line.startswith("UFFICIO_SIDECAR_TOKEN="):
                        t = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if len(t) >= MIN_TOKEN_LENGTH:
                            return t
            except Exception:
                pass

    raise ValueError(
        f"TAKTSTOCK_SIDECAR_TOKEN not configured or invalid (non configurato o non valido): a key of at least {MIN_TOKEN_LENGTH} characters is required. "
        "Configure TAKTSTOCK_SIDECAR_TOKEN or TAKTSTOCK_SIDECAR_TOKEN_FILE before starting daemon."
    )


def get_peer_credentials(sock: socket.socket) -> Optional[Tuple[int, int, int]]:
    """Recupera PID, UID, GID del processo client tramite SO_PEERCRED (Linux)."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            ucred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", ucred)
            return pid, uid, gid
    except Exception as e:
        logger.warning(f"Impossibile leggere SO_PEERCRED: {e}")
    return None


def validate_sidecar_request(
    payload: Dict[str, Any],
    expected_token: str,
    allowed_roots: Optional[List[Path]] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """Strictly validates input payload using allowlist schema."""
    if not isinstance(payload, dict):
        return False, "Invalid payload (Payload non valido): must be a JSON object.", {}

    # 1. Authentication with dedicated secret (timing-safe)
    auth_token = str(payload.get("auth_token", "")).strip()
    if not auth_token or not hmac.compare_digest(auth_token, expected_token):
        return False, "Socket authentication failed (Autenticazione socket fallita): invalid or unauthorized token.", {}

    # 1b. Fast readiness/health check operation (zero Codex execution, zero worktree/prompt)
    action = str(payload.get("action", "")).strip().lower()
    if action in ["ready", "health"]:
        return True, "", {"action": "ready"}

    # 2. Profile allowlist
    profile = str(payload.get("profile", "")).strip().lower()
    if profile not in ALLOWED_PROFILES:
        return False, f"Profile '{profile}' not allowed (Profilo non consentito). Allowed: {sorted(list(ALLOWED_PROFILES))}", {}

    # 3. Sandbox Mode allowlist
    sandbox = str(payload.get("sandbox", "read-only")).strip().lower()
    if sandbox not in ALLOWED_SANDBOX_MODES:
        return False, f"Sandbox mode '{sandbox}' invalid (Sandbox mode non valido). Allowed: {sorted(list(ALLOWED_SANDBOX_MODES))}", {}

    # 4. Worktree directory (Anti-Path Traversal with canonical resolution)
    worktree_raw = str(payload.get("worktree", "")).strip()
    if not worktree_raw:
        return False, "Field 'worktree' is required (obbligatorio).", {}

    worktree_path = Path(worktree_raw).resolve()
    if not worktree_path.exists() or not worktree_path.is_dir():
        return False, f"Worktree directory not found: {worktree_raw}", {}

    roots = [r.resolve() for r in (allowed_roots or DEFAULT_WORKTREE_ROOTS)]

    # Reject root itself: must be a real subdirectory
    if any(worktree_path == root for root in roots):
        return False, f"Access denied (Accesso negato): '{worktree_raw}' is a root directory (directory radice). A valid subdirectory under worktrees/ or workspaces/ is required.", {}

    is_safe_child = any(
        root in worktree_path.parents
        for root in roots
    )
    if not is_safe_child:
        return False, f"Access denied (Accesso negato) to path '{worktree_raw}': outside authorized worktrees (fuori dai worktree autorizzati).", {}

    # 5. Reasoning Effort
    effort = str(payload.get("reasoning_effort", "low")).strip().lower()
    if effort not in ALLOWED_REASONING_EFFORTS:
        effort = "low"

    # 6. Prompt
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return False, "Field 'prompt' is required and non-empty (Campo 'prompt' obbligatorio e non vuoto).", {}

    sanitized = {
        "profile": profile,
        "worktree": str(worktree_path),
        "sandbox": sandbox,
        "reasoning_effort": effort,
        "prompt": prompt.strip()
    }
    return True, "", sanitized


class HostCodexDaemon:
    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        auth_token: Optional[str] = None,
        codex_bin_path: Path = DEFAULT_CODEX_BIN,
        allowed_roots: Optional[List[Path]] = None,
        allowed_client_uids: Optional[List[int]] = None,
        canonical_accounts_dir: Path = CANONICAL_CODEX_DIR
    ):
        # Valida rigorosamente il token all'inizializzazione: fallisce prima di creare il socket se assente
        self.auth_token = load_sidecar_token(auth_token)
        self.socket_path = Path(socket_path)
        self.codex_bin = Path(codex_bin_path)
        self.allowed_roots = allowed_roots
        self.allowed_client_uids = allowed_client_uids if allowed_client_uids is not None else ALLOWED_CLIENT_UIDS
        self.canonical_accounts_dir = Path(canonical_accounts_dir)
        self.running = False
        self.job_lock = threading.Lock()  # Exactly 1 job at a time
        self.instance_lock_path = Path(f"{self.socket_path}.lock")
        self._instance_lock_file = None

    def _acquire_instance_lock(self) -> None:
        """Prevents two daemons from non-deterministically serving the same socket."""
        self.instance_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.instance_lock_path, "a+", encoding="utf-8")
        try:
            os.chmod(self.instance_lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError(
                f"Another instance of HostCodexDaemon is already active ({self.instance_lock_path})."
            )
        self._instance_lock_file = lock_file

    def _release_instance_lock(self) -> None:
        if self._instance_lock_file is None:
            return
        try:
            fcntl.flock(self._instance_lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._instance_lock_file.close()
            self._instance_lock_file = None

    def audit_log(self, event: str, details: Dict[str, Any]):
        """Writes a structured audit record without sensitive data."""
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **details
        }
        logger.info(f"[AUDIT] {json.dumps(record, ensure_ascii=False)}")

    def execute_codex(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Executes Codex natively on the host with continuous streaming, strict cap, and forced termination."""
        profile = req["profile"]
        worktree = req["worktree"]
        sandbox = req["sandbox"]
        effort = req["reasoning_effort"]
        prompt = req["prompt"]

        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        start_time = time.time()

        account_name = "sol" if profile in ["sol", "director"] else ("luna" if profile == "luna" else "bonus")
        account_home = self.canonical_accounts_dir / account_name

        cmd = [
            str(self.codex_bin),
            "exec",
            "--profile", profile,
            "-s", sandbox,
            "--skip-git-repo-check"
        ]
        if profile in ["sol", "director"]:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
        cmd.append(prompt)

        env = os.environ.copy()
        env["CODEX_HOME"] = str(account_home)
        # Rigorous sanitization of child environment: remove secrets and sidecar internal variables
        for secret_var in [
            "TAKTSTOCK_SIDECAR_TOKEN",
            "TAKTSTOCK_SIDECAR_TOKEN_FILE",
            "UFFICIO_SIDECAR_TOKEN",
            "UFFICIO_SIDECAR_TOKEN_FILE",
            "DASHBOARD_PASSWORD",
            "TAKTSTOCK_AUTH_TOKEN",
            "UFFICO_AUTH_TOKEN"
        ]:
            env.pop(secret_var, None)

        self.audit_log("JOB_STARTED", {
            "profile": profile,
            "sandbox": sandbox,
            "reasoning_effort": effort,
            "worktree": worktree,
            "prompt_len": len(prompt),
            "prompt_sha256": prompt_hash
        })

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=worktree,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL
            )

            stdout_chunks: List[bytes] = []
            stderr_chunks: List[bytes] = []
            cap_exceeded = threading.Event()

            def stream_reader(pipe, buffer_list, cap_limit):
                total = 0
                try:
                    while True:
                        chunk = pipe.read(4096)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > cap_limit:
                            cap_exceeded.set()
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            break
                        buffer_list.append(chunk)
                except Exception:
                    pass
                finally:
                    try:
                        pipe.close()
                    except Exception:
                        pass

            t_out = threading.Thread(target=stream_reader, args=(proc.stdout, stdout_chunks, MAX_OUTPUT_BYTES), daemon=True)
            t_err = threading.Thread(target=stream_reader, args=(proc.stderr, stderr_chunks, MAX_OUTPUT_BYTES), daemon=True)
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=MAX_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                duration = round(time.time() - start_time, 2)
                self.audit_log("JOB_TIMEOUT", {"profile": profile, "duration_sec": duration})
                return {
                    "status": "ERROR",
                    "code": 124,
                    "stdout": "",
                    "stderr": f"Codex execution timeout (Timeout esecuzione Codex) ({MAX_TIMEOUT_SECONDS}s)."
                }

            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
            duration = round(time.time() - start_time, 2)

            if cap_exceeded.is_set():
                self.audit_log("JOB_CAP_EXCEEDED", {"profile": profile, "duration_sec": duration})
                return {
                    "status": "ERROR",
                    "code": 137,
                    "stdout": "",
                    "stderr": f"Output limit exceeded (Limite di output superato) (max {MAX_OUTPUT_BYTES} byte). Processo terminato forzatamente per prevenire esaurimento memoria."
                }

            stdout_str = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr_str = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            self.audit_log("JOB_COMPLETED", {
                "profile": profile,
                "exit_code": proc.returncode,
                "duration_sec": duration,
                "stdout_len": len(stdout_str)
            })

            return {
                "status": "SUCCESS" if proc.returncode == 0 else "ERROR",
                "code": proc.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "duration_sec": duration
            }

        except Exception as e:
            duration = round(time.time() - start_time, 2)
            self.audit_log("JOB_ERROR", {"profile": profile, "duration_sec": duration, "error": str(e)})
            return {"status": "ERROR", "code": 1, "stdout": "", "stderr": str(e)}

    def handle_client(self, client_sock: socket.socket):
        """Handles a client connection with peer validation, concurrency, and payload checks."""
        try:
            # 1. Verify Peer Credentials: accepts only allowed UID, UID 0 strictly rejected
            peer = get_peer_credentials(client_sock)
            if peer:
                pid, uid, gid = peer
                if uid == 0 or (self.allowed_client_uids is not None and uid not in self.allowed_client_uids):
                    logger.warning(f"Rejected connection from unauthorized UID {uid} (PID {pid})")
                    self.audit_log("UNAUTHORIZED_PEER_UID", {"uid": uid, "pid": pid})
                    client_sock.sendall(json.dumps({
                        "status": "ERROR",
                        "error": f"UID {uid} non autorizzato sul socket (UID 0 categoricamente vietato)."
                    }).encode("utf-8"))
                    return

            # 2. Read payload stream with max limit check
            data = b""
            while True:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_OUTPUT_BYTES:
                    self.audit_log("REQUEST_SIZE_EXCEEDED", {"received_bytes": len(data)})
                    client_sock.sendall(json.dumps({
                        "status": "ERROR",
                        "error": f"Socket request exceeds maximum allowed size (dimensione massima consentita) ({MAX_OUTPUT_BYTES} byte)."
                    }).encode("utf-8"))
                    return
                if b"\n" in data:
                    break

            if not data:
                return

            raw_str = data.decode("utf-8", errors="ignore").strip()
            try:
                req_json = json.loads(raw_str)
            except Exception as e:
                client_sock.sendall(json.dumps({"status": "ERROR", "error": f"Malformed JSON (JSON malformato): {e}"}).encode("utf-8"))
                return

            # 3. Validate schema and token
            is_valid, err_msg, sanitized = validate_sidecar_request(req_json, self.auth_token, allowed_roots=self.allowed_roots)
            if not is_valid:
                self.audit_log("REQUEST_REJECTED", {"reason": err_msg})
                client_sock.sendall(json.dumps({"status": "ERROR", "error": err_msg}).encode("utf-8"))
                return

            # 3b. Fast readiness/health check: immediate response without job_lock or child processes
            if sanitized.get("action") == "ready":
                self.audit_log("READINESS_CHECK", {"status": "SUCCESS"})
                client_sock.sendall(json.dumps({
                    "status": "SUCCESS",
                    "service": "taktstock-codex",
                    "ready": True
                }).encode("utf-8"))
                return

            # 4. Concurrency: exactly 1 job at a time
            acquired = self.job_lock.acquire(blocking=False)
            if not acquired:
                self.audit_log("REQUEST_BUSY", {"profile": sanitized.get("profile")})
                client_sock.sendall(json.dumps({
                    "status": "BUSY",
                    "error": "The host runner is currently busy (occupato) with another job. Please retry later."
                }).encode("utf-8"))
                return

            try:
                result = self.execute_codex(sanitized)
                client_sock.sendall(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            finally:
                self.job_lock.release()

        except Exception as e:
            logger.error(f"Error handling socket client: {e}")
            try:
                client_sock.sendall(json.dumps({"status": "ERROR", "error": str(e)}).encode("utf-8"))
            except Exception:
                pass
        finally:
            client_sock.close()

    def run(self):
        """Starts the Unix Domain Socket server."""
        self._acquire_instance_lock()

        try:
            if self.socket_path.exists():
                self.socket_path.unlink()

            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.socket_path))
            try:
                os.chmod(str(self.socket_path), 0o600)
            except Exception:
                pass
            server.listen(10)
            self.running = True
            logger.info(f"Codex Host Daemon listening on Unix Socket: {self.socket_path} (mode 0600)")

            while self.running:
                client_sock, _ = server.accept()
                threading.Thread(target=self.handle_client, args=(client_sock,), daemon=True).start()
        except KeyboardInterrupt:
            logger.info("Codex daemon shutdown requested.")
        finally:
            if "server" in locals():
                server.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            self._release_instance_lock()


if __name__ == "__main__":
    daemon = HostCodexDaemon()
    daemon.run()
