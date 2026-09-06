#!/usr/bin/env python3
"""
Unit tests for deterministic mechanical execution, preset parsing, write allowlisting, and telemetry granularity.
Zero LLM calls, zero sidecars, pure local testing.
"""

import os
import sys
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import moduli Ufficio
server_dir = Path(__file__).resolve().parent.parent / "server"
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from health_server import parse_command_text, validate_api_run_payload, ALLOWED_API_PRESETS
from orchestrator_core import MultiAgentRunner, TokensDict, BudgetExceededError, MECHANICAL_ALLOWED_WRITE_FILES
from infrastructure.database import DatabaseManager
from infrastructure.telemetry_repository import TelemetryRepository, extract_token_breakdown, extract_reported_tokens


class TestMechanicalPresetParsing(unittest.TestCase):
    """Test parsing, stripping e validazione fail-closed dei preset."""

    def test_parse_preset_colon_syntax(self):
        """Riconosce preset:mechanical e rimuove il selettore dal testo."""
        res = parse_command_text("Sincronizza il mirror preset:mechanical")
        self.assertEqual(res["preset"], "mechanical")
        self.assertEqual(res["text"], "Sincronizza il mirror")
        self.assertIsNone(res.get("invalid_preset"))

    def test_parse_preset_flag_syntax(self):
        """Riconosce --preset mechanical e rimuove il selettore dal testo."""
        res = parse_command_text("/task Sincronizza MiniApp Master API --preset mechanical")
        self.assertEqual(res["action"], "execute_task")
        self.assertEqual(res["preset"], "mechanical")
        self.assertEqual(res["text"], "Sincronizza MiniApp Master API")
        self.assertIsNone(res.get("invalid_preset"))

    def test_parse_preset_equals_syntax(self):
        """Riconosce --preset=critical e rimuove il selettore dal testo."""
        res = parse_command_text("Analisi vulnerabilità --preset=critical")
        self.assertEqual(res["preset"], "critical")
        self.assertEqual(res["text"], "Analisi vulnerabilità")

    def test_parse_preset_shorthand(self):
        """Riconosce --mechanical e /mechanical."""
        res1 = parse_command_text("Export mirror --mechanical")
        self.assertEqual(res1["preset"], "mechanical")
        self.assertEqual(res1["text"], "Export mirror")

        res2 = parse_command_text("/mechanical Sincronizza mirror")
        self.assertEqual(res2["preset"], "mechanical")
        self.assertEqual(res2["text"], "Sincronizza mirror")

    def test_unknown_preset_rejected_fail_closed(self):
        """Un preset sconosciuto viene intercettato e rifiutato invece di degradare a standard."""
        res = parse_command_text("Esegui task preset:invalid_preset_xyz")
        self.assertEqual(res.get("invalid_preset"), "invalid_preset_xyz")

        # Verifica rifiuto nel payload validator
        is_valid, err_msg, sanitized = validate_api_run_payload({
            "action": "execute_task",
            "task": "Esegui task preset:invalid_preset_xyz",
            "repo": "Assistente"
        })
        self.assertFalse(is_valid)
        self.assertIn("Preset non valido o sconosciuto: 'invalid_preset_xyz'", err_msg)
        self.assertIsNone(sanitized)

    def test_runner_init_rejects_unknown_preset(self):
        """MultiAgentRunner rifiuta preset sconosciuti all'inizializzazione."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(ValueError) as ctx:
                MultiAgentRunner(workspace_path=Path(tmp_dir), preset="non_existent_preset")
            self.assertIn("Preset non valido o sconosciuto", str(ctx.exception))


class TestDeterministicMechanicalExecution(unittest.TestCase):
    """Test del runner deterministico per task meccanici (0 LLM, 0 agenti)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp_dir.name)
        (self.workspace / ".git").mkdir()
        self.runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="mechanical",
            mock_mode=True,
            mechanical_action="sync-n8n-mirror"
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_mechanical_subtasks_generation(self):
        """Genera subtask deterministici con agent='local_mechanical'."""
        tasks = self.runner.generate_mechanical_subtasks("Sincronizza il mirror locale MiniApp Master API")
        self.assertEqual(len(tasks), 4)
        for t in tasks:
            self.assertEqual(t["a"], "local_mechanical")
        self.assertEqual(tasks[0]["id"], "T1")
        self.assertEqual(tasks[1]["id"], "T2")
        self.assertEqual(tasks[2]["id"], "T3")
        self.assertEqual(tasks[3]["id"], "T4")

    def test_mechanical_t1_preflight_zero_agent_calls(self):
        """T1 preflight esegue solo controlli locali e zero chiamate agente/LLM."""
        t1_task = {
            "id": "T1",
            "d": "Preflight locale deterministico",
            "a": "local_mechanical",
            "target": "workspace"
        }
        res_str = self.runner.execute_task(t1_task)
        res = json.loads(res_str)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["phase"], "preflight")
        self.assertTrue(res["workspace_exists"])
        self.assertEqual(res["agent_calls"], 0)
        self.assertEqual(res["tokens_used"], 0)
        self.assertEqual(self.runner.tokens_used.get_total_calls(), 0)

    def test_mechanical_full_pipeline_zero_llm_calls(self):
        """L'intera pipeline meccanica T1-T4 viene eseguita con 0 chiamate LLM e 0 token."""
        task_desc = "Sincronizza il mirror locale MiniApp Master API preset:mechanical --action sync-n8n-mirror"
        res = self.runner.run_full_workflow(task_desc, branch_name="feature/mirror-sync")

        self.assertIn(res["status"], ["COMPLETED", "WAITING_FOR_APPROVAL"])
        self.assertEqual(res["preset"], "mechanical")
        self.assertEqual(res["tokens_used"]["total_measured"], None)
        self.assertEqual(res["tokens_used"]["calls_count"]["total"], 0)

        # Verifica file scritti
        json_mirror = self.workspace / "MiniApp_Master_API.json"
        log_file = self.workspace / "WORKFLOW_LOG.md"
        self.assertTrue(json_mirror.exists())
        self.assertTrue(log_file.exists())

        # Validazione contenuto JSON
        data = json.loads(json_mirror.read_text(encoding="utf-8"))
        self.assertIn("nodes", data)

    def test_write_allowlist_enforcement(self):
        """Il runner meccanico rifiuta qualsiasi tentativo di scrittura fuori allowlist."""
        self.assertIn("MiniApp_Master_API.json", MECHANICAL_ALLOWED_WRITE_FILES)
        self.assertIn("WORKFLOW_LOG.md", MECHANICAL_ALLOWED_WRITE_FILES)
        self.assertIn("docs/HANDOFF.md", MECHANICAL_ALLOWED_WRITE_FILES)

        # File autorizzati passano
        self.runner.assert_allowlisted_write("MiniApp_Master_API.json")
        self.runner.assert_allowlisted_write("WORKFLOW_LOG.md")
        self.runner.assert_allowlisted_write("docs/HANDOFF.md")

        # File non autorizzati vengono categoricamente rifiutati
        unauthorized_files = ["malicious.py", "server/main.py", "exploit.sh", "../outside.json"]
        for bad_file in unauthorized_files:
            with self.assertRaises(PermissionError):
                self.runner.assert_allowlisted_write(bad_file)


class TestBudgetAndTelemetryGranularity(unittest.TestCase):
    """Test del budget guard preventivo e della granularità telemetrica."""

    def test_budget_checked_before_each_llm_call(self):
        """Verifica che il budget blocchi la chiamata prima dell'invocazione."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = MultiAgentRunner(
                workspace_path=Path(tmp_dir),
                preset="quick",
                mock_mode=False
            )
            # quick preset ha limite 4 chiamate totali
            runner.budget_limits["max_llm_calls_total"] = 2
            runner.tokens_used.record_call("sol", phase="execution", reported_tokens=1000)
            runner.tokens_used.record_call("sol", phase="execution", reported_tokens=1000)

            allowed, reason = runner.check_budget_before_call("sol", phase="execution", prompt_len=100)
            self.assertFalse(allowed)
            self.assertIn("Budget superato: limite massimo chiamate LLM", reason)

    def test_token_breakdown_extraction(self):
        """Estrae separatamente input, output, thinking e cache_read senza sommare cache_read ai token fatturati."""
        mock_output = json.dumps({
            "response": "ok",
            "usage": {
                "total_tokens": 1500,
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "reasoning_tokens": 200,
                "cached_tokens": 400
            }
        })
        bd = extract_token_breakdown(mock_output)
        self.assertEqual(bd["total_tokens"], 1500)
        self.assertEqual(bd["input_tokens"], 1000)
        self.assertEqual(bd["output_tokens"], 500)
        self.assertEqual(bd["thinking_tokens"], 200)
        self.assertEqual(bd["cache_read_tokens"], 400)

    def test_tokens_dict_summary_distinguishes_breakdown(self):
        """TokensDict mantiene e riporta in to_summary il breakdown granulare e le motivazioni."""
        td = TokensDict()
        td.record_call(
            agent_name="sol",
            phase="brainstorm",
            prompt_len=400,
            output_len=200,
            reported_tokens=150,
            input_tokens=100,
            output_tokens=50,
            thinking_tokens=30,
            cache_read_tokens=40,
            call_reason="brainstorm_initial"
        )
        summary = td.to_summary()
        self.assertEqual(summary["breakdown"]["input_tokens"], 100)
        self.assertEqual(summary["breakdown"]["output_tokens"], 50)
        self.assertEqual(summary["breakdown"]["thinking_tokens"], 30)
        self.assertEqual(summary["breakdown"]["cache_read_tokens"], 40)
        self.assertEqual(len(summary["call_reasons"]), 1)
        self.assertEqual(summary["call_reasons"][0]["reason"], "brainstorm_initial")


if __name__ == "__main__":
    unittest.main()
