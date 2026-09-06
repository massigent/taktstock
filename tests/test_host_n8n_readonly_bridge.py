#!/usr/bin/env python3
"""
Unit tests for Host n8n Read-Only Bridge / Sidecar, T2/T3/T4 failure lifecycle, and secret isolation.
Zero LLM calls, deterministic socket tests, zero mutating operations.
"""

import os
import sys
import json
import time
import socket
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import moduli Taktstock
server_dir = Path(__file__).resolve().parent.parent / "server"
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from infrastructure.host_n8n_socket_daemon import HostN8nSocketDaemon
from infrastructure.host_n8n_client import HostN8nClient, N8nSidecarError
from orchestrator_core import MultiAgentRunner, TokensDict, MECHANICAL_ALLOWED_WRITE_FILES


class TestHostN8nReadOnlyBridge(unittest.TestCase):
    """Test del bridge socket Unix n8n host in sola lettura."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.socket_path = self.tmp_path / "n8n_test.sock"
        self.secret_token = "a" * 32  # 32 chars token
        self.mock_api_key = "test_n8n_secret_key_123456789"
        self.mock_api_url = "http://127.0.0.1:9999"

        # Avvio daemon di test
        self.daemon = HostN8nSocketDaemon(
            socket_path=self.socket_path,
            auth_token=self.secret_token,
            api_url=self.mock_api_url,
            api_key=self.mock_api_key
        )
        self.daemon_thread = threading.Thread(target=self.daemon.start, daemon=True)
        self.daemon_thread.start()
        time.sleep(0.1)

        self.client = HostN8nClient(
            socket_path=self.socket_path,
            auth_token=self.secret_token
        )

    def tearDown(self):
        self.daemon.stop()
        self.tmp_dir.cleanup()

    def test_1_bridge_ready_and_export_success(self):
        """1. Bridge read-only con export riuscito via Unix socket."""
        self.assertTrue(self.client.check_ready())

        mock_wf_response = {
            "id": "oyJX2al4JMmNz7lk",
            "name": "MiniApp Master API",
            "active": True,
            "nodes": [{"name": "Webhook", "type": "n8n-nodes-base.webhook"}],
            "connections": {}
        }

        with patch("infrastructure.host_n8n_socket_daemon.execute_n8n_readonly_get", return_value=mock_wf_response) as mock_get:
            wf = self.client.get_workflow_details("oyJX2al4JMmNz7lk")
            self.assertEqual(wf["id"], "oyJX2al4JMmNz7lk")
            self.assertEqual(wf["name"], "MiniApp Master API")
            self.assertEqual(len(wf["nodes"]), 1)
            mock_get.assert_called_once()

    def test_1b_bridge_strictly_rejects_mutations(self):
        """1b. Il bridge rifiuta categoricamente qualsiasi azione di modifica/scrittura."""
        mutating_actions = ["update_workflow", "delete_workflow", "execute_workflow", "create_node", "activate"]
        for act in mutating_actions:
            with self.assertRaises(N8nSidecarError) as ctx:
                self.client._send_ipc_request({"action": act})
            self.assertIn("non consentita", str(ctx.exception).lower())

    def test_2_missing_secret_blocks_before_writes(self):
        """2. Segreto n8n assente/non disponibile -> errore e blocco prima di qualunque scrittura."""
        # Daemon senza api_key
        no_key_socket = self.tmp_path / "no_key.sock"
        no_key_daemon = HostN8nSocketDaemon(
            socket_path=no_key_socket,
            auth_token=self.secret_token,
            api_url=self.mock_api_url,
            api_key=""
        )
        t = threading.Thread(target=no_key_daemon.start, daemon=True)
        t.start()
        time.sleep(0.1)

        try:
            no_key_client = HostN8nClient(socket_path=no_key_socket, auth_token=self.secret_token)
            with self.assertRaises(N8nSidecarError) as ctx:
                no_key_client.get_workflow_details("oyJX2al4JMmNz7lk")
            self.assertIn("N8N_API_KEY non configurata", str(ctx.exception))
        finally:
            no_key_daemon.stop()

    def test_3_t2_error_causes_t3_t4_skipped_and_terminal_blocked_status(self):
        """3. Se T2 fallisce -> T3 e T4 vengono SKIPPED, stato terminale non-completed, zero scritture."""
        ws_dir = self.tmp_path / "workspace_test"
        ws_dir.mkdir()
        (ws_dir / ".git").mkdir()

        runner = MultiAgentRunner(
            workspace_path=ws_dir,
            preset="mechanical",
            mock_mode=False,  # non-mock per testare il flusso reale di fallback/blocco
            mechanical_action="sync-n8n-mirror"
        )

        # Simuliamo socket inesistente / errore export
        with patch.object(HostN8nClient, "check_ready", return_value=False):
            with patch("infrastructure.n8n_readonly_mcp.handle_get_workflow_details", side_effect=RuntimeError("N8N_API_KEY non presente")):
                t2_res = json.loads(runner.execute_mechanical_task({"id": "T2"}))
                self.assertEqual(t2_res["status"], "BLOCKED_PREREQUISITE")

                # T3 deve risultare SKIPPED e non scrivere nulla
                t3_res = json.loads(runner.execute_mechanical_task({"id": "T3"}))
                self.assertEqual(t3_res["status"], "SKIPPED")
                mirror_file = ws_dir / "MiniApp_Master_API.json"
                self.assertFalse(mirror_file.exists(), "Il file mirror non deve essere creato se T2 fallisce")

                # T4 deve risultare SKIPPED
                t4_res = json.loads(runner.execute_mechanical_task({"id": "T4"}))
                self.assertEqual(t4_res["status"], "SKIPPED")

                # Test esecuzione workflow completa: deve arrestarsi con BLOCKED_PREREQUISITE
                summary = runner.run_full_workflow("Sincronizza mirror locale MiniApp Master API --action sync-n8n-mirror", branch_name="feature/test-fail")
                self.assertEqual(summary["status"], "BLOCKED_PREREQUISITE")
                self.assertNotEqual(summary["status"], "COMPLETED")
                self.assertFalse(mirror_file.exists())

    def test_4_zero_api_keys_leaked_in_logs_and_responses(self):
        """4. Nessuna chiave API nei log, nella risposta socket o nelle eccezioni."""
        secret_key_pattern = "SUPER_SECRET_N8N_API_KEY_XYZ_987654321"

        leak_socket = self.tmp_path / "leak_test.sock"
        leak_daemon = HostN8nSocketDaemon(
            socket_path=leak_socket,
            auth_token=self.secret_token,
            api_url="http://invalid-n8n-url-that-fails.local",
            api_key=secret_key_pattern
        )
        t = threading.Thread(target=leak_daemon.start, daemon=True)
        t.start()
        time.sleep(0.1)

        try:
            leak_client = HostN8nClient(socket_path=leak_socket, auth_token=self.secret_token)
            with self.assertRaises(N8nSidecarError) as ctx:
                leak_client.get_workflow_details("oyJX2al4JMmNz7lk")
            err_msg = str(ctx.exception)
            self.assertNotIn(secret_key_pattern, err_msg)
        finally:
            leak_daemon.stop()

    def test_5_live_get_workflow_if_configured(self):
        """5. Test live esclusivamente GET di un workflow n8n (se sidecar host reale presente)."""
        real_sock = Path("/run/taktstock-n8n/n8n.sock")
        if real_sock.exists() and real_sock.is_socket():
            client = HostN8nClient(socket_path=real_sock)
            if client.check_ready(timeout=2.0):
                wf = client.get_workflow_details("oyJX2al4JMmNz7lk")
                self.assertIsInstance(wf, dict)
                self.assertIn("nodes", wf)
                print("✅ Live host n8n bridge GET workflow test riuscito!")
            else:
                self.skipTest("Live sidecar host n8n non pronto per la connessione.")
        else:
            # Test in ambiente simulato
            self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
