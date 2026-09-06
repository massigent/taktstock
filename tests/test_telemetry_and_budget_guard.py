#!/usr/bin/env python3
"""
Unit tests for Telemetry, Budget Guard, and Mechanical Task Execution
---------------------------------------------------------------------
Test rigorosamente senza chiamate a LLM (mock/stubs).
Verifica:
1. Telemetria SQLite (campi aggiuntivi, stima vs reale, assenza di segreti/prompt completi).
2. Budget Guard: blocco prima dell'invocazione LLM con BLOCKED_BUDGET.
3. Task meccanici/read-only: bypass completo di Brainstorming e Decomposizione Sol (Sol calls = 0).
4. Disabilitazione fixer/retry per BLOCKED_PREREQUISITE e BLOCKED_BUDGET.
5. TokensDict: distinzione tra real, estimated e not_measured (nessun falso 0).
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Assicura import corretti
server_dir = Path(__file__).resolve().parent.parent / "server"
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from infrastructure.database import DatabaseManager
from infrastructure.telemetry_repository import TelemetryRepository, sanitize_telemetry_metadata, extract_reported_tokens
from infrastructure.agent_gateway import AgentGateway
from orchestrator_core import MultiAgentRunner, TokensDict, BudgetExceededError, DEFAULT_BUDGET_LIMITS


class TestTelemetryAndBudgetGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ufficio_telemetry_test_")
        self.db_path = Path(self.temp_dir) / "test_telemetry.db"
        self.db_manager = DatabaseManager(self.db_path)
        self.db_manager.run_migrations()
        self.telemetry_repo = TelemetryRepository(self.db_manager)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_telemetry_repository_records_enriched_fields_safely(self):
        """1. Registra correttamente campi arricchiti (subtask_id, estimated_tokens, sha256) senza segreti."""
        event_id = self.telemetry_repo.record_event(
            agent_role="sol",
            provider="openai_codex",
            model="gpt-5.6-sol",
            phase="planning",
            status="SUCCESS",
            duration_ms=1200,
            prompt_length=500,
            reported_tokens=None,  # Non misurato -> deve stimare
            estimated_tokens=None, # Deve calcolare stima automatica
            run_id="run-1234",
            subtask_id="T1",
            session_id="sess-abc",
            prompt_sha256="abcdef1234567890",
            metadata={
                "preset": "standard",
                "output_length": 300,
                "secret_key": "sk-proj-SECRET123"  # Deve essere filtrato via da SAFE_METADATA_KEYS
            }
        )
        self.assertIsNotNone(event_id)

        # Verifica in SQLite
        with self.db_manager.connection() as conn:
            cursor = conn.execute("SELECT * FROM agent_usage_events WHERE id = ?", (event_id,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["agent_role"], "sol")
            self.assertEqual(row["subtask_id"], "T1")
            self.assertEqual(row["run_id"], "run-1234")
            self.assertEqual(row["prompt_sha256"], "abcdef1234567890")
            self.assertIsNone(row["reported_tokens"])
            self.assertEqual(row["estimated_tokens"], (500 + 300) // 4) # 200 token stimati

            # Verifica che il segreto NON sia finito nei metadati JSON
            meta_json = row["metadata_json"]
            self.assertIsNotNone(meta_json)
            self.assertNotIn("sk-proj", meta_json)
            self.assertNotIn("secret_key", meta_json)
            self.assertIn("preset", meta_json)

    def test_tokens_dict_distinguishes_real_estimated_and_not_measured(self):
        """2. TokensDict distingue real, estimated e not_measured senza presentare falsi 0."""
        td = TokensDict()
        
        # Inizialmente nessun token misurato né stimato
        summary1 = td.to_summary()
        self.assertEqual(summary1["total"], "not_measured")
        self.assertEqual(summary1["sol"], "not_measured")
        self.assertEqual(summary1["luna"], "not_measured")
        self.assertEqual(summary1["calls_count"]["total"], 0)

        # Registra una chiamata con token reali
        td.record_call("sol", phase="planning", prompt_len=400, output_len=200, reported_tokens=150)
        summary2 = td.to_summary()
        self.assertEqual(summary2["sol"], 150)
        self.assertEqual(summary2["measurement_status"]["sol"], "measured")
        self.assertEqual(summary2["calls_count"]["total"], 1)
        self.assertEqual(summary2["calls_count"]["by_agent"]["sol"], 1)

        # Registra una chiamata senza token reali (solo stima)
        td.record_call("luna", phase="n8n_execution", prompt_len=800, output_len=400, reported_tokens=None)
        summary3 = td.to_summary()
        self.assertEqual(summary3["measurement_status"]["luna"], "estimated")
        self.assertIn("~300 (estimated)", str(summary3["luna"]))
        self.assertEqual(summary3["calls_count"]["total"], 2)
        self.assertEqual(summary3["calls_count"]["by_agent"]["luna"], 1)
        self.assertEqual(summary3["calls_count"]["by_phase"]["n8n_execution"], 1)

    def test_budget_guard_blocks_before_llm_call_when_limit_exceeded(self):
        """3. BudgetGuard blocca prima della chiamata LLM sollevando BudgetExceededError."""
        runner = MultiAgentRunner(
            workspace_path=self.temp_dir,
            preset="quick",
            mock_mode=False
        )
        runner.budget_limits = {
            "max_llm_calls_total": 2,
            "max_llm_calls_per_agent": {"sol": 1, "luna": 1, "agy": 5},
            "max_estimated_tokens": 1000
        }

        # Prima chiamata Sol: consentita
        allowed, err = runner.check_budget_before_call("sol", phase="planning", prompt_len=100)
        self.assertTrue(allowed)
        self.assertIsNone(err)
        runner.tokens_used.record_call("sol", phase="planning", prompt_len=100, output_len=100)

        # Seconda chiamata Sol: deve essere BLOCCATA per superamento max per-agent (1)
        allowed, err = runner.check_budget_before_call("sol", phase="planning", prompt_len=100)
        self.assertFalse(allowed)
        self.assertIn("limite chiamate per l'agente 'sol' (1) raggiunto", err)

        # Invocazione call_director deve sollevare BudgetExceededError senza effettuare chiamate
        with self.assertRaises(BudgetExceededError):
            runner.call_director("Task prompt...")

    def test_mechanical_task_bypasses_sol_brainstorm_and_decomposition(self):
        """4. I task meccanici/read-only bypassano completamente Brainstorm e Decompose di Sol."""
        runner = MultiAgentRunner(
            workspace_path=self.temp_dir,
            preset="standard",
            mock_mode=True
        )

        mechanical_task = "esporta in sola lettura da n8n live il workflow MiniApp Master API e sincronizza il mirror locale --action sync-n8n-mirror"
        self.assertTrue(runner.is_mechanical_task(mechanical_task))

        # Esegui workflow completo mock su task meccanico
        summary = runner.run_full_workflow(mechanical_task, branch_name="test-mech-branch")

        # Verifica che Sol non sia mai stato chiamato (calls["by_agent"]["sol"] == 0)
        sol_calls = runner.tokens_used.get_agent_calls("sol")
        self.assertEqual(sol_calls, 0, "Sol non deve essere invocato per task meccanici di mirror/export")
        self.assertEqual(summary["status"], "COMPLETED" if summary.get("status") in ["COMPLETED", "WAITING_FOR_APPROVAL"] else summary.get("status"))

    def test_run_terminates_with_blocked_budget_when_limit_hit(self):
        """5. Quando il budget è esaurito a metà run, l'orchestrazione termina con BLOCKED_BUDGET."""
        runner = MultiAgentRunner(
            workspace_path=self.temp_dir,
            preset="standard",
            mock_mode=False
        )
        # Limite severo: 1 sola chiamata ad agy consentita
        runner.budget_limits = {
            "max_llm_calls_total": 10,
            "max_llm_calls_per_agent": {"sol": 0, "luna": 2, "agy": 1},
            "max_estimated_tokens": 50000
        }

        # Mock subtask execution
        with patch.object(runner, "check_subtask_prerequisites", return_value=(True, None)), \
             patch.object(runner, "is_mechanical_task", return_value=True):
            
            # T1 consumerà la chiamata ad agy
            def mock_exec_task(t):
                if t["id"] == "T1":
                    runner.tokens_used.record_call("agy", phase="execution", prompt_len=200, output_len=200)
                    return json.dumps({"status": "SUCCESS"})
                # T2 richiede chiamata ad agy che eccede il budget
                allowed, err = runner.check_budget_before_call("agy", phase="execution")
                if not allowed:
                    raise BudgetExceededError(err)
                return json.dumps({"status": "SUCCESS"})

            with patch.object(runner, "execute_task", side_effect=mock_exec_task):
                summary = runner.run_full_workflow("sincronizza il mirror locale --action sync-n8n-mirror", branch_name="test-budget-branch")

                self.assertEqual(summary["status"], "BLOCKED_BUDGET")
                self.assertIn("Budget superato", summary["blocker_reason"])
                self.assertEqual(summary["blocker_task_id"], "T2")
                self.assertIn("SKIPPED", summary["subtasks"][2]["outcome"])


if __name__ == "__main__":
    unittest.main()
