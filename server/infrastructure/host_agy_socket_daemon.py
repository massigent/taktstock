#!/usr/bin/env python3
"""
Taktstock Host AGY Runner Daemon (Sidecar)
------------------------------------------
Isolated Unix Domain Socket daemon on Debian host for executing Antigravity CLI (agy).
Guarantees:
- Complete isolation of Gemini credentials and API keys outside the container.
- Authentication with dedicated secret TAKTSTOCK_AGY_SIDECAR_TOKEN (never shared with Codex).
- Peer credentials validation (SO_PEERCRED UID 1000, UID 0 categorically forbidden).
- Anti-path traversal and strict confinement to authorized worktrees/workspaces only.
- Single-job concurrency lock with BUSY state.
- 512 KB output cap with real-time streaming and immediate kill on quota exceedance.
- Child environment on STRICT ALLOWLIST: only PATH, HOME, USER, LANG/locales. No TAKTSTOCK_*, UFFICIO_*, API keys, passwords, or tokens.
- Authenticated protocol supporting 'ready' and 'execute' actions.
"""

import os
import sys
import json
import time
import hmac
import stat
import struct
import socket
import logging
import hashlib
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List, Set

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][AgyHostDaemon] %(message)s"
)
logger = logging.getLogger("AgyHostDaemon")

# Security Configurations and Defaults
DEFAULT_SOCKET_PATH = Path(os.environ.get("AGY_SOCKET_PATH") or os.environ.get("TAKTSTOCK_AGY_SOCKET_PATH") or "/run/taktstock-agy/agy.sock")
DEFAULT_TOKEN_FILE = Path(os.environ.get("TAKTSTOCK_AGY_SIDECAR_TOKEN_FILE") or os.environ.get("UFFICIO_AGY_SIDECAR_TOKEN_FILE") or "/run/taktstock-agy/token")
_default_config = Path(os.environ.get("TAKTSTOCK_AGY_CONFIG") or os.environ.get("UFFICIO_AGY_CONFIG") or (Path.home() / ".config" / "taktstock-agy" / "sidecar.env"))
if not _default_config.exists() and (Path.home() / ".config" / "ufficio-agy" / "sidecar.env").exists():
    _default_config = Path.home() / ".config" / "ufficio-agy" / "sidecar.env"
DEFAULT_MASTER_CONFIG = _default_config

_taktstock_base = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or (Path.home() / "taktstock")).resolve()
DEFAULT_WORKTREE_ROOTS = [
    (_taktstock_base / "worktrees").resolve(),
    (_taktstock_base / "workspaces").resolve(),
    (_taktstock_base / "repos").resolve()
]

ALLOWED_SANDBOX_MODES: Set[str] = {"read-only", "workspace-write"}
ALLOWED_CLIENT_UIDS: Set[int] = {1000}
MIN_TOKEN_LENGTH: int = 32
MAX_OUTPUT_BYTES: int = 512 * 1024       # 512 KB to prevent memory DoS
MAX_TIMEOUT_SECONDS: int = 1800          # 30 minutes maximum

# Strict allowlist for AGY child process environment
CHILD_ENV_ALLOWLIST: Set[str] = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SHELL",
    "TMPDIR",
    "TERM"
}


def load_agy_sidecar_token(explicit_token: Optional[str] = None) -> str:
    """Loads and validates the AGY sidecar token from argument, file, or master config."""
    if explicit_token and len(explicit_token.strip()) >= MIN_TOKEN_LENGTH:
        return explicit_token.strip()

    # 1. Attempt from environment variable
    env_token = (os.environ.get("TAKTSTOCK_AGY_SIDECAR_TOKEN") or os.environ.get("UFFICIO_AGY_SIDECAR_TOKEN") or "").strip()
    if env_token and len(env_token) >= MIN_TOKEN_LENGTH:
        return env_token

    # 2. Attempt from dedicated runtime secret file
    token_file_path = (os.environ.get("TAKTSTOCK_AGY_SIDECAR_TOKEN_FILE") or os.environ.get("UFFICIO_AGY_SIDECAR_TOKEN_FILE") or str(DEFAULT_TOKEN_FILE)).strip()
    if token_file_path and Path(token_file_path).exists():
        try:
            file_token = Path(token_file_path).read_text(encoding="utf-8").strip()
            if len(file_token) >= MIN_TOKEN_LENGTH:
                return file_token
        except Exception as e:
            logger.error(f"Error reading token file {token_file_path}: {e}")

    # 3. Attempt from persistent master protected configuration
    if DEFAULT_MASTER_CONFIG.exists():
        try:
            for line in DEFAULT_MASTER_CONFIG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TAKTSTOCK_AGY_SIDECAR_TOKEN=") or line.startswith("UFFICIO_AGY_SIDECAR_TOKEN="):
                    t = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if len(t) >= MIN_TOKEN_LENGTH:
                        return t
        except Exception:
            pass

    raise ValueError(
        f"TAKTSTOCK_AGY_SIDECAR_TOKEN not configured or invalid (non configurato o non valido): a key of at least {MIN_TOKEN_LENGTH} characters is required. "
        "Configure TAKTSTOCK_AGY_SIDECAR_TOKEN or TAKTSTOCK_AGY_SIDECAR_TOKEN_FILE before starting the AGY daemon."
    )


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


def validate_agy_request(
    payload: Dict[str, Any],
    expected_token: str,
    allowed_roots: Optional[List[Path]] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """Strictly validates the incoming payload for the AGY sidecar."""
    if not isinstance(payload, dict):
        return False, "Invalid payload (Payload non valido): must be a JSON object.", {}

    # 1. Authentication with dedicated AGY secret (timing-safe)
    auth_token = str(payload.get("auth_token", "")).strip()
    if not auth_token or not hmac.compare_digest(auth_token, expected_token):
        return False, "Socket authentication failed (Autenticazione socket fallita): invalid or unauthorized AGY token.", {}

    # 1b. Fast readiness/health check operation (zero agy execution)
    action = str(payload.get("action", "")).strip().lower()
    if action in ["ready", "health"]:
        return True, "", {"action": "ready"}

    # 2. Worktree validation (Anti-Path Traversal with canonical resolution)
    worktree_raw = str(payload.get("worktree", "")).strip()
    if not worktree_raw:
        return False, "Field 'worktree' required for agy execution (Campo 'worktree' obbligatorio).", {}

    worktree_path = Path(worktree_raw).resolve()
    if not worktree_path.exists() or not worktree_path.is_dir():
        return False, f"Worktree directory not found (Directory worktree non trovata): {worktree_raw}", {}

    roots = [r.resolve() for r in (allowed_roots or DEFAULT_WORKTREE_ROOTS)]

    # Reject the root itself: must be a real subdirectory
    if any(worktree_path == root for root in roots):
        return False, f"Access denied (Accesso negato): path '{worktree_raw}' is a root directory (directory radice). A valid subdirectory under worktrees/ or workspaces/ is required.", {}

    is_safe_child = any(root in worktree_path.parents for root in roots)
    if not is_safe_child:
        return False, f"Access denied to path '{worktree_raw}': outside authorized worktrees for AGY (fuori dai worktree autorizzati per AGY).", {}

    # 3. Sandbox Mode
    sandbox = str(payload.get("sandbox", "workspace-write")).strip().lower()
    if sandbox not in ALLOWED_SANDBOX_MODES:
        return False, f"Invalid sandbox mode '{sandbox}' for AGY (Sandbox mode non valido). Valid: {sorted(list(ALLOWED_SANDBOX_MODES))}", {}

    # 4. Timeout
    try:
        timeout_val = int(payload.get("timeout", 900))
        if timeout_val < 5 or timeout_val > MAX_TIMEOUT_SECONDS:
            timeout_val = 900
    except (ValueError, TypeError):
        timeout_val = 900

    # 5. Prompt
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return False, "Field 'prompt' required and non-empty for agy execution (Campo 'prompt' obbligatorio).", {}

    # 6. Model (optional)
    model = payload.get("model")
    if model is not None:
        model = str(model).strip()
        if not model:
            model = None

    sanitized = {
        "action": "execute",
        "worktree": str(worktree_path),
        "sandbox": sandbox,
        "timeout": timeout_val,
        "prompt": prompt.strip(),
        "model": model
    }
    return True, "", sanitized


def resolve_agy_bin(explicit_bin: Optional[str] = None) -> str:
    """Resolves and validates the AGY binary path from parameter, variable, or master config."""
    bin_path = explicit_bin or os.environ.get("AGY_BIN")
    if not bin_path and DEFAULT_MASTER_CONFIG.exists():
        try:
            for line in DEFAULT_MASTER_CONFIG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("AGY_BIN="):
                    bin_path = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass

    if not bin_path:
        raise ValueError(
            "AGY_BIN not configured: specify the AGY binary path via AGY_BIN environment variable or in file "
            f"'{DEFAULT_MASTER_CONFIG}'."
        )

    p = Path(bin_path).resolve()
    # If the path does not exist or is not executable, fail on startup (fail-closed)
    if not p.exists() or not os.access(str(p), os.X_OK):
        raise ValueError(f"AGY binary '{bin_path}' not found or not executable (non trovato o non eseguibile).")
    return str(p)


def resolve_agy_home(explicit_home: Optional[str] = None) -> Path:
    """Resolves the dedicated isolated home directory for AGY."""
    home_path = explicit_home or os.environ.get("AGY_HOME")
    if not home_path and DEFAULT_MASTER_CONFIG.exists():
        try:
            for line in DEFAULT_MASTER_CONFIG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("AGY_HOME="):
                    home_path = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass
    fallback_agy_home = Path.home() / ".config" / "taktstock-agy" / "home"
    if not fallback_agy_home.exists() and (Path.home() / ".config" / "ufficio-agy" / "home").exists():
        fallback_agy_home = Path.home() / ".config" / "ufficio-agy" / "home"
    return Path(home_path or os.environ.get("AGY_HOME") or fallback_agy_home).resolve()


class HostAgyDaemon:
    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        auth_token: Optional[str] = None,
        allowed_roots: Optional[List[Path]] = None,
        allowed_client_uids: Optional[Set[int]] = ALLOWED_CLIENT_UIDS,
        agy_binary_path: Optional[str] = None,
        agy_home: Optional[str] = None
    ):
        self.socket_path = socket_path
        self.auth_token = load_agy_sidecar_token(auth_token)
        self.allowed_roots = allowed_roots or DEFAULT_WORKTREE_ROOTS
        self.allowed_client_uids = allowed_client_uids
        self.agy_binary = resolve_agy_bin(agy_binary_path)
        self.agy_home = resolve_agy_home(agy_home)
        self.job_lock = threading.Lock()
        self.running = False

    def audit_log(self, event: str, details: Dict[str, Any]):
        """Safely logs audit events without including full prompt or tokens."""
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": "host_agy_daemon",
            "event": event,
            **details
        }
        logger.info(f"[AUDIT] {json.dumps(entry)}")

    def build_child_env(self) -> Dict[str, str]:
        """Builds child process environment based exclusively on STRICT ALLOWLIST and AGY_HOME."""
        env = {}
        for k, v in os.environ.items():
            if k in CHILD_ENV_ALLOWLIST:
                env[k] = v

        # Set safe defaults confined to AGY_HOME
        current_user = os.environ.get("USER") or Path.home().name or "taktstock"
        env["USER"] = current_user
        env["LOGNAME"] = current_user
        env["HOME"] = str(self.agy_home)
        if "PATH" not in env:
            env["PATH"] = f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin"
        return env

    def execute_agy(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Executes isolated Antigravity CLI (agy) on authorized host worktree with deterministic sandbox."""
        prompt = params["prompt"]
        worktree = params["worktree"]
        sandbox = params["sandbox"]
        timeout = params.get("timeout", 900)
        model = params.get("model")

        start_time = time.time()
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

        # Build agy command with mandatory sandbox and mode flags (never bypass permissions)
        cmd = [
            self.agy_binary,
            "-p", prompt,
            # The AGY CLI JSON format returns a technical envelope that in some
            # versions has an empty 'response' even with exit code 0. The sidecar
            # must therefore receive final text to forward downstream.
            "--output-format", "text",
            # Chat text is untrusted data: must not trigger slash commands or CLI skills.
            "--disable-slash-commands",
            "--print-timeout", f"{timeout}s",
            "--add-dir", str(worktree),
            "--sandbox"
        ]
        if sandbox == "read-only":
            cmd.extend(["--mode", "plan"])
        elif sandbox == "workspace-write":
            cmd.extend(["--mode", "accept-edits"])

        if model:
            cmd.extend(["--model", model])

        # Strict sanitization on minimal allowlist (zero Taktstock/API secrets)
        child_env = self.build_child_env()

        self.audit_log("JOB_STARTED", {
            "sandbox": sandbox,
            "worktree": worktree,
            "timeout_sec": timeout,
            "prompt_len": len(prompt),
            "prompt_sha256": prompt_hash
        })

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=worktree,
                env=child_env,
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
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                duration = round(time.time() - start_time, 2)
                self.audit_log("JOB_TIMEOUT", {"duration_sec": duration})
                return {
                    "status": "ERROR",
                    "code": 124,
                    "stdout": "",
                    "stderr": f"AGY execution timeout (Timeout esecuzione AGY) ({timeout}s)."
                }

            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
            duration = round(time.time() - start_time, 2)

            if cap_exceeded.is_set():
                self.audit_log("JOB_CAP_EXCEEDED", {"duration_sec": duration})
                return {
                    "status": "ERROR",
                    "code": 137,
                    "stdout": "",
                    "stderr": f"Output limit exceeded (Limite di output superato) (max {MAX_OUTPUT_BYTES} byte). Process forcibly terminated."
                }

            stdout_str = b"".join(stdout_chunks).decode("utf-8", errors="replace")
            stderr_str = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            self.audit_log("JOB_COMPLETED", {
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
            self.audit_log("JOB_ERROR", {"duration_sec": duration, "error": str(e)})
            return {"status": "ERROR", "code": 1, "stdout": "", "stderr": str(e)}

    def handle_client(self, client_sock: socket.socket):
        """Handles a client connection with peer credentials validation, token, and concurrency."""
        try:
            # 1. Verify Peer Credentials: UID 0 categorically rejected
            peer = get_peer_credentials(client_sock)
            if peer:
                pid, uid, gid = peer
                if uid == 0 or (self.allowed_client_uids is not None and uid not in self.allowed_client_uids):
                    logger.warning(f"Rejected connection from unauthorized UID {uid} (PID {pid})")
                    self.audit_log("UNAUTHORIZED_PEER_UID", {"uid": uid, "pid": pid})
                    client_sock.sendall(json.dumps({
                        "status": "ERROR",
                        "error": f"UID {uid} not authorized on AGY socket (UID 0 categoricamente vietato / categorically forbidden)."
                    }).encode("utf-8"))
                    return

            # 2. Read stream payload
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
                client_sock.sendall(json.dumps({"status": "ERROR", "error": f"Malformed JSON: {e}"}).encode("utf-8"))
                return

            # 3. Validate schema and token
            is_valid, err_msg, sanitized = validate_agy_request(req_json, self.auth_token, allowed_roots=self.allowed_roots)
            if not is_valid:
                self.audit_log("REQUEST_REJECTED", {"reason": err_msg})
                client_sock.sendall(json.dumps({"status": "ERROR", "error": err_msg}).encode("utf-8"))
                return

            # 3b. Fast readiness/health check: immediate response without job_lock or child processes
            if sanitized.get("action") == "ready":
                self.audit_log("READINESS_CHECK", {"status": "SUCCESS"})
                client_sock.sendall(json.dumps({
                    "status": "SUCCESS",
                    "service": "taktstock-agy",
                    "ready": True
                }).encode("utf-8"))
                return

            # 4. Concurrency: 1 job at a time
            acquired = self.job_lock.acquire(blocking=False)
            if not acquired:
                self.audit_log("REQUEST_BUSY", {})
                client_sock.sendall(json.dumps({
                    "status": "BUSY",
                    "error": "The AGY host runner is currently busy (occupato) with another task. Please retry later."
                }).encode("utf-8"))
                return

            try:
                result = self.execute_agy(sanitized)
                client_sock.sendall(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            finally:
                self.job_lock.release()

        except Exception as e:
            logger.error(f"Error handling AGY client socket: {e}")
            try:
                client_sock.sendall(json.dumps({"status": "ERROR", "error": str(e)}).encode("utf-8"))
            except Exception:
                pass
        finally:
            client_sock.close()

    def run(self):
        """Starts Unix Domain Socket server for AGY."""
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
        logger.info(f"AGY Host Daemon listening on Unix Socket: {self.socket_path} (mode 0600)")

        try:
            while self.running:
                client_sock, _ = server.accept()
                threading.Thread(target=self.handle_client, args=(client_sock,), daemon=True).start()
        except KeyboardInterrupt:
            logger.info("Closing AGY daemon upon request.")
        finally:
            server.close()
            if self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except Exception:
                    pass


if __name__ == "__main__":
    daemon = HostAgyDaemon()
    daemon.run()
