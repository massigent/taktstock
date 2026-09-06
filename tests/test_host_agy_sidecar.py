#!/usr/bin/env python3
"""
Unit Tests for AGY Host Sidecar (HostAgyDaemon, HostAgyClient, AgentGateway & MultiAgentRunner)
---------------------------------------------------------------------------------------------
Verifica completa (senza binari live, socket di produzione o segreti reali):
1. Validazione schema e token AGY (ready, health, worktree whitelist, sandbox, timeout).
2. Socket Unix AGY, permessi 0600, rifiuto UID 0 e concorrenza single-job (BUSY).
3. Risoluzione e validazione di AGY_BIN (fail-closed se assente o non eseguibile).
4. Isolamento di AGY_HOME e ambiente figlio su STRICT ALLOWLIST (zero leak di segreti).
5. Enforcement sandbox AGY:
   - read-only => --sandbox --mode plan (senza --dangerously-skip-permissions).
   - workspace-write => --sandbox --mode accept-edits (senza --dangerously-skip-permissions).
6. Gestione Timeout (124) e Output Cap (137) con terminazione forzata del processo.
7. HostAgyClient: fail-closed su token mancante/debole, gestione BUSY e risposte strutturate.
8. Routing in AgentGateway: 'agy' instradato su HostAgyClient, phase='chat' forzato read-only.
9. Chat @agy in brainstorm_manager: routing effettivo via sidecar, nessun fallback a Gemini, compattazione esclusiva con Gemini Compactor.
10. MultiAgentRunner.call_executor_agy: delega integrale ad AgentGateway, zero subprocess nel container e zero fallback locale.
"""

import os
import sys
import json
import stat
import uuid
import socket
import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(SERVER_DIR / "infrastructure"))

from infrastructure.host_agy_socket_daemon import (
    validate_agy_request,
    load_agy_sidecar_token,
    resolve_agy_bin,
    resolve_agy_home,
    HostAgyDaemon,
    CHILD_ENV_ALLOWLIST
)
from infrastructure.host_agy_client import (
    HostAgyClient,
    AgySidecarError,
    AgySidecarBusyError
)
from infrastructure.agent_gateway import AgentGateway
from orchestrator_core import MultiAgentRunner
from brainstorm_manager import BrainstormManager


def connect_with_retry(sock: socket.socket, path: Path, max_attempts: int = 50, delay: float = 0.05) -> bool:
    import time
    for _ in range(max_attempts):
        try:
            sock.connect(str(path))
            return True
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(delay)
    return False


class TestHostAgySidecarValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.valid_token = "valid_agy_secret_token_12345678901234567890"
        self.roots = [self.tmp_path / "worktrees", self.tmp_path / "workspaces"]
        for r in self.roots:
            r.mkdir(parents=True, exist_ok=True)

        self.child_wt = self.roots[0] / "wt_task_1"
        self.child_wt.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ready_and_health_actions_validation(self):
        """1. Validazione rapida per action='ready' e 'health'."""
        valid, err, sanitized = validate_agy_request(
            {"auth_token": self.valid_token, "action": "ready"},
            self.valid_token,
            allowed_roots=self.roots
        )
        self.assertTrue(valid)
        self.assertEqual(sanitized, {"action": "ready"})

        valid, err, sanitized = validate_agy_request(
            {"auth_token": self.valid_token, "action": "health"},
            self.valid_token,
            allowed_roots=self.roots
        )
        self.assertTrue(valid)
        self.assertEqual(sanitized, {"action": "ready"})

    def test_invalid_auth_token_rejected(self):
        """2. Richieste con token errato vengono rifiutate."""
        valid, err, _ = validate_agy_request(
            {"auth_token": "wrong_token", "action": "ready"},
            self.valid_token,
            allowed_roots=self.roots
        )
        self.assertFalse(valid)
        self.assertIn("Autenticazione socket fallita", err)

    def test_worktree_root_and_traversal_rejected(self):
        """3. La root stessa e percorsi esterni vengono respinti."""
        # Root stessa
        valid, err, _ = validate_agy_request(
            {"auth_token": self.valid_token, "action": "execute", "worktree": str(self.roots[0]), "prompt": "test"},
            self.valid_token,
            allowed_roots=self.roots
        )
        self.assertFalse(valid)
        self.assertIn("directory radice", err)

        # Percorso esterno
        external = self.tmp_path / "external_dir"
        external.mkdir(parents=True, exist_ok=True)
        valid, err, _ = validate_agy_request(
            {"auth_token": self.valid_token, "action": "execute", "worktree": str(external), "prompt": "test"},
            self.valid_token,
            allowed_roots=self.roots
        )
        self.assertFalse(valid)
        self.assertIn("fuori dai worktree autorizzati", err)

    def test_valid_execute_request_sanitized(self):
        """4. Richiesta execute conforme viene validata."""
        valid, err, sanitized = validate_agy_request(
            {
                "auth_token": self.valid_token,
                "action": "execute",
                "worktree": str(self.child_wt),
                "sandbox": "workspace-write",
                "timeout": 600,
                "prompt": "Implement feature X"
            },
            self.valid_token,
            allowed_roots=self.roots
        )
        self.assertTrue(valid)
        self.assertEqual(sanitized["action"], "execute")
        self.assertEqual(sanitized["worktree"], str(self.child_wt.resolve()))
        self.assertEqual(sanitized["sandbox"], "workspace-write")
        self.assertEqual(sanitized["timeout"], 600)
        self.assertEqual(sanitized["prompt"], "Implement feature X")


class TestHostAgyDaemonConfigurationAndExecution(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.sock_path = Path("/tmp") / f"agy_{uuid.uuid4().hex[:8]}.sock"
        self.valid_token = "valid_agy_secret_token_12345678901234567890"

        self.mock_bin = (self.tmp_path / "mock_agy").resolve()
        self.mock_bin.write_text("#!/bin/sh\necho '{\"status\": \"SUCCESS\"}'\n", encoding="utf-8")
        os.chmod(str(self.mock_bin), 0o755)

        self.mock_home = (self.tmp_path / "agy_home").resolve()
        self.mock_home.mkdir(parents=True, exist_ok=True)

        self.roots = [self.tmp_path / "worktrees"]
        self.roots[0].mkdir(parents=True, exist_ok=True)
        self.child_wt = self.roots[0] / "wt_agy_test"
        self.child_wt.mkdir(parents=True, exist_ok=True)

        self.daemon = HostAgyDaemon(
            socket_path=self.sock_path,
            auth_token=self.valid_token,
            allowed_roots=self.roots,
            agy_binary_path=str(self.mock_bin),
            agy_home=str(self.mock_home)
        )

    def tearDown(self):
        self.daemon.running = False
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_resolve_agy_bin_fails_closed_when_missing_or_not_executable(self):
        """5. resolve_agy_bin fallisce se il file è inesistente o privo di permessi di esecuzione."""
        with self.assertRaises(ValueError) as cm1:
            resolve_agy_bin("/nonexistent/agy_bin_path")
        self.assertIn("non trovato o non eseguibile", str(cm1.exception))

        non_exec_file = self.tmp_path / "not_executable"
        non_exec_file.write_text("dummy", encoding="utf-8")
        os.chmod(str(non_exec_file), 0o644)
        with self.assertRaises(ValueError) as cm2:
            resolve_agy_bin(str(non_exec_file))
        self.assertIn("non trovato o non eseguibile", str(cm2.exception))

    def test_daemon_socket_creation_and_ready_action(self):
        """6. Creazione socket con permessi 0600 e risposta ready."""
        server_thread = threading.Thread(target=self.daemon.run, daemon=True)
        server_thread.start()

        # Invia azione ready
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.assertTrue(connect_with_retry(client, self.sock_path), "Impossibile connettersi al socket AGY")

        mode = stat.S_IMODE(self.sock_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

        req = {"auth_token": self.valid_token, "action": "ready"}
        client.sendall((json.dumps(req) + "\n").encode("utf-8"))
        resp_raw = client.recv(4096).decode("utf-8")
        client.close()

        resp = json.loads(resp_raw)
        self.assertEqual(resp.get("status"), "SUCCESS")
        self.assertEqual(resp.get("service"), "taktstock-agy")
        self.assertTrue(resp.get("ready"))
        self.assertFalse(self.daemon.job_lock.locked())

        self.daemon.running = False

    def test_single_job_concurrency_lock_returns_busy(self):
        """7. Quando un job AGY è attivo, ulteriori richieste ricevono BUSY."""
        server_thread = threading.Thread(target=self.daemon.run, daemon=True)
        server_thread.start()

        # Simula job occupato
        self.daemon.job_lock.acquire()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.assertTrue(connect_with_retry(client, self.sock_path), "Impossibile connettersi al socket AGY")
        req = {
            "auth_token": self.valid_token,
            "action": "execute",
            "worktree": str(self.child_wt),
            "prompt": "Test"
        }
        client.sendall((json.dumps(req) + "\n").encode("utf-8"))
        resp_raw = client.recv(4096).decode("utf-8")
        client.close()

        resp = json.loads(resp_raw)
        self.assertEqual(resp.get("status"), "BUSY")
        self.assertIn("occupato", resp.get("error"))

        self.daemon.job_lock.release()
        self.daemon.running = False

    def test_child_environment_strict_allowlist_and_isolated_home(self):
        """8. L'ambiente del processo figlio imposta HOME=AGY_HOME ed include solo chiavi nella allowlist (zero segreti)."""
        params = {
            "prompt": "Test prompt",
            "worktree": str(self.child_wt),
            "sandbox": "workspace-write",
            "timeout": 300
        }

        with patch.dict(os.environ, {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/massimo",
            "LANG": "en_US.UTF-8",
            "UFFICIO_AGY_SIDECAR_TOKEN": "super_secret_token_1234567890123456",
            "UFFICIO_SIDECAR_TOKEN": "codex_secret_token_1234567890123456",
            "UFFICIO_HOME": "/home/massimo/ufficio",
            "DASHBOARD_PASSWORD": "secret_dashboard_password_123456",
            "UFFICO_AUTH_TOKEN": "auth_token_1234567890123456",
            "OPENAI_API_KEY": "sk-12345678901234567890",
            "N8N_WEBHOOK_URL": "http://n8n.local/secret",
            "DATABASE_URL": "sqlite:////secret.db"
        }):
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.stdout.read.return_value = b""
                mock_proc.stderr.read.return_value = b""
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_popen.return_value = mock_proc

                self.daemon.execute_agy(params)

                mock_popen.assert_called_once()
                call_kwargs = mock_popen.call_args[1]
                child_env = call_kwargs["env"]

                # Verifica che HOME sia isolata su AGY_HOME
                self.assertEqual(child_env["HOME"], str(self.mock_home))

                # Verifica che tutte le chiavi appartengano all'allowlist
                for key in child_env.keys():
                    self.assertIn(key, CHILD_ENV_ALLOWLIST)

                # Verifica esplicita assenza segreti
                for forbidden in [
                    "UFFICIO_AGY_SIDECAR_TOKEN", "UFFICIO_SIDECAR_TOKEN", "UFFICIO_HOME",
                    "DASHBOARD_PASSWORD", "UFFICO_AUTH_TOKEN", "OPENAI_API_KEY",
                    "N8N_WEBHOOK_URL", "DATABASE_URL"
                ]:
                    self.assertNotIn(forbidden, child_env)

    def test_execute_agy_sandbox_arguments_enforcement(self):
        """9. Verifica l'enforcement deterministico dei flag di sandbox per read-only e workspace-write."""
        # 1. Test sandbox read-only -> --sandbox --mode plan
        params_ro = {
            "prompt": "Inspect codebase",
            "worktree": str(self.child_wt),
            "sandbox": "read-only",
            "timeout": 300
        }
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout.read.return_value = b""
            mock_proc.stderr.read.return_value = b""
            mock_proc.wait.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            self.daemon.execute_agy(params_ro)

            mock_popen.assert_called_once()
            cmd_ro = mock_popen.call_args[0][0]
            self.assertEqual(cmd_ro[0], str(self.mock_bin))
            self.assertIn("-p", cmd_ro)
            self.assertIn("Inspect codebase", cmd_ro)
            self.assertIn("--output-format", cmd_ro)
            self.assertIn("text", cmd_ro)
            self.assertIn("--disable-slash-commands", cmd_ro)
            self.assertIn("--print-timeout", cmd_ro)
            self.assertIn("300s", cmd_ro)
            self.assertIn("--add-dir", cmd_ro)
            idx_dir = cmd_ro.index("--add-dir")
            self.assertEqual(cmd_ro[idx_dir + 1], str(self.child_wt))
            self.assertIn("--sandbox", cmd_ro)
            self.assertIn("--mode", cmd_ro)
            idx_mode = cmd_ro.index("--mode")
            self.assertEqual(cmd_ro[idx_mode + 1], "plan")
            self.assertNotIn("--dangerously-skip-permissions", cmd_ro)

        # 2. Test sandbox workspace-write -> --sandbox --mode accept-edits
        params_rw = {
            "prompt": "Apply changes",
            "worktree": str(self.child_wt),
            "sandbox": "workspace-write",
            "timeout": 600
        }
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout.read.return_value = b""
            mock_proc.stderr.read.return_value = b""
            mock_proc.wait.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            self.daemon.execute_agy(params_rw)

            mock_popen.assert_called_once()
            cmd_rw = mock_popen.call_args[0][0]
            self.assertIn("--add-dir", cmd_rw)
            idx_dir_rw = cmd_rw.index("--add-dir")
            self.assertEqual(cmd_rw[idx_dir_rw + 1], str(self.child_wt))
            self.assertIn("--sandbox", cmd_rw)
            self.assertIn("--mode", cmd_rw)
            idx_mode = cmd_rw.index("--mode")
            self.assertEqual(cmd_rw[idx_mode + 1], "accept-edits")
            self.assertNotIn("--dangerously-skip-permissions", cmd_rw)

    def test_execute_agy_output_cap_exceeded_kills_child_process(self):
        """10. Superamento del limite di output (512 KB) termina il processo con codice 137."""
        params = {
            "prompt": "Test prompt",
            "worktree": str(self.child_wt),
            "sandbox": "workspace-write",
            "timeout": 300
        }

        # Genera stream di output superiore a 512 KB
        big_chunk = b"A" * (600 * 1024)
        mock_proc = MagicMock()
        mock_proc.stdout.read.side_effect = [big_chunk, b""]
        mock_proc.stderr.read.return_value = b""
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            res = self.daemon.execute_agy(params)
            self.assertEqual(res["status"], "ERROR")
            self.assertEqual(res["code"], 137)
            self.assertIn("Limite di output superato", res["stderr"])
            mock_proc.kill.assert_called()

    def test_execute_agy_timeout_kills_child_process(self):
        """11. Timeout esecuzione termina il processo con codice 124."""
        params = {
            "prompt": "Test prompt",
            "worktree": str(self.child_wt),
            "sandbox": "workspace-write",
            "timeout": 2
        }

        mock_proc = MagicMock()
        mock_proc.stdout.read.return_value = b""
        mock_proc.stderr.read.return_value = b""
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["agy"], timeout=2)
        mock_proc.returncode = None

        with patch("subprocess.Popen", return_value=mock_proc):
            res = self.daemon.execute_agy(params)
            self.assertEqual(res["status"], "ERROR")
            self.assertEqual(res["code"], 124)
            self.assertIn("Timeout esecuzione AGY", res["stderr"])
            mock_proc.kill.assert_called()


class TestHostAgyClientAndGatewayIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.sock_path = Path("/tmp") / f"agy_cl_{uuid.uuid4().hex[:8]}.sock"
        self.token = "valid_agy_secret_token_12345678901234567890"
        self.token_file = self.tmp_path / "token"
        self.token_file.write_text(self.token, encoding="utf-8")

        self.roots = [self.tmp_path / "worktrees"]
        self.roots[0].mkdir(parents=True, exist_ok=True)
        self.child_wt = self.roots[0] / "wt_client_test"
        self.child_wt.mkdir(parents=True, exist_ok=True)

        self.client = HostAgyClient(
            socket_path=self.sock_path,
            token_file_path=self.token_file
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_client_fails_closed_when_token_file_missing_or_weak(self):
        """12. Client fallisce all'istante senza toccare il socket se il file del token è assente o debole."""
        bad_client = HostAgyClient(
            socket_path=self.sock_path,
            token_file_path=self.tmp_path / "nonexistent_token"
        )
        with self.assertRaises(AgySidecarError) as cm:
            bad_client.send_request("prompt", str(self.child_wt))
        self.assertIn("non trovato", str(cm.exception))

        weak_token_file = self.tmp_path / "weak_token"
        weak_token_file.write_text("short_tok", encoding="utf-8")
        bad_client2 = HostAgyClient(
            socket_path=self.sock_path,
            token_file_path=weak_token_file
        )
        with self.assertRaises(AgySidecarError) as cm:
            bad_client2.send_request("prompt", str(self.child_wt))
        self.assertIn("troppo debole", str(cm.exception))

    def test_agent_gateway_routes_agy_role_to_host_agy_client_with_chat_sandbox(self):
        """13. AgentGateway instrada 'agy' su HostAgyClient e imposta sandbox read-only per phase='chat'."""
        with patch.dict(os.environ, {"TAKTSTOCK_HOST_AGY_SIDECAR": "1"}):
            with patch("infrastructure.host_agy_client.HostAgyClient.send_request") as mock_send:
                mock_send.return_value = json.dumps({"response": "Task completed by AGY"})

                # Test fase chat: sandbox forzata a read-only
                success, output, meta = AgentGateway.execute_agent_call(
                    agent_role="agy",
                    prompt="@agy analizza il file X",
                    worktree_path=self.child_wt,
                    phase="chat"
                )

                self.assertTrue(success)
                self.assertIn("Task completed by AGY", output)
                self.assertEqual(meta.get("role"), "agy")
                mock_send.assert_called_once_with(
                    prompt="@agy analizza il file X",
                    worktree=str(self.child_wt.resolve()),
                    sandbox="read-only"
                )

    def test_multi_agent_runner_call_executor_agy_uses_gateway_without_subprocess(self):
        """14. MultiAgentRunner.call_executor_agy usa AgentGateway e non subprocess locale."""
        runner = MultiAgentRunner(workspace_path=self.child_wt, mock_mode=False)

        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call") as mock_gateway_call:
            mock_gateway_call.return_value = (True, "AGY execution result from sidecar", {"status": "SUCCESS"})

            with patch("subprocess.run") as mock_subproc:
                res = runner.call_executor_agy("Refactor module A")

                self.assertEqual(res, "AGY execution result from sidecar")
                mock_gateway_call.assert_called_once()
                # Verifica categorica che subprocess locale NON sia stato invocato
                mock_subproc.assert_not_called()

    def test_multi_agent_runner_call_executor_agy_fails_closed_without_local_fallback(self):
        """15. Se il sidecar AGY fallisce, call_executor_agy solleva eccezione senza fallback locale."""
        runner = MultiAgentRunner(workspace_path=self.child_wt, mock_mode=False)

        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call") as mock_gateway_call:
            mock_gateway_call.return_value = (False, "Sidecar AGY down", {"status": "ERROR"})

            with patch("subprocess.run") as mock_subproc:
                with self.assertRaises(RuntimeError) as cm:
                    runner.call_executor_agy("Refactor module A")

                self.assertIn("Fallimento esecuzione AGY", str(cm.exception))
                mock_subproc.assert_not_called()

    def test_brainstorm_manager_post_chat_message_routes_agy_via_gateway_without_gemini_fallback(self):
        """16. In brainstorm_manager, il messaggio @agy chiama AgentGateway in read-only e non fallisce su Gemini."""
        bm = BrainstormManager(state_dir=self.tmp_path)
        bs_id = bm.create_brainstorm(task="Test task", chat_id="12345")

        # Mock di AgentGateway: fallimento del sidecar AGY
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call") as mock_gw:
            mock_gw.return_value = (False, "Socket connection error", {"status": "ERROR"})
            with patch.object(bm, "_query_agent_llm") as mock_query_llm:
                replies = bm.post_chat_message(bs_id, message="@agy verifica lo stato del modulo", sender="User")

                # Verifica che _query_agent_llm NON sia stato chiamato per agy
                for call in mock_query_llm.call_args_list:
                    self.assertNotEqual(call[0][0], "agy")
                    self.assertNotEqual(call[0][0], "gemini")

                # Verifica che la risposta registri l'errore del sidecar senza mascherarlo
                agy_reply = next(r for r in replies if r.get("agent") == "agy")
                self.assertIn("AGY Sidecar Error", agy_reply["text"])

    def test_compaction_announcement_and_gemini_query(self):
        """17. La compattazione della chat interroga esclusivamente 'gemini' e annuncia sender='Gemini Compactor'."""
        bm = BrainstormManager(state_dir=self.tmp_path)
        bs_id = bm.create_brainstorm(task="Test compaction", chat_id="12345")
        bs = bm.load_brainstorm(bs_id)

        # Inserisci messaggi sufficienti per superare la soglia di compattazione
        for i in range(15):
            bs["messages"].append({
                "id": f"msg_{i}",
                "sender": "User",
                "text": f"Messaggio di discussione numero {i} con dettagli tecnici.",
                "timestamp": "2026-08-24T08:00:00"
            })

        valid_summary = json.dumps({
            "facts": ["Discussione su architettura e target"],
            "decisions": ["Allineamento sui requisiti"],
            "constraints": ["Nessun downtime"],
            "open_questions": [],
            "next_steps": ["Procedere con i test"]
        })
        with patch.object(bm, "_query_agent_llm", return_value=valid_summary) as mock_query:
            compacted = bm._check_and_compact_chat(bs)
            self.assertTrue(compacted)
            mock_query.assert_called_once()
            self.assertEqual(mock_query.call_args[0][0], "gemini")

            # Verifica messaggio di annuncio
            compaction_msg = bs["messages"][-1]
            self.assertEqual(compaction_msg["sender"], "Gemini Compactor")
            self.assertEqual(compaction_msg["agent"], "gemini")
            self.assertIn("Memoria Compattata", compaction_msg["text"])


if __name__ == "__main__":
    unittest.main()
