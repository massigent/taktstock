#!/usr/bin/env python3
"""
Unit Tests for Host Codex Socket Daemon (Sidecar PoC)
------------------------------------------------------
Testa:
1. Rifiuto di richieste con token di autenticazione errato o mancante
2. Rifiuto di profili non compresi nella whitelist
3. Rifiuto di percorsi worktree non consentiti o tentativi di Path Traversal
4. Rifiuto di modalità sandbox non valide
5. Accettazione di richieste conformi allo schema di sicurezza
6. Comunicazione client-server end-to-end su Unix domain socket
"""

import os
import sys
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from infrastructure.host_codex_socket_daemon import (
    validate_sidecar_request,
    HostCodexDaemon,
    ALLOWED_PROFILES,
    ALLOWED_SANDBOX_MODES
)


def connect_with_retry(sock: socket.socket, path: Path, max_attempts: int = 50, delay: float = 0.05) -> bool:
    import time
    for _ in range(max_attempts):
        try:
            sock.connect(str(path))
            return True
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(delay)
    return False


class TestHostCodexSidecarValidation(unittest.TestCase):
    def setUp(self):
        self.expected_token = "secret_sidecar_token_1234567890123456"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.worktree_dir = Path(self.temp_dir.name) / "worktrees" / "wt_test"
        self.worktree_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_daemon_raises_and_creates_no_socket_when_token_is_missing_or_short(self):
        """0. Il demone fallisce immediatamente all'avvio e non crea socket se il token è assente o debole."""
        sock_path = Path(self.temp_dir.name) / "no_token.sock"
        orig_exists = Path.exists

        def fake_exists(path_obj):
            if "sidecar.env" in str(path_obj):
                return False
            return orig_exists(path_obj)

        with patch.dict(os.environ, {"UFFICIO_SIDECAR_TOKEN": "", "UFFICIO_SIDECAR_TOKEN_FILE": ""}), \
             patch.object(Path, "exists", fake_exists):
            with self.assertRaises(ValueError):
                HostCodexDaemon(socket_path=sock_path, auth_token="")

            with self.assertRaises(ValueError):
                HostCodexDaemon(socket_path=sock_path, auth_token="too_short_weak_token")

        self.assertFalse(sock_path.exists())

    def test_missing_or_invalid_auth_token_rejected(self):
        """1. Richieste con token errato o mancante devono essere respinte."""
        payload = {
            "auth_token": "wrong_token",
            "profile": "sol",
            "worktree": str(self.worktree_dir),
            "prompt": "Test prompt",
            "sandbox": "read-only"
        }
        is_valid, err_msg, _ = validate_sidecar_request(payload, self.expected_token)
        self.assertFalse(is_valid)
        self.assertIn("Autenticazione", err_msg)

    def test_unknown_profile_rejected(self):
        """2. Profili non presenti nella whitelist vengono categoricamente rifiutati."""
        payload = {
            "auth_token": self.expected_token,
            "profile": "arbitrary_attacker_profile",
            "worktree": str(self.worktree_dir),
            "prompt": "Test prompt",
            "sandbox": "read-only"
        }
        is_valid, err_msg, _ = validate_sidecar_request(payload, self.expected_token)
        self.assertFalse(is_valid)
        self.assertIn("non consentito", err_msg)

    def test_path_traversal_worktree_rejected(self):
        """3. Tentativi di Path Traversal o percorsi fuori dai worktree autorizzati vengono bloccati."""
        payload = {
            "auth_token": self.expected_token,
            "profile": "sol",
            "worktree": "/etc",
            "prompt": "Test prompt",
            "sandbox": "read-only"
        }
        is_valid, err_msg, _ = validate_sidecar_request(payload, self.expected_token)
        self.assertFalse(is_valid)
        self.assertIn("negato", err_msg)

    def test_root_worktree_rejected(self):
        """3b. La directory radice stessa di worktrees o workspaces viene rifiutata."""
        root_dir = Path(self.temp_dir.name) / "worktrees"
        root_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "auth_token": self.expected_token,
            "profile": "sol",
            "worktree": str(root_dir),
            "prompt": "Test prompt",
            "sandbox": "read-only"
        }
        is_valid, err_msg, _ = validate_sidecar_request(payload, self.expected_token, allowed_roots=[root_dir])
        self.assertFalse(is_valid)
        self.assertIn("directory radice", err_msg)

    def test_child_worktree_accepted(self):
        """3c. Una sottocartella valida sotto worktrees o workspaces viene regolarmente accettata."""
        root_dir = Path(self.temp_dir.name) / "worktrees"
        child_dir = root_dir / "wt_valid_child"
        child_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "auth_token": self.expected_token,
            "profile": "sol",
            "worktree": str(child_dir),
            "prompt": "Test prompt",
            "sandbox": "read-only"
        }
        is_valid, err_msg, data = validate_sidecar_request(payload, self.expected_token, allowed_roots=[root_dir])
        self.assertTrue(is_valid)
        self.assertEqual(data["worktree"], str(child_dir.resolve()))

    def test_invalid_sandbox_mode_rejected(self):
        """4. Modalità sandbox non previste vengono rifiutate."""
        payload = {
            "auth_token": self.expected_token,
            "profile": "sol",
            "worktree": str(self.worktree_dir),
            "prompt": "Test prompt",
            "sandbox": "danger-full-access"
        }
        is_valid, err_msg, _ = validate_sidecar_request(payload, self.expected_token, allowed_roots=[Path(self.temp_dir.name) / "worktrees"])
        self.assertFalse(is_valid)
        self.assertIn("Sandbox mode", err_msg)

    def test_valid_request_sanitized(self):
        """5. Payload conforme viene validato e normalizzato."""
        payload = {
            "auth_token": self.expected_token,
            "profile": "sol",
            "worktree": str(self.worktree_dir),
            "prompt": "Analizza il codice del repository",
            "sandbox": "read-only",
            "reasoning_effort": "low"
        }
        is_valid, err_msg, data = validate_sidecar_request(payload, self.expected_token, allowed_roots=[Path(self.temp_dir.name) / "worktrees"])
        self.assertTrue(is_valid)
        self.assertEqual(data["profile"], "sol")
        self.assertEqual(data["sandbox"], "read-only")
        self.assertEqual(data["reasoning_effort"], "low")


import uuid

class TestHostCodexSocketCommunication(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp_dir.name)
        self.worktree_dir = self.root_dir / "wt_child"
        self.worktree_dir.mkdir(parents=True, exist_ok=True)
        self.sock_path = Path("/tmp") / f"sc_{uuid.uuid4().hex[:8]}.sock"
        self.token = "test_token_xyz_123456789012345678"
        self.daemon = HostCodexDaemon(
            socket_path=self.sock_path,
            auth_token=self.token,
            allowed_roots=[self.root_dir]
        )

    def tearDown(self):
        self.daemon.running = False
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_socket_lifecycle_and_mock_execution(self):
        """6. Testa il ciclo di vita del socket Unix e la risposta del server."""
        # Avvia il server in un thread separato
        server_thread = threading.Thread(target=self.daemon.run, daemon=True)
        server_thread.start()

        # Attendi creazione socket
        import time
        for _ in range(50):
            if self.sock_path.exists():
                break
            time.sleep(0.05)

        self.assertTrue(self.sock_path.exists())
        self.assertTrue(self.sock_path.is_socket())

        # Invia richiesta valida con mock execution
        with patch.object(self.daemon, "execute_codex", return_value={"status": "SUCCESS", "stdout": "OK Mock", "code": 0}):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connected = False
            for _ in range(50):
                try:
                    client.connect(str(self.sock_path))
                    connected = True
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    time.sleep(0.05)
            self.assertTrue(connected, "Impossibile connettersi al socket sidecar")
            req = {
                "auth_token": self.token,
                "profile": "sol",
                "worktree": str(self.worktree_dir),
                "prompt": "Test",
                "sandbox": "read-only"
            }
            client.sendall((json.dumps(req) + "\n").encode("utf-8"))
            resp_raw = client.recv(4096).decode("utf-8")
            client.close()

            resp = json.loads(resp_raw)
            self.assertEqual(resp.get("status"), "SUCCESS")
            self.assertEqual(resp.get("stdout"), "OK Mock")

        self.daemon.running = False

    def test_single_job_lock_returns_busy_when_occupied(self):
        """7. Quando un job è già in esecuzione, le richieste concorrenti ricevono BUSY."""
        server_thread = threading.Thread(target=self.daemon.run, daemon=True)
        server_thread.start()

        # Simula il lock occupato da un job precedente
        self.daemon.job_lock.acquire()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.assertTrue(connect_with_retry(client, self.sock_path), "Impossibile connettersi al socket sidecar")
            req = {
                "auth_token": self.token,
                "profile": "sol",
                "worktree": str(self.worktree_dir),
                "prompt": "Test",
                "sandbox": "read-only"
            }
            client.sendall((json.dumps(req) + "\n").encode("utf-8"))
            resp_raw = client.recv(4096).decode("utf-8")
            client.close()

            resp = json.loads(resp_raw)
            self.assertEqual(resp.get("status"), "BUSY")
            self.assertIn("occupato", resp.get("error", ""))
        finally:
            self.daemon.job_lock.release()
            self.daemon.running = False

    def test_secure_audit_logging_does_not_log_full_prompt_or_token(self):
        """8. L'audit log registra solo hash e metadati, mai token o prompt completi."""
        with self.assertLogs("CodexHostDaemon", level="INFO") as cm:
            self.daemon.audit_log("TEST_EVENT", {"profile": "sol", "prompt_len": 42, "prompt_sha256": "abcdef1234567890"})
            log_output = " ".join(cm.output)
            self.assertIn("TEST_EVENT", log_output)
            self.assertIn("abcdef1234567890", log_output)
            self.assertNotIn("test_token_xyz", log_output)

    def test_uid_0_root_rejected_by_peer_check(self):
        """9. Connessioni da UID 0 (root) vengono esplicitamente rifiutate dal controllo peer credentials."""
        sock_path = Path("/tmp") / f"root_peer_{uuid.uuid4().hex[:8]}.sock"
        daemon = HostCodexDaemon(
            socket_path=sock_path,
            auth_token=self.token,
            allowed_roots=[Path(self.temp_dir.name)]
        )
        with patch("infrastructure.host_codex_socket_daemon.get_peer_credentials", return_value=(1234, 0, 0)):
            server_thread = threading.Thread(target=daemon.run, daemon=True)
            server_thread.start()

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.assertTrue(connect_with_retry(client, sock_path), "Impossibile connettersi al socket sidecar")
            try:
                client.sendall((json.dumps({"auth_token": self.token}) + "\n").encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            resp_raw = client.recv(4096).decode("utf-8")
            client.close()

            resp = json.loads(resp_raw)
            self.assertEqual(resp.get("status"), "ERROR")
            self.assertIn("UID 0", resp.get("error", ""))
            daemon.running = False
            if sock_path.exists():
                try:
                    sock_path.unlink()
                except Exception:
                    pass

    def test_output_cap_exceeded_kills_process_and_returns_structured_error(self):
        """9b. Quando stdout supera MAX_OUTPUT_BYTES, il processo figlio viene terminato e restituisce codice 137."""
        req = {
            "profile": "sol",
            "worktree": str(self.worktree_dir),
            "sandbox": "read-only",
            "reasoning_effort": "low",
            "prompt": "Test"
        }
        # Simula processo che emette più di MAX_OUTPUT_BYTES
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout.read.side_effect = [b"A" * (600 * 1024), b""]
            mock_proc.stderr.read.side_effect = [b""]
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            res = self.daemon.execute_codex(req)
            self.assertEqual(res["status"], "ERROR")
            self.assertEqual(res["code"], 137)
            self.assertIn("Limite di output superato", res["stderr"])
            mock_proc.kill.assert_called()

    def test_stderr_cap_exceeded_kills_process_and_returns_structured_error(self):
        """9c. Quando stderr supera MAX_OUTPUT_BYTES, il processo figlio viene terminato e restituisce codice 137."""
        req = {
            "profile": "sol",
            "worktree": str(self.worktree_dir),
            "sandbox": "read-only",
            "reasoning_effort": "low",
            "prompt": "Test"
        }
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout.read.side_effect = [b""]
            mock_proc.stderr.read.side_effect = [b"E" * (600 * 1024), b""]
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            res = self.daemon.execute_codex(req)
            self.assertEqual(res["status"], "ERROR")
            self.assertEqual(res["code"], 137)
            self.assertIn("Limite di output superato", res["stderr"])
            mock_proc.kill.assert_called()


class TestHostCodexClientAndOrchestratorIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sock_path = Path("/tmp") / f"cl_{uuid.uuid4().hex[:8]}.sock"
        self.worktree_path = Path(self.temp_dir.name) / "worktree"
        self.worktree_path.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_host_codex_client_send_request_success(self):
        """10. HostCodexClient invia richiesta e restituisce stdout su successo."""
        from infrastructure.host_codex_client import HostCodexClient

        # Crea un mock server Unix socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.sock_path))
        server.listen(1)

        def mock_server_response():
            conn, _ = server.accept()
            data = conn.recv(4096)
            conn.sendall(json.dumps({"status": "SUCCESS", "stdout": "Output dal sidecar", "code": 0}).encode("utf-8"))
            conn.close()

        threading.Thread(target=mock_server_response, daemon=True).start()

        client = HostCodexClient(socket_path=self.sock_path, auth_token="test_token_xyz_123456789012345678")
        out = client.send_request("sol", "Prompt", str(self.worktree_path), "read-only", "low")
        self.assertEqual(out, "Output dal sidecar")
        server.close()

    def test_orchestrator_call_codex_profile_uses_sidecar_when_flag_enabled(self):
        """11. call_codex_profile invoca HostCodexClient quando UFFICIO_HOST_CODEX_SIDECAR=1."""
        from orchestrator_core import MultiAgentRunner

        runner = MultiAgentRunner(workspace_path=self.worktree_path, mock_mode=False)

        with patch.dict(os.environ, {"UFFICIO_HOST_CODEX_SIDECAR": "1"}):
            with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", return_value=(True, '{"verdict": "pass", "ok": true}', {"status": "SUCCESS"})) as mock_gw:
                res = runner.call_codex_profile("sol", "Analizza codice", reasoning_effort="low")
                mock_gw.assert_called_once_with(
                    agent_role="sol",
                    prompt="Analizza codice",
                    worktree_path=str(self.worktree_path),
                    preset="standard",
                    reasoning_effort="low",
                    sandbox_mode="read-only",
                    phase="orchestrator_call",
                    run_id=runner.run_id,
                    metadata=None
                )
                self.assertIn('"ok": true', res)

    def test_orchestrator_call_codex_profile_no_cli_fallback_on_sidecar_error(self):
        """12. Quando UFFICIO_HOST_CODEX_SIDECAR=1 e il sidecar fallisce, è vietato ogni fallback alla CLI locale."""
        from orchestrator_core import MultiAgentRunner
        from infrastructure.host_codex_client import CodexSidecarError

        runner = MultiAgentRunner(workspace_path=self.worktree_path, mock_mode=False)

        with patch.dict(os.environ, {"UFFICIO_HOST_CODEX_SIDECAR": "1"}):
            with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", return_value=(False, "⚠️ Sidecar irraggiungibile", {"status": "ERROR"})):
                with patch.object(runner, "run_cmd") as mock_run_cmd:
                    res = runner.call_codex_profile("sol", "Analizza codice")
                    # run_cmd NON deve essere stato chiamato (nessun fallback)
                    mock_run_cmd.assert_not_called()
                    res_json = json.loads(res)
                    self.assertFalse(res_json["ok"])
                    self.assertEqual(res_json["status"], "ERROR")
                    self.assertIn("Sidecar irraggiungibile", res_json["error"])

    def test_client_token_file_missing_or_weak_fails_closed(self):
        """13. Se il file del token sidecar è assente o debole, il client applica fail-closed senza toccare il socket."""
        from infrastructure.host_codex_client import HostCodexClient, CodexSidecarError

        # File assente
        non_existent = Path(self.temp_dir.name) / "non_existent_token_file"
        client = HostCodexClient(socket_path=self.sock_path, token_file=non_existent)
        with patch.dict(os.environ, {"UFFICIO_SIDECAR_TOKEN_FILE": str(non_existent), "UFFICIO_SIDECAR_TOKEN": ""}):
            with self.assertRaises(CodexSidecarError) as cm:
                client.send_request("sol", "Prompt", str(self.worktree_path))
            self.assertIn("fail-closed", str(cm.exception))

        # File con token troppo debole (< 32 caratteri)
        weak_file = Path(self.temp_dir.name) / "weak_token_file"
        weak_file.write_text("short_token_123", encoding="utf-8")
        client_weak = HostCodexClient(socket_path=self.sock_path, token_file=weak_file)
        with patch.dict(os.environ, {"UFFICIO_SIDECAR_TOKEN_FILE": str(weak_file), "UFFICIO_SIDECAR_TOKEN": ""}):
            with self.assertRaises(CodexSidecarError) as cm:
                client_weak.send_request("sol", "Prompt", str(self.worktree_path))
            self.assertIn("fail-closed", str(cm.exception))

    def test_client_socket_response_too_large_raises_error(self):
        """14. Se il socket restituisce una risposta superiore a MAX_RESPONSE_BYTES, il client interrompe e fallisce."""
        from infrastructure.host_codex_client import HostCodexClient, CodexSidecarError

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.sock_path))
        server.listen(1)

        def mock_huge_response():
            conn, _ = server.accept()
            conn.recv(4096)
            # Invia oltre 512 KB
            conn.sendall(b"X" * (600 * 1024))
            conn.close()

        threading.Thread(target=mock_huge_response, daemon=True).start()

        client = HostCodexClient(socket_path=self.sock_path, auth_token="test_token_xyz_123456789012345678")
        with self.assertRaises(CodexSidecarError) as cm:
            client.send_request("sol", "Prompt", str(self.worktree_path))
        self.assertIn("dimensione massima consentita", str(cm.exception))
        server.close()

    def test_daemon_socket_request_too_large_rejects_immediately(self):
        """15. Se il daemon riceve una richiesta superiore a MAX_OUTPUT_BYTES, rifiuta immediatamente senza json parziali."""
        daemon_sock = Path("/tmp") / f"dm_{uuid.uuid4().hex[:8]}.sock"
        daemon = HostCodexDaemon(
            socket_path=daemon_sock,
            auth_token="test_token_xyz_123456789012345678",
            allowed_roots=[Path(self.temp_dir.name)]
        )
        server_thread = threading.Thread(target=daemon.run, daemon=True)
        server_thread.start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connected = False
        for _ in range(50):
            try:
                client.connect(str(daemon_sock))
                connected = True
                break
            except Exception:
                time.sleep(0.05)
        self.assertTrue(connected, "Impossibile connettersi al socket del daemon")
        # Invia payload di oltre 512 KB in blocchi
        huge_payload = b"{\"too_large\":\"" + (b"A" * (550 * 1024)) + b"\"}\n"
        try:
            for offset in range(0, len(huge_payload), 4096):
                client.sendall(huge_payload[offset:offset+4096])
        except (BrokenPipeError, ConnectionResetError):
            pass

        resp_raw = client.recv(4096).decode("utf-8")
        client.close()

        resp = json.loads(resp_raw)
        self.assertEqual(resp.get("status"), "ERROR")
        self.assertIn("dimensione massima consentita", resp.get("error", ""))
        daemon.running = False
        if daemon_sock.exists():
            try:
                daemon_sock.unlink()
            except Exception:
                pass

    def test_agent_gateway_reasoning_effort_rules(self):
        """16. AgentGateway applica low default per Sol, high per critical ed esclude reasoning effort per Luna."""
        from infrastructure.agent_gateway import AgentGateway

        resolved_wt = str(self.worktree_path.resolve())
        with patch.dict(os.environ, {"UFFICIO_HOST_CODEX_SIDECAR": "1"}):
            with patch("infrastructure.host_codex_client.HostCodexClient.send_request", return_value="OK") as mock_send:
                # Sol standard -> low
                AgentGateway.execute_agent_call("sol", "Prompt", worktree_path=self.worktree_path, preset="standard")
                mock_send.assert_called_with(
                    profile="sol",
                    prompt="Prompt",
                    worktree=resolved_wt,
                    sandbox="read-only",
                    reasoning_effort="low"
                )

                # Sol critical -> high
                AgentGateway.execute_agent_call("director", "Prompt", worktree_path=self.worktree_path, preset="critical")
                mock_send.assert_called_with(
                    profile="sol",
                    prompt="Prompt",
                    worktree=resolved_wt,
                    sandbox="read-only",
                    reasoning_effort="high"
                )

                # Luna -> None (nessun override)
                AgentGateway.execute_agent_call("luna", "Prompt", worktree_path=self.worktree_path, preset="critical")
                mock_send.assert_called_with(
                    profile="luna",
                    prompt="Prompt",
                    worktree=resolved_wt,
                    sandbox="read-only",
                    reasoning_effort=None
                )

    def test_brainstorm_manager_routes_sol_and_luna_via_agent_gateway_when_flag_on(self):
        """17. BrainstormManager instrada Sol e Luna via AgentGateway quando UFFICIO_HOST_CODEX_SIDECAR=1 senza chiamare subprocess."""
        from brainstorm_manager import BrainstormManager

        bm = BrainstormManager(state_dir=Path(self.temp_dir.name))

        with patch.dict(os.environ, {"UFFICIO_HOST_CODEX_SIDECAR": "1"}):
            with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", return_value=(True, "Risposta Sol via Sidecar", {"status": "SUCCESS"})) as mock_gw:
                with patch("subprocess.run") as mock_subproc:
                    res = bm._query_agent_llm(
                        agent="sol",
                        messages=[{"role": "user", "content": "Ciao Sol"}],
                        preset="standard"
                    )
                    self.assertEqual(res, "Risposta Sol via Sidecar")
                    mock_gw.assert_called_once()
                    # Nessun subprocess deve essere stato chiamato
                    mock_subproc.assert_not_called()

    def test_brainstorm_manager_legacy_compatibility_when_flag_off(self):
        """18. Quando UFFICIO_HOST_CODEX_SIDECAR=0, BrainstormManager preserva la modalità legacy senza bloccare il flusso."""
        from brainstorm_manager import BrainstormManager

        bm = BrainstormManager(state_dir=Path(self.temp_dir.name))

        with patch.dict(os.environ, {"UFFICIO_HOST_CODEX_SIDECAR": "0"}):
            with patch("account_manager.CodexAccountManager.get_account_for_role", return_value=None):
                res = bm._query_agent_llm(
                    agent="sol",
                    messages=[{"role": "user", "content": "Ciao Sol"}],
                    preset="standard"
                )
                self.assertIn("Quota settimanale", res)

    def test_client_fails_closed_even_with_valid_env_token_if_file_missing(self):
        """13b. Anche se UFFICIO_SIDECAR_TOKEN è presente nell'ambiente, se UFFICIO_SIDECAR_TOKEN_FILE è assente il client fallisce a monte senza socket."""
        from infrastructure.host_codex_client import HostCodexClient, CodexSidecarError

        non_existent_file = Path(self.temp_dir.name) / "missing_secret_token_file"
        with patch.dict(os.environ, {
            "UFFICIO_SIDECAR_TOKEN_FILE": str(non_existent_file),
            "UFFICIO_SIDECAR_TOKEN": "extremely_valid_token_with_more_than_32_characters_12345"
        }):
            client = HostCodexClient(socket_path=self.sock_path)
            with self.assertRaises(CodexSidecarError) as cm:
                client.send_request("sol", "Prompt", str(self.worktree_path))
            self.assertIn("fail-closed", str(cm.exception))

    def test_brainstorm_chat_workspace_uses_subfolder_under_workspaces(self):
        """19. BrainstormManager risolve ed utilizza una sottodirectory effettiva sotto workspaces/ (mai la root)."""
        from brainstorm_manager import BrainstormManager

        bm = BrainstormManager(state_dir=Path(self.temp_dir.name))
        with patch.dict(os.environ, {"UFFICIO_HOME": self.temp_dir.name}):
            ws_path = bm._resolve_chat_workspace("test_session_123")
            self.assertTrue(ws_path.exists())
            self.assertEqual(ws_path.name, "chat_test_session_123")
            self.assertEqual(ws_path.parent.name, "workspaces")
            self.assertNotEqual(ws_path, ws_path.parent)


if __name__ == "__main__":
    unittest.main()
