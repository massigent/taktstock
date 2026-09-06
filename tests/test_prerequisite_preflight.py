#!/usr/bin/env python3
"""
Unit tests for Taktstock Prerequisite Preflight & Telemetry Hardening
------------------------------------------------------------------
Testa:
1. Blocco preflight se Luna è in cooldown/quota esaurita per subtask n8n_mcp (nessun fallback ds-flash).
2. Blocco preflight se il workflow target ha availableInMCP=false (es. oyJX2al4JMmNz7lk).
3. Superamento preflight per workflow con availableInMCP=true (es. zbbXieIEJcwtMRWp).
4. Task workspace per AGY invariati e superamento preflight.
5. Telemetria token: distinzione chiara tra 'not_measured' e valori numerici misurati.
6. Propagazione dello stato BLOCKED_PREREQUISITE nelle API e nel QueueWorker.
"""

import os
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from orchestrator_core import (
    MultiAgentRunner,
    TokensDict,
    KNOWN_N8N_WORKFLOWS,
)
from account_manager import CodexAccountManager
from infrastructure.run_state_adapter import RunStateAdapter
from infrastructure.run_queue import QueueWorker, extract_terminal_json


class TestPrerequisitePreflight(unittest.TestCase):
    def setUp(self):
        self.runner = MultiAgentRunner(
            workspace_path=Path("/tmp/test_workspace"),
            preset="standard",
            mock_mode=False
        )

    def test_tokens_dict_distinguishes_not_measured_from_zero(self):
        """4. Telemetria: i token non misurati restituiscono 'not_measured', non 0 numerico ambiguo."""
        td = TokensDict()
        summary = td.to_summary()
        self.assertEqual(summary["agy"], "not_measured")
        self.assertEqual(summary["luna"], "not_measured")
        self.assertEqual(summary["deepseek_flash"], "not_measured")
        self.assertEqual(summary["total"], "not_measured")
        self.assertEqual(summary["measurement_status"]["agy"], "not_measured")

        # Quando viene registrato un consumo effettivo
        td.record("agy", 450, measured=True)
        summary_after = td.to_summary()
        self.assertEqual(summary_after["agy"], 450)
        self.assertEqual(summary_after["total"], 450)
        self.assertEqual(summary_after["measurement_status"]["agy"], "measured")
        self.assertEqual(summary_after["measurement_status"]["luna"], "not_measured")

    def test_luna_in_cooldown_blocks_preflight(self):
        """1. Se Luna è in cooldown/quota esaurita, il preflight blocca il subtask e non invoca fallback ds-flash."""
        task_n8n = {
            "id": "T2",
            "d": "Esportazione read-only del workflow live",
            "a": "luna",
            "target": "n8n_mcp",
            "p": "Esporta il workflow zbbXieIEJcwtMRWp via server MCP"
        }

        # Mock: nessun account Luna disponibile (cooldown attivo)
        with patch.object(self.runner.account_manager, "get_account_for_role", return_value=None):
            allowed, reason = self.runner.check_subtask_prerequisites(task_n8n)
            self.assertFalse(allowed)
            self.assertIn("nessun account Luna è disponibile", reason)
            self.assertIn("Fallback a DeepSeek Flash disabilitato", reason)

            # Invocazione diretta execute_task: deve restituire payload BLOCKED_PREREQUISITE
            out = self.runner.execute_task(task_n8n)
            self.assertIn("BLOCKED_PREREQUISITE", out)
            self.assertIn("nessun account Luna è disponibile", out)

    def test_n8n_live_mutation_attempt_fails_closed(self):
        """2. Qualsiasi tentativo di mutazione/scrittura/modifica live n8n viene bloccato fail-closed."""
        task_mutation = {
            "id": "T2",
            "d": "Modifica nodo nel workflow live",
            "a": "luna",
            "target": "n8n_mcp",
            "p": "Modifica nodo HTTP e aggiorna workflow live oyJX2al4JMmNz7lk"
        }

        mock_acc = MagicMock()
        mock_acc.name = "luna"
        with patch.object(self.runner.account_manager, "get_account_for_role", return_value=mock_acc):
            allowed, reason = self.runner.check_subtask_prerequisites(task_mutation)
            self.assertFalse(allowed)
            self.assertIn("Tentata modifica/mutazione live", reason)
            self.assertIn("strettamente circoscritto a sola lettura", reason)

    def test_dynamic_catalog_readonly_export_passes_preflight(self):
        """3. Se il task è in sola lettura/export ed il workflow esiste nel catalogo dinamico, il preflight passa."""
        task_master = {
            "id": "T2",
            "d": "Esporta MiniApp Master API in sola lettura",
            "a": "luna",
            "target": "n8n_mcp",
            "p": "Esporta il workflow MiniApp Master API (ID: oyJX2al4JMmNz7lk) per sincronizzazione mirror"
        }

        mock_acc = MagicMock()
        mock_acc.name = "luna"
        fake_catalog = {"oyJX2al4JMmNz7lk": {"name": "MiniApp Master API", "id": "oyJX2al4JMmNz7lk"}}
        with patch.object(self.runner.account_manager, "get_account_for_role", return_value=mock_acc), \
             patch.object(MultiAgentRunner, "get_dynamic_n8n_workflows_catalog", return_value=fake_catalog):
            allowed, reason = self.runner.check_subtask_prerequisites(task_master)
            self.assertTrue(allowed)
            self.assertIsNone(reason)

    def test_nonexistent_workflow_id_fails_preflight(self):
        """4. Se l'ID workflow non esiste nel catalogo dinamico, il preflight blocca il subtask."""
        task_unknown = {
            "id": "T2",
            "d": "Esporta workflow sconosciuto",
            "a": "luna",
            "target": "n8n_mcp",
            "p": "Esporta il workflow ID nonExistentId1234 per sincronizzazione"
        }

        mock_acc = MagicMock()
        mock_acc.name = "luna"
        fake_catalog = {"oyJX2al4JMmNz7lk": {"name": "MiniApp Master API"}}
        with patch.object(self.runner.account_manager, "get_account_for_role", return_value=mock_acc), \
             patch.object(MultiAgentRunner, "get_dynamic_n8n_workflows_catalog", return_value=fake_catalog):
            allowed, reason = self.runner.check_subtask_prerequisites(task_unknown)
            self.assertFalse(allowed)
            self.assertIn("non è stato trovato nel catalogo live", reason)

    def test_readonly_mcp_server_blocks_mutations(self):
        """5. Il server MCP n8n-readonly rifiuta tassativamente qualsiasi tool di scrittura o esecuzione."""
        from infrastructure.n8n_readonly_mcp import process_jsonrpc_message

        # Tentativo di chiamata a execute_workflow
        resp_exec = process_jsonrpc_message({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "execute_workflow", "arguments": {"workflowId": "oyJX2al4JMmNz7lk"}}
        })
        self.assertTrue(resp_exec["result"].get("isError"))
        self.assertIn("non consentito", resp_exec["result"]["content"][0]["text"])

        # Tentativo di chiamata a update_workflow
        resp_upd = process_jsonrpc_message({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "update_workflow", "arguments": {"workflowId": "oyJX2al4JMmNz7lk"}}
        })
        self.assertTrue(resp_upd["result"].get("isError"))
        self.assertIn("non consentito", resp_upd["result"]["content"][0]["text"])

    def test_agy_workspace_tasks_unaffected(self):
        """6. I task workspace AGY non vengono alterati e superano il preflight."""
        task_agy = {
            "id": "T1",
            "d": "Preflight locale e lettura mirror",
            "a": "agy",
            "target": "workspace",
            "p": "Leggi il file docs/HANDOFF.md e verifica lo stato locale"
        }
        allowed, reason = self.runner.check_subtask_prerequisites(task_agy)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_run_full_workflow_aborts_early_with_blocked_prerequisite_status(self):
        """2 & 3. run_full_workflow interrompe l'esecuzione e restituisce BLOCKED_PREREQUISITE nel summary."""
        fake_tasks = [
            {"id": "T1", "d": "Preflight locale", "a": "agy", "target": "workspace", "p": "Leggi file"},
            {"id": "T2", "d": "Export live n8n", "a": "luna", "target": "n8n_mcp", "p": "Export oyJX2al4JMmNz7lk"},
            {"id": "T3", "d": "Sync mirror", "a": "agy", "target": "workspace", "p": "Sync file"}
        ]

        with patch.object(self.runner, "brainstorm", return_value={"analysis": "ok"}), \
             patch.object(self.runner, "decompose", return_value=fake_tasks), \
             patch.object(self.runner.account_manager, "get_account_for_role", return_value=None):

            summary = self.runner.run_full_workflow(
                task_description="Sincronizza Master API live",
                branch_name="feature/test-preflight-blocked"
            )

            self.assertEqual(summary["status"], "BLOCKED_PREREQUISITE")
            self.assertEqual(summary["blocker_task_id"], "T2")
            self.assertIn("nessun account Luna è disponibile", summary["blocker_reason"])
            self.assertIsNone(summary["html_diff"])
            self.assertFalse(summary["git_committed"])

            # Verifica subtask risultati
            subtasks = summary["subtasks"]
            self.assertEqual(len(subtasks), 3)
            self.assertIn("BLOCKED_PREREQUISITE", subtasks[1]["outcome"])
            self.assertIn("SKIPPED", subtasks[2]["outcome"])

    def test_agy_sidecar_ready_passes_preflight(self):
        """7. Preflight AGY con sidecar disponibile (check_ready -> True) è consentito."""
        task_agy = {
            "id": "T1",
            "d": "Preflight locale AGY",
            "a": "agy",
            "target": "workspace",
            "p": "Ispeziona file"
        }
        with patch.dict(os.environ, {"TAKTSTOCK_HOST_AGY_SIDECAR": "1"}), \
             patch("infrastructure.host_agy_client.HostAgyClient.check_ready", return_value=True):
            allowed, reason = self.runner.check_subtask_prerequisites(task_agy)
            self.assertTrue(allowed)
            self.assertIsNone(reason)

    def test_agy_sidecar_not_ready_blocks_preflight_without_llm(self):
        """8. Preflight AGY con sidecar non disponibile (check_ready -> False) blocca con BLOCKED_PREREQUISITE senza chiamare LLM."""
        task_agy = {
            "id": "T1",
            "d": "Preflight locale AGY",
            "a": "agy",
            "target": "workspace",
            "p": "Ispeziona file"
        }
        with patch.dict(os.environ, {"TAKTSTOCK_HOST_AGY_SIDECAR": "1"}), \
             patch("infrastructure.host_agy_client.HostAgyClient.check_ready", return_value=False):
            allowed, reason = self.runner.check_subtask_prerequisites(task_agy)
            self.assertFalse(allowed)
            self.assertIn("non è pronto", reason)

    def test_telegram_progress_node_configuration_has_parse_mode_none(self):
        """9. Verifica che il workflow n8n mirror abbia parse_mode='None' sul nodo Send Live Progress."""
        wf_path = Path(__file__).resolve().parent.parent / "server" / "Taktstock Multi-Agent Orchestrator.json"
        self.assertTrue(wf_path.exists())
        with open(wf_path, "r", encoding="utf-8") as f:
            wf_data = json.load(f)

        found_node = False
        for node in wf_data.get("nodes", []):
            if node.get("name") == "Send Live Progress":
                found_node = True
                additional_fields = node.get("parameters", {}).get("additionalFields", {})
                self.assertEqual(additional_fields.get("parse_mode"), "None")

        self.assertTrue(found_node)

    def test_luna_profile_host_available_without_local_codex_dir_passes_preflight(self):
        """10. Profilo Luna host disponibile con container senza ~/.codex/accounts consente il preflight."""
        task_n8n = {
            "id": "T2",
            "d": "Esportazione workflow",
            "a": "luna",
            "target": "n8n_mcp",
            "p": "Export live oyJX2al4JMmNz7lk"
        }
        with patch.dict(os.environ, {"TAKTSTOCK_HOST_CODEX_SIDECAR": "1"}), \
             patch("pathlib.Path.home", return_value=Path("/nonexistent_home_in_container")), \
             patch("infrastructure.host_codex_client.HostCodexClient.check_ready", return_value=True), \
             patch.object(self.runner, "check_workflow_mcp_availability", return_value=(True, None)):
            
            # Re-inizializza account_manager per simulare l'avvio in container
            from account_manager import CodexAccountManager
            self.runner.account_manager = CodexAccountManager()
            
            self.assertIn("luna", [a.name for a in self.runner.account_manager.accounts])
            allowed, reason = self.runner.check_subtask_prerequisites(task_n8n)
            self.assertTrue(allowed)
            self.assertIsNone(reason)

    def test_luna_sidecar_not_ready_blocks_preflight_without_llm(self):
        """11. Sidecar Codex host non disponibile blocca con BLOCKED_PREREQUISITE senza chiamare LLM."""
        task_n8n = {
            "id": "T2",
            "d": "Esportazione workflow",
            "a": "luna",
            "target": "n8n_mcp",
            "p": "Export live oyJX2al4JMmNz7lk"
        }
        with patch.dict(os.environ, {"TAKTSTOCK_HOST_CODEX_SIDECAR": "1"}), \
             patch("infrastructure.host_codex_client.HostCodexClient.check_ready", return_value=False):
            
            allowed, reason = self.runner.check_subtask_prerequisites(task_n8n)
            self.assertFalse(allowed)
            self.assertIn("sidecar Host Codex non è pronto o non è raggiungibile", reason)

    def test_n8n_evaluate_run_state_terminates_on_blocked_prerequisite(self):
        """12. Verifica che il nodo Evaluate Run State nel JSON del workflow n8n termini su blocked_prerequisite senza polling."""
        wf_path = Path(__file__).resolve().parent.parent / "server" / "Taktstock Multi-Agent Orchestrator.json"
        with open(wf_path, "r", encoding="utf-8") as f:
            wf_data = json.load(f)

        eval_node = next((n for n in wf_data.get("nodes", []) if n.get("name") == "Evaluate Run State"), None)
        self.assertIsNotNone(eval_node)
        js_code = eval_node["parameters"]["jsCode"]
        self.assertIn("blocked_prerequisite", js_code)
        self.assertIn("route: 'failed'", js_code)
        self.assertIn("BLOCKED_PREREQUISITE", js_code)

    def test_run_queue_worker_handles_blocked_prerequisite(self):
        """6. Il worker della coda imposta lo status su blocked_prerequisite quando il summary lo indica."""
        payload_str = json.dumps({
            "status": "BLOCKED_PREREQUISITE",
            "task": "Test task",
            "blocker_reason": "Workflow non esposto su MCP",
            "tokens_used": {"total": "not_measured"}
        })
        extracted = extract_terminal_json(payload_str)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted.get("status"), "BLOCKED_PREREQUISITE")


if __name__ == "__main__":
    unittest.main()
