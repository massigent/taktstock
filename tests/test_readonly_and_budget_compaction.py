#!/usr/bin/env python3
"""
Unit and Regression Tests for:
1. Explicit Read-Only / Preventive Sandbox & Invariance Guard
2. Bounded Audit & Dynamic Conservative Budget Reservation & Compaction
3. State Directory Isolation
--------------------------------------------------------------------------
Test rigorosamente senza chiamate reali a LLM (mock/stubs).
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from orchestrator_core import (
    MultiAgentRunner,
    is_explicit_read_only_task,
    is_audit_task,
    check_read_only_invariance,
    get_role_max_output_tokens,
    DEFAULT_BUDGET_LIMITS,
    BudgetExceededError,
)


class TestReadOnlyAndInvarianceGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="taktstock_readonly_test_")
        self.repo_dir = Path(self.temp_dir) / "test_repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = Path(self.temp_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Inizializza un repository Git reale e pulito
        subprocess.run(["git", "init"], cwd=str(self.repo_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@taktstock.local"], cwd=str(self.repo_dir), check=True)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.repo_dir), check=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_detection_of_explicit_read_only_phrases(self):
        """1. Rilevamento accurato delle frasi esplicite di sola lettura e divieto branch/worktree."""
        phrases_true = [
            "Esegui un audit del codice in sola lettura",
            "Analisi architetturale non creare branch o worktree",
            "Verifica dei file senza branch",
            "Controllo di sicurezza in solo lettura",
            "Audit performance read-only sul repository",
            "Task con flag --read-only per ispezione",
            "mode: read-only verifica conformità",
            "non creare branch né worktree sul repo",
        ]
        for phrase in phrases_true:
            self.assertTrue(
                is_explicit_read_only_task(phrase),
                f"Dovrebbe essere riconosciuto come read-only: '{phrase}'"
            )

        phrases_false = [
            "Implementa la nuova feature di login",
            "Aggiungi un endpoint REST per i pagamenti",
            "Correggi il bug nel calcolo dei prezzi",
        ]
        for phrase in phrases_false:
            self.assertFalse(
                is_explicit_read_only_task(phrase),
                f"NON dovrebbe essere riconosciuto come read-only: '{phrase}'"
            )

    def test_check_read_only_invariance_valid_and_invalid(self):
        """2. check_read_only_invariance rileva repository valido/pulito o non valido/inaccessibile."""
        valid, err, initial_status = check_read_only_invariance(self.repo_dir)
        self.assertTrue(valid)
        self.assertIsNone(err)
        self.assertEqual(initial_status, "")

        non_existent = Path(self.temp_dir) / "does_not_exist"
        valid_ne, err_ne, _ = check_read_only_invariance(non_existent)
        self.assertFalse(valid_ne)
        self.assertIn("non trovato o non accessibile", err_ne)

        not_a_git_dir = Path(self.temp_dir) / "plain_dir"
        not_a_git_dir.mkdir()
        valid_ng, err_ng, _ = check_read_only_invariance(not_a_git_dir)
        self.assertFalse(valid_ng)
        self.assertIn("non è un repository Git valido", err_ng)

    def test_call_executor_agy_passes_readonly_sandbox_when_read_only_is_active(self):
        """3. Quando read_only è attivo, call_executor_agy passa sandbox_mode='read-only' ad AgentGateway."""
        runner = MultiAgentRunner(
            workspace_path=self.repo_dir,
            mock_mode=False,
            read_only=True,
            state_dir=self.state_dir
        )

        mock_execute = MagicMock(return_value=(True, "Audit report text", {"reported_tokens": 200}))
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", mock_execute):
            out = runner.call_executor_agy("Audit rapido dei file")
            self.assertEqual(out, "Audit report text")
            mock_execute.assert_called_once()
            called_kwargs = mock_execute.call_args.kwargs
            self.assertEqual(called_kwargs.get("sandbox_mode"), "read-only")

    def test_call_executor_agy_passes_workspace_write_when_not_read_only(self):
        """4. Quando read_only è False, call_executor_agy passa sandbox_mode='workspace-write' ad AgentGateway."""
        runner = MultiAgentRunner(
            workspace_path=self.repo_dir,
            mock_mode=False,
            read_only=False,
            state_dir=self.state_dir
        )

        mock_execute = MagicMock(return_value=(True, "Execution text", {"reported_tokens": 300}))
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", mock_execute):
            out = runner.call_executor_agy("Implementa codice")
            self.assertEqual(out, "Execution text")
            mock_execute.assert_called_once()
            called_kwargs = mock_execute.call_args.kwargs
            self.assertEqual(called_kwargs.get("sandbox_mode"), "workspace-write")

    def test_read_only_workflow_runs_without_worktree_commit_or_push(self):
        """5. MultiAgentRunner in sola lettura esegue il workflow senza creare branch/worktree, commit o push."""
        runner = MultiAgentRunner(
            workspace_path=self.repo_dir,
            mock_mode=True,
            read_only=True,
            state_dir=self.state_dir
        )

        task_desc = "Audit di conformità e sicurezza del repository in sola lettura"
        res = runner.run_full_workflow(task_desc, branch_name="", do_push=True)

        self.assertEqual(res["status"], "COMPLETED")
        self.assertFalse(res["git_committed"])
        self.assertIsNone(res["html_diff"])

        valid, _, current_status = check_read_only_invariance(self.repo_dir)
        self.assertTrue(valid)
        self.assertEqual(current_status, "")

    def test_read_only_workflow_fails_if_invariance_is_violated(self):
        """6. Se durante l'esecuzione in sola lettura vengono creati/modificati file, il workflow fallisce per violazione invarianza."""
        runner = MultiAgentRunner(
            workspace_path=self.repo_dir,
            mock_mode=False,
            read_only=True,
            state_dir=self.state_dir
        )

        def malicious_executor(prompt, *args, **kwargs):
            (self.repo_dir / "unauthorized_file.txt").write_text("modifica non consentita", encoding="utf-8")
            return "Audit completato con file generato."

        with patch.object(runner, "call_executor_agy", side_effect=malicious_executor), \
             patch.object(runner, "brainstorm", return_value={"analysis": "ok"}), \
             patch.object(runner, "decompose", return_value=[{"id": "T1", "a": "agy", "d": "Audit", "p": "Audit"}]), \
             patch.object(runner, "call_director", return_value={"ok": True, "status": "done"}), \
             patch.object(runner, "validate", return_value={"status": "done"}):

            task_desc = "Ispezione in sola lettura non creare branch o worktree"
            res = runner.run_full_workflow(task_desc, branch_name="", do_push=False)

            self.assertEqual(res["status"], "FAILED")
            self.assertIn("ERROR_READ_ONLY_INVARIANCE_VIOLATION", res.get("blocker_reason", ""))
            self.assertFalse(res["git_committed"])


class TestBoundedAuditAndBudgetCompaction(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="taktstock_budget_test_")
        self.workspace = Path(self.temp_dir) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir = Path(self.temp_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_budget_limits_are_strictly_preserved(self):
        """1. I limiti di DEFAULT_BUDGET_LIMITS sono preservati senza aumenti o bypass."""
        self.assertEqual(DEFAULT_BUDGET_LIMITS["standard"]["max_llm_calls_total"], 8)
        self.assertEqual(DEFAULT_BUDGET_LIMITS["quick"]["max_llm_calls_total"], 4)
        self.assertEqual(DEFAULT_BUDGET_LIMITS["critical"]["max_llm_calls_total"], 18)
        self.assertEqual(DEFAULT_BUDGET_LIMITS["mechanical"]["max_llm_calls_total"], 4)

    def test_conservative_reservation_includes_input_and_max_output(self):
        """2. check_budget_before_call include input prompt + max output previsto per ruolo/fase."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            preset="standard",
            state_dir=self.state_dir
        )
        # Limitiamo il budget token a 5000 per verificare la prenotazione
        runner.budget_limits["max_estimated_tokens"] = 5000

        # Input 400 caratteri = 100 token input
        # AGY execution standard max output = 4000 token -> Totale prenotazione = 4100 token (<= 5000: consentito)
        allowed, err = runner.check_budget_before_call("agy", phase="execution", prompt_len=400)
        self.assertTrue(allowed)
        self.assertIsNone(err)

        # Input 5000 caratteri = 1250 token input + 4000 output = 5250 token (> 5000: bloccato)
        allowed_over, err_over = runner.check_budget_before_call("agy", phase="execution", prompt_len=5000)
        self.assertFalse(allowed_over)
        self.assertIn("Budget superato: stima token con prenotazione conservativa", err_over)

    def test_conservative_reservation_blocks_call_before_execution(self):
        """3. Se la prenotazione conservativa non entra nel budget, la chiamata non viene avviata (BLOCKED_BUDGET)."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            preset="standard",
            state_dir=self.state_dir
        )
        runner.budget_limits["max_estimated_tokens"] = 2000

        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call") as mock_exec:
            with self.assertRaises(BudgetExceededError):
                runner.call_executor_agy("Prompt molto lungo " * 200)
            mock_exec.assert_not_called()

    def test_conservative_reservation_compact_fallback_for_audit_task(self):
        """4. Per task audit/read-only, prova prima una prenotazione compatta ed economica se la standard sfora."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            read_only=True,
            preset="standard",
            state_dir=self.state_dir
        )
        runner.task_description = "Audit sola lettura"
        # 3000 token: la standard (100 + 4000 = 4100) non entra, ma la compatta audit (100 + 1200 = 1300) entra!
        runner.budget_limits["max_estimated_tokens"] = 3000
        allowed, err = runner.check_budget_before_call("agy", phase="execution", prompt_len=400)
        self.assertTrue(allowed)
        self.assertIsNone(err)

    def test_reservation_is_recalibrated_with_reported_tokens_after_response(self):
        """5. Dopo la risposta, la prenotazione viene sostituita/ricalibrata con i token effettivi."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            preset="standard",
            state_dir=self.state_dir
        )

        mock_execute = MagicMock(return_value=(True, "Audit output", {"reported_tokens": 150}))
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", mock_execute):
            runner.call_executor_agy("Verifica log")
            # Dopo la chiamata, le prenotazioni attive devono essere 0 e i token effettivi registrati
            self.assertEqual(runner.tokens_used.get_total_reserved_tokens(), 0)
            self.assertEqual(runner.tokens_used.get("agy"), 150)

    def test_decompose_compacts_plan_for_audit_tasks(self):
        """6. Decompose compatta dinamicamente il piano per task di audit/delimitati per rispettare il budget."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=True,
            preset="quick",
            state_dir=self.state_dir
        )
        mock_tasks = [
            {"id": "T1", "a": "agy", "d": "Ispezione modulo A", "p": "Prompt A"},
            {"id": "T2", "a": "agy", "d": "Ispezione modulo B", "p": "Prompt B"},
            {"id": "T3", "a": "agy", "d": "Ispezione modulo C", "p": "Prompt C"},
            {"id": "T4", "a": "agy", "d": "Ispezione modulo D", "p": "Prompt D"},
        ]

        with patch.object(runner, "call_director", return_value={"tasks": mock_tasks}):
            compacted_tasks = runner.decompose(
                task_description="Esegui audit codice e verifica sicurezza",
                brainstorm_ctx={"analysis": "ok"}
            )
            self.assertEqual(len(compacted_tasks), 1)
            self.assertIn("Ispezione modulo A", compacted_tasks[0]["d"])
            self.assertIn("Ispezione modulo B", compacted_tasks[0]["d"])

    def test_execute_task_skips_quick_check_and_reviews_when_budget_is_exhausted(self):
        """7. Quando il budget per Sol/Reviewer è saturo, quick check e review vengono saltati senza bloccare l'audit già eseguito."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            preset="standard",
            state_dir=self.state_dir
        )

        runner.tokens_used.record_call("sol", phase="planning", prompt_len=100, output_len=50)
        runner.tokens_used.record_call("sol", phase="planning", prompt_len=100, output_len=50)
        runner.tokens_used.record_call("sol", phase="planning", prompt_len=100, output_len=50)
        self.assertEqual(runner.tokens_used.get_agent_calls("sol"), 3)

        task = {
            "id": "T1",
            "a": "agy",
            "d": "Audit rapido configurazione",
            "p": "Controlla configurazione",
            "r": True
        }

        with patch.object(runner, "call_executor_agy", return_value="Audit superato con successo."):
            out = runner.execute_task(task)
            self.assertEqual(out, "Audit superato con successo.")

    def test_full_workflow_completes_bounded_audit_without_blocked_budget(self):
        """8. Un audit minimo e delimitato completa con successo COMPLETED senza incorrere in BLOCKED_BUDGET."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            preset="quick",
            state_dir=self.state_dir
        )

        task_desc = "Audit minimo e delimitato dei log di sistema in sola lettura"

        with patch.object(runner, "brainstorm", return_value={"analysis": "analisi audit"}), \
             patch.object(runner, "call_director", return_value={"tasks": [{"id": "T1", "a": "agy", "d": "Verifica log", "p": "Prompt log"}]}), \
             patch.object(runner, "call_executor_agy", return_value="Log verificati e conformi."):

            result = runner.run_full_workflow(task_desc, branch_name="", do_push=False)

            self.assertEqual(result["status"], "COMPLETED")
            self.assertNotEqual(result["status"], "BLOCKED_BUDGET")
            self.assertEqual(len(result["subtasks"]), 1)
            self.assertEqual(result["subtasks"][0]["outcome"], "Log verificati e conformi.")

    def test_strict_token_budget_blocks_agy_before_execution(self):
        """9. strict_token_budget=True: se il provider non ha cap nativo token, AGY non viene avviato e ritorna BLOCKED_BUDGET."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            preset="standard",
            state_dir=self.state_dir,
            strict_token_budget=True
        )

        # Invocazione diretta di call_executor_agy
        with self.assertRaises(BudgetExceededError) as ctx:
            runner.call_executor_agy("Esegui task con cap rigoroso")
        self.assertIn("cap token non imponibile dal provider", str(ctx.exception))

        # Invocazione workflow completo
        task_desc = "Implementa feature con budget garantito"
        with patch.object(runner, "brainstorm", return_value={"analysis": "ok"}), \
             patch.object(runner, "call_director", return_value={"tasks": [{"id": "T1", "a": "agy", "d": "Task 1", "p": "Prompt 1"}]}):

            result = runner.run_full_workflow(task_desc, branch_name="feat/strict", do_push=False)
            self.assertEqual(result["status"], "BLOCKED_BUDGET")
            self.assertIn("cap token non imponibile dal provider", result["blocker_reason"])

    def test_soft_budget_audit_timeout_and_time_budget_exceeded(self):
        """10. soft_budget: per audit/read-only passa timeout breve configurabile; allo scadere termina con TIME_BUDGET_EXCEEDED senza retry."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            read_only=True,
            audit_timeout_sec=15,
            preset="standard",
            state_dir=self.state_dir
        )
        runner.task_description = "Audit sola lettura con timeout"

        # Mock per verificare che il timeout di 15s venga passato al gateway
        mock_execute = MagicMock(return_value=(False, "TIME_BUDGET_EXCEEDED: Timeout esecuzione superato", {"status": "TIME_BUDGET_EXCEEDED"}))
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", mock_execute):
            task_desc = "Audit sola lettura con timeout"
            two_tasks = [
                {"id": "T1", "a": "agy", "d": "Audit T1", "p": "Prompt T1"},
                {"id": "T2", "a": "agy", "d": "Audit T2", "p": "Prompt T2"}
            ]
            with patch.object(runner, "brainstorm", return_value={"analysis": "ok"}), \
                 patch.object(runner, "decompose", return_value=two_tasks):

                result = runner.run_full_workflow(task_desc, branch_name="", do_push=False)

                # Verifica che timeout=15 sia stato passato ad AgentGateway
                mock_execute.assert_called()
                call_kwargs = mock_execute.call_args.kwargs
                self.assertEqual(call_kwargs.get("timeout"), 15)

                # Esito workflow: TIME_BUDGET_EXCEEDED, T1 fallito per timeout, T2 saltato senza retry
                self.assertEqual(result["status"], "TIME_BUDGET_EXCEEDED")
                self.assertIn("TIME_BUDGET_EXCEEDED", result["blocker_reason"])
                self.assertEqual(len(result["subtasks"]), 2)
                self.assertIn("TIME_BUDGET_EXCEEDED", result["subtasks"][0]["outcome"])
                self.assertIn("SKIPPED", result["subtasks"][1]["outcome"])

    def test_soft_budget_output_cap_truncation(self):
        """11. soft_budget: applica un cap all'output per richiesta per non saturare il contesto."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            mock_mode=False,
            read_only=True,
            preset="standard",
            state_dir=self.state_dir
        )
        large_response = "A" * 20000
        mock_execute = MagicMock(return_value=(True, large_response, {"reported_tokens": 500}))
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call", mock_execute):
            out = runner.call_executor_agy("Audit log lungo")
            self.assertLessEqual(len(out), 12500)
            self.assertIn("[OUTPUT_TRUNCATED_TO_SOFT_CAP]", out)

    def test_telegram_budget_clarity_guaranteed_vs_soft_cap(self):
        """12. I messaggi Telegram e i summary indicano chiaramente budget garantito oppure stima/soft cap."""
        from orchestrator_core import format_workflow_telegram_message, TokensDict
        from brainstorm_manager import BrainstormManager

        tokens_dict = TokensDict()
        tokens_dict.record_call("agy", phase="execution", prompt_len=200, output_len=200, reported_tokens=100)

        # 1. Summary token dict
        summary_strict = tokens_dict.to_summary(strict_guarantee=True)
        self.assertEqual(summary_strict["budget_guarantee_type"], "guaranteed")
        self.assertIn("Budget Garantito", summary_strict["budget_regime"])

        summary_soft = tokens_dict.to_summary(strict_guarantee=False)
        self.assertEqual(summary_soft["budget_guarantee_type"], "soft_cap")
        self.assertIn("Stima / Soft Cap", summary_soft["budget_regime"])

        # 2. Telegram Workflow Summary format
        fake_summary = {
            "task": "Test task",
            "preset": "standard",
            "status": "COMPLETED",
            "tokens_used": summary_soft
        }
        tg_msg_soft = format_workflow_telegram_message("Taktstock ha completato il lavoro!", fake_summary, is_strict=False)
        self.assertIn("Stima / Soft Cap", tg_msg_soft)

        tg_msg_strict = format_workflow_telegram_message("Taktstock ha completato il lavoro!", fake_summary, is_strict=True)
        self.assertIn("Budget Garantito", tg_msg_strict)

        # 3. Brainstorming Telegram Summary format
        bm = BrainstormManager(state_dir=self.state_dir)
        bs_fake = {
            "id": "bs-test-123",
            "task": "Test Brainstorm",
            "status": "active",
            "rounds": [{"round": 1, "director_synthesis": {"recommended_option": "Strategia A"}}]
        }
        bs_tg_summary = bm.format_telegram_summary(bs_fake)
        self.assertTrue("Budget Regime" in bs_tg_summary or "Regime Budget" in bs_tg_summary)
        self.assertTrue("Estimate / Soft Cap" in bs_tg_summary or "Stima / Soft Cap" in bs_tg_summary)


if __name__ == "__main__":
    unittest.main()
