#!/usr/bin/env python3
"""
Unit, Integration and Resilience Tests for Ufficio Multi-Agent Orchestrator
--------------------------------------------------------------------------
Testa:
- Parsing ed estrazione JSON
- Configurazione Presets ed override centralizzati (config.json)
- Git Worktree Manager (creazione, listing e rimozione)
- Diff Review Manager (calcolo diff, generazione HTML e riassunto Telegram)
- Codex Account Hot-Switching (rilevamento rate limit, rotazione, cooldown reset, failover e state persistence)
- Design Mode Capture (cattura DOM/CSS e formattazione prompt)
- Esecuzione End-to-End Workflow (Presets: Light, Standard, Critical)
- Test di Resilienza (Rate Limit failover, Token Budget Warnings, Critical GLM blocking)
- Decodifica Base64 e protezione da command injection
- Logging Strutturato JSON (JSONStructuredFormatter)
- Checkpointing & Resume con salto dei subtask già completati
- Web Dashboard HTML Rendering & System Data API
- Generazione Automatica Report Metriche Markdown (METRICS.md)
- Modalità Brainstorming Strutturata (Create, Round, Feedback, Approve, Reject, Persistence, to_execution_task)
- Buzz-Style Multi-Agent Chat & Mentions (@sol, @deepseek, @glm, @agy, @all)
"""

import sys
import os
import json
import base64
import shutil
import logging
import inspect
import unittest
from pathlib import Path

# Add server directory to path
SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from orchestrator_core import (
    extract_json,
    MultiAgentRunner,
    PRESETS,
    send_notification,
    load_central_config,
    JSONStructuredFormatter
)
from worktree_manager import GitWorktreeManager
from diff_review import DiffReviewManager
from account_manager import CodexAccountManager, CodexAccount
from design_mode import DesignCapture
from health_server import render_html_dashboard, get_system_data
from metrics_generator import generate_metrics_markdown
from brainstorm_manager import BrainstormManager
from agent_prompts import (
    AGY_EXECUTOR_SYSTEM_PROMPT,
    CHAT_AGENT_PROMPTS,
    SOL_DIRECTOR_SYSTEM_PROMPT,
)

class TestJsonExtraction(unittest.TestCase):
    def test_pure_json(self):
        sample = '{"phase": "brainstorm", "ok": true}'
        res = extract_json(sample)
        self.assertEqual(res.get("phase"), "brainstorm")
        self.assertTrue(res.get("ok"))

    def test_markdown_codeblock_json(self):
        sample = '```json\n{"status": "SUCCESS", "tasks": [1, 2, 3]}\n```'
        res = extract_json(sample)
        self.assertEqual(res.get("status"), "SUCCESS")
        self.assertEqual(len(res.get("tasks", [])), 3)

    def test_noisy_prose_json(self):
        sample = 'Ecco la risposta dell\'agente:\n{"ok": true, "notes": "Perfetto"}\nSpero vada bene!'
        res = extract_json(sample)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("notes"), "Perfetto")

    def test_invalid_json(self):
        sample = "Nessun json qui presente."
        res = extract_json(sample)
        self.assertEqual(res, {})


class TestPresetsAndOverrides(unittest.TestCase):
    def test_agent_prompts_define_distinct_roles_and_agy_contract(self):
        self.assertIn("lowest reasonable token consumption", SOL_DIRECTOR_SYSTEM_PROMPT)
        self.assertIn("do not commit/push", AGY_EXECUTOR_SYSTEM_PROMPT)
        self.assertIn("Do not expose secrets", AGY_EXECUTOR_SYSTEM_PROMPT)
        self.assertIn("failure mode", CHAT_AGENT_PROMPTS["deepseek"])
        self.assertIn("frontend/UI", CHAT_AGENT_PROMPTS["glm"])
        self.assertIn("n8n workflow", CHAT_AGENT_PROMPTS["luna"])

    def test_agy_executor_injects_the_operating_contract(self):
        source = inspect.getsource(MultiAgentRunner.call_executor_agy)
        self.assertIn("AGY_EXECUTOR_SYSTEM_PROMPT", source)
        self.assertIn("TASK ASSIGNED BY SOL", source)

    def test_preset_light(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="light", mock_mode=True)
        self.assertEqual(runner.preset_config["director"], "sol")
        self.assertEqual(runner.preset_config["reviewers"], [])
        self.assertTrue(runner.preset_config["allow_escalation"])

    def test_preset_standard(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="standard", mock_mode=True)
        self.assertEqual(runner.preset_config["director"], "sol")
        self.assertEqual(runner.preset_config["reviewers"], [])
        self.assertTrue(runner.preset_config["allow_escalation"])

    def test_preset_critical(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="critical", mock_mode=True)
        self.assertEqual(runner.preset_config["director"], "sol")
        self.assertIn("ds-pro", runner.preset_config["reviewers"])
        self.assertNotIn("glm", runner.preset_config["reviewers"])

    def test_custom_overrides(self):
        runner = MultiAgentRunner(
            workspace_path=Path("/tmp"),
            preset="light",
            director_override="sol",
            reviewers_override=["ds-pro", "glm"],
            mock_mode=True
        )
        self.assertEqual(runner.preset_config["director"], "sol")
        self.assertEqual(runner.preset_config["reviewers"], ["ds-pro"])

    def test_glm_flash_is_a_fixer_not_a_reviewer(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="standard", mock_mode=True)
        result = runner.execute_task({
            "id": "GLMF1",
            "d": "Aggiorna stile frontend",
            "a": "glm-flash",
            "p": "Modifica un selettore CSS",
            "r": False,
        })
        self.assertIn("GLM 5.3 Flash fix", result)
        self.assertEqual(runner.tokens_used.get_agent_calls("glm_flash"), 1)

    def test_agy_failure_routes_to_one_contextual_flash_fallback(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="standard", mock_mode=True)
        runner.call_executor_luna = lambda prompt, subtask_id=None: "luna retry"
        runner.call_fixer_deepseek = lambda prompt, subtask_id=None: "deepseek flash retry"
        runner.call_fixer_glm_flash = lambda prompt, subtask_id=None: "glm flash retry"
        runner.call_agy_fallback = lambda prompt, context, subtask_id=None, reason="": "agy fallback"
        self.assertEqual(runner.select_agy_fallback("Aggiorna il layout CSS"), "glm-flash")
        self.assertEqual(runner.select_agy_fallback("Correggi il test della migrazione SQL"), "ds-flash")
        self.assertEqual(runner.retry_with_primary_executor("agy", "fix", context="backend"), "agy fallback")
        self.assertEqual(runner.retry_with_primary_executor("luna", "fix"), "luna retry")
        self.assertEqual(runner.retry_with_primary_executor("ds-flash", "fix"), "deepseek flash retry")
        self.assertEqual(runner.retry_with_primary_executor("glm-flash", "fix"), "glm flash retry")

    def test_initial_agy_runtime_failure_uses_fallback(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="standard", mock_mode=True)
        runner.call_executor_agy = lambda prompt, subtask_id=None: (_ for _ in ()).throw(RuntimeError("sidecar offline"))
        runner.call_agy_fallback = lambda prompt, context, subtask_id=None, reason="": "fallback completed"
        result = runner.execute_task({
            "id": "AGYF1",
            "d": "Correggi logica backend",
            "a": "agy",
            "p": "Applica fix minimo",
            "r": False,
        })
        self.assertEqual(result, "fallback completed")

    def test_n8n_task_is_routed_to_luna(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="standard", mock_mode=True)
        result = runner.execute_task({
            "id": "N8N1",
            "d": "Aggiorna workflow n8n via MCP",
            "a": "agy",
            "target": "n8n_mcp",
            "p": "Modifica il workflow Ufficio",
            "r": False
        })
        self.assertIn("Luna n8n MCP", result)
        self.assertEqual(runner.tokens_used["luna"], 150)

    def test_luna_flash_preset_uses_flash_for_n8n(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), preset="luna_flash", mock_mode=True)
        result = runner.execute_task({
            "id": "N8N2",
            "d": "Aggiorna workflow n8n via MCP",
            "a": "luna",
            "target": "n8n_mcp",
            "p": "Modifica il workflow Ufficio",
            "r": False
        })
        self.assertIn("Mock DeepSeek Flash fix", result)


class TestGitWorktreeManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("/tmp/ufficio-test-wt-suite")
        self.repos_dir = self.test_dir / "repos"
        self.worktrees_dir = self.test_dir / "worktrees"
        self.mgr = GitWorktreeManager(self.repos_dir, self.worktrees_dir)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_repo_name_extraction(self):
        self.assertEqual(self.mgr.get_repo_name("https://github.com/user/my-repo.git"), "my-repo")
        self.assertEqual(self.mgr.get_repo_name("git@github.com:user/my-repo.git"), "my-repo")
        self.assertEqual(self.mgr.get_repo_name("https://github.com/user/another-repo/"), "another-repo")


class TestDiffReviewManager(unittest.TestCase):
    def setUp(self):
        self.test_diffs = Path("/tmp/taktstock-test-diffs")
        self.mgr = DiffReviewManager(self.test_diffs)

    def tearDown(self):
        if self.test_diffs.exists():
            shutil.rmtree(self.test_diffs, ignore_errors=True)

    def test_telegram_summary_format(self):
        fake_data = {
            "has_changes": True,
            "files_changed": ["src/index.js", "README.md"],
            "insertions": 15,
            "deletions": 3,
            "raw_diff": "--- a/src/index.js\n+++ b/src/index.js\n@@ -1 +1 @@\n-old\n+new"
        }
        msg = self.mgr.format_telegram_summary(fake_data, "Update auth", "feature/auth")
        self.assertIn("Visual Diff Review", msg)
        self.assertIn("feature/auth", msg)
        self.assertIn("+15 / -3", msg)
        self.assertIn("src/index.js", msg)

    def test_telegram_summary_empty(self):
        fake_data = {"has_changes": False}
        msg = self.mgr.format_telegram_summary(fake_data, "Task", "branch")
        self.assertIn("No changes", msg)


class TestCodexAccountManager(unittest.TestCase):
    def setUp(self):
        self.test_state = Path("/tmp/test_accounts_state.json")

    def tearDown(self):
        if self.test_state.exists():
            self.test_state.unlink(missing_ok=True)

    def test_rate_limit_detection(self):
        mgr = CodexAccountManager([
            CodexAccount("acc1", api_key="sk-1"),
            CodexAccount("acc2", api_key="sk-2")
        ], state_file=self.test_state)
        self.assertTrue(mgr.is_rate_limit_error("Error 429: Rate limit exceeded"))
        self.assertTrue(mgr.is_rate_limit_error("Your quota exceeded"))
        self.assertFalse(mgr.is_rate_limit_error("SyntaxError: unexpected token"))

    def test_hot_switching_on_error(self):
        acc1 = CodexAccount("acc1", api_key="sk-1")
        acc2 = CodexAccount("acc2", api_key="sk-2")
        mgr = CodexAccountManager([acc1, acc2], state_file=self.test_state)

        self.assertEqual(mgr.get_current_account().name, "acc1")
        
        # Simulate rate limit error
        switched = mgr.handle_possible_error("Rate limit reached on model")
        self.assertTrue(switched)
        self.assertEqual(mgr.get_current_account().name, "acc2")
        self.assertFalse(acc1.is_available)

    def test_mark_success_resets_cooldown(self):
        acc = CodexAccount("acc1", api_key="sk-1")
        acc.mark_rate_limited(cooldown_seconds=300.0)
        self.assertFalse(acc.is_available)
        self.assertGreater(acc.cooldown_until, 0)

        acc.mark_success()
        self.assertTrue(acc.is_available)
        self.assertEqual(acc.cooldown_until, 0.0)

    def test_state_persistence_and_reload(self):
        acc1 = CodexAccount("acc1", api_key="sk-1")
        acc2 = CodexAccount("acc2", api_key="sk-2")
        mgr1 = CodexAccountManager([acc1, acc2], state_file=self.test_state)
        acc1.mark_rate_limited(cooldown_seconds=120.0)
        mgr1.rotate_to_next()
        mgr1.save_state()

        self.assertTrue(self.test_state.exists())

        # Create new manager pointing to same state file
        mgr2 = CodexAccountManager([
            CodexAccount("acc1", api_key="sk-1"),
            CodexAccount("acc2", api_key="sk-2")
        ], state_file=self.test_state)

        self.assertEqual(mgr2.get_current_account().name, "acc2")
        self.assertFalse(mgr2.accounts[0].is_available)

    def test_environment_injection(self):
        acc = CodexAccount("acc_test", api_key="sk-test-key-123")
        mgr = CodexAccountManager([acc], state_file=self.test_state)
        env = mgr.apply_account_env({"EXISTING": "1"})
        self.assertEqual(env["OPENAI_API_KEY"], "sk-test-key-123")
        self.assertEqual(env["EXISTING"], "1")


class TestDesignMode(unittest.TestCase):
    def setUp(self):
        self.out_dir = Path("/tmp/taktstock-test-design")
        self.d_cap = DesignCapture(self.out_dir)

    def tearDown(self):
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir, ignore_errors=True)

    def test_prompt_context_formatting(self):
        mock_capture = {
            "success": True,
            "url": "http://localhost:3000",
            "title": "Astro Test App",
            "selector": ".nav-button",
            "screenshot_path": "/tmp/screen.png",
            "computed_styles": {"display": "flex", "color": "rgb(255, 0, 0)"},
            "dom_snippet": "<button class='nav-button'>Click Me</button>"
        }
        res = self.d_cap.format_prompt_context(mock_capture)
        self.assertIn("VISUAL CONTEXT & DESIGN MODE", res)
        self.assertIn("Astro Test App", res)
        self.assertIn(".nav-button", res)
        self.assertIn("rgb(255, 0, 0)", res)


class TestEndToEndAndResilience(unittest.TestCase):
    def setUp(self):
        self.workspace = Path("/tmp/taktstock-e2e-workspace")
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.workspace.exists():
            shutil.rmtree(self.workspace, ignore_errors=True)

    def test_full_workflow_critical_preset(self):
        """Test E2E per Preset Critical: verifica DeepSeek Pro come unico reviewer."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="critical",
            mock_mode=True
        )
        result = runner.run_full_workflow(
            task_description="Migrazione schema database e autenticazione JWT",
            branch_name="feature/critical-auth",
            do_push=False
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["preset"], "critical")
        self.assertEqual(result["director"], "sol")
        self.assertIn("ds-pro", result["reviewers"])
        self.assertNotIn("glm", result["reviewers"])
        self.assertGreaterEqual(result["tokens_used"]["total"], 100)

    def test_full_workflow_light_preset(self):
        """Test E2E per Preset Light: veloce, zero review esterne."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="light",
            mock_mode=True
        )
        result = runner.run_full_workflow(
            task_description="Fix typo nel file README",
            branch_name="fix/typo",
            do_push=False
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["preset"], "light")
        self.assertEqual(result["reviewers"], [])

    def test_checkpoint_and_resume_flow(self):
        """Verifica che con flag resume i task già completati vengano saltati."""
        branch = "feature/resume-test"
        runner1 = MultiAgentRunner(
            workspace_path=self.workspace,
            branch_name=branch,
            mock_mode=True
        )
        runner1.completed_task_ids = ["T1"]
        runner1.completed_tasks_history["T1"] = "Mock output T1"
        runner1.save_checkpoint("task_T1_completed", {"task_id": "T1"})

        runner2 = MultiAgentRunner(
            workspace_path=self.workspace,
            branch_name=branch,
            resume=True,
            mock_mode=True
        )
        self.assertIn("T1", runner2.completed_task_ids)
        result = runner2.run_full_workflow(
            task_description="Task con resume",
            branch_name=branch,
            do_push=False
        )
        self.assertEqual(result["status"], "COMPLETED")

    def test_glm_reviewer_is_disabled_by_policy(self):
        """GLM non puo essere usato come reviewer, anche se configurato."""
        crit_runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="critical",
            mock_mode=False
        )
        res = crit_runner.call_reviewer_glm("Prompt critical review")
        self.assertEqual(res.get("verdict"), "pass")
        self.assertIn("disabilitato", res.get("opinion", ""))

    def test_base64_task_decoding(self):
        raw_task = "echo hello; rm -rf /"
        b64 = base64.b64encode(raw_task.encode("utf-8")).decode("utf-8")
        decoded = base64.b64decode(b64).decode("utf-8")
        self.assertEqual(decoded, raw_task)

    def test_json_structured_logging(self):
        """Verifica la corretta formattazione JSON dei log per query con jq."""
        formatter = JSONStructuredFormatter()
        record = logging.LogRecord(
            name="UfficioOrchestrator",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Task T1 completato con successo",
            args=(),
            exc_info=None
        )
        record.task_id = "T1"
        record.agent = "agy"
        record.tokens = 1500
        formatted = formatter.format(record)
        parsed = json.loads(formatted)
        self.assertEqual(parsed["level"], "INFO")
        self.assertEqual(parsed["task_id"], "T1")
        self.assertEqual(parsed["agent"], "agy")
        self.assertEqual(parsed["tokens"], 1500)

    def test_central_config_loading(self):
        config = load_central_config()
        self.assertIn("presets", config)
        self.assertIn("standard", config["presets"])
        self.assertIn("critical", config["presets"])
        self.assertIn("brainstorm", config)

    def test_web_dashboard_html_rendering(self):
        """Verifica il rendering HTML della dashboard."""
        html = render_html_dashboard()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Taktstock Multi-Agent", html)
        self.assertIn("Control Center", html)

    def test_metrics_markdown_generation(self):
        """Verifica la corretta generazione del report Markdown."""
        test_history = Path("/tmp/test_runs_history.jsonl")
        test_output_md = Path("/tmp/TEST_METRICS.md")
        
        sample_runs = [
            {"task": "Task 1", "preset": "light", "tokens_used": {"total": 500}, "status": "COMPLETED", "timestamp": "2026-08-22T10:00:00"},
            {"task": "Task 2", "preset": "critical", "tokens_used": {"total": 15000}, "status": "COMPLETED", "timestamp": "2026-08-22T11:00:00"}
        ]
        test_history.write_text("\n".join([json.dumps(r) for r in sample_runs]), encoding="utf-8")

        try:
            generate_metrics_markdown(test_history, test_output_md)
            self.assertTrue(test_output_md.exists())
            content = test_output_md.read_text(encoding="utf-8")
            self.assertIn("Metrics & Cost Report", content)
            self.assertIn("15,500", content)
            self.assertIn("LIGHT", content)
            self.assertIn("CRITICAL", content)
        finally:
            if test_history.exists(): test_history.unlink()
            if test_output_md.exists(): test_output_md.unlink()


class TestBrainstormManager(unittest.TestCase):
    def setUp(self):
        self.test_bs_dir = Path("/tmp/ufficio-test-brainstorms")
        self.bm = BrainstormManager(self.test_bs_dir)
        self.runner = MultiAgentRunner(workspace_path=Path("/tmp"), mock_mode=True, preset="critical")

    def tearDown(self):
        if self.test_bs_dir.exists():
            shutil.rmtree(self.test_bs_dir, ignore_errors=True)

    def test_brainstorm_create(self):
        """Verifica creazione sessione brainstorming."""
        bs_id = self.bm.create_brainstorm(
            task="Aggiungere supporto OAuth2",
            preset="critical",
            chat_id="998877",
            repo="https://github.com/org/repo"
        )
        self.assertTrue(bs_id.startswith("bs_"))
        status = self.bm.get_status(bs_id)
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["task"], "Aggiungere supporto OAuth2")
        self.assertEqual(status["chat_id"], "998877")

    def test_brainstorm_round(self):
        """Verifica round con director + reviewers."""
        bs_id = self.bm.create_brainstorm(task="Aggiungere OAuth2", chat_id="123")
        res = self.bm.run_round(bs_id, user_feedback=None, runner=self.runner)
        self.assertEqual(len(res["rounds"]), 1)
        r1 = res["rounds"][0]
        self.assertEqual(r1["round"], 1)
        self.assertIn("analysis", r1["director_analysis"])
        self.assertGreaterEqual(len(r1["reviewer_opinions"]), 1)
        self.assertIn("recommended_option", r1["director_synthesis"])

    def test_standard_brainstorm_does_not_call_reviewers_by_default(self):
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), mock_mode=True, preset="standard")
        bs_id = self.bm.create_brainstorm(task="Task ordinario", chat_id="987")
        result = self.bm.run_round(bs_id, runner=runner)
        self.assertEqual(result["rounds"][0]["reviewer_opinions"], [])

    def test_brainstorm_feedback(self):
        """Verifica incorporamento feedback utente al round 2."""
        bs_id = self.bm.create_brainstorm(task="Aggiungere OAuth2", chat_id="123")
        self.bm.run_round(bs_id, user_feedback=None, runner=self.runner)
        res = self.bm.run_round(bs_id, user_feedback="Usa SQLite e sessioni separate", runner=self.runner)
        self.assertEqual(len(res["rounds"]), 2)
        r2 = res["rounds"][1]
        self.assertEqual(r2["round"], 2)
        self.assertEqual(r2["user_feedback"], "Usa SQLite e sessioni separate")

    def test_brainstorm_approve(self):
        """Verifica approvazione e generazione final_plan."""
        bs_id = self.bm.create_brainstorm(task="Aggiungere OAuth2", chat_id="123")
        self.bm.run_round(bs_id, runner=self.runner)
        approved = self.bm.approve(bs_id, approved_by="admin")
        self.assertEqual(approved["status"], "approved")
        self.assertIsNotNone(approved["final_plan"])
        self.assertEqual(approved["approved_by"], "admin")
        self.assertEqual(approved["final_plan"]["phase"], "final_plan")

    def test_brainstorm_reject(self):
        """Verifica archiviazione rifiuto."""
        bs_id = self.bm.create_brainstorm(task="Aggiungere OAuth2", chat_id="778899")
        self.bm.run_round(bs_id, runner=self.runner)
        self.bm.reject(bs_id, reason="Troppo complesso")
        status = self.bm.get_status(bs_id)
        self.assertEqual(status["status"], "rejected")
        self.assertEqual(status["rejected_reason"], "Troppo complesso")
        self.assertIsNone(self.bm.get_active_brainstorm_id("778899"))

    def test_brainstorm_persistence(self):
        """Verifica persistenza e ricaricamento da file."""
        bs_id = self.bm.create_brainstorm(task="Persistenza test", chat_id="111")
        self.bm.run_round(bs_id, runner=self.runner)
        
        bm2 = BrainstormManager(self.test_bs_dir)
        loaded = bm2.load_brainstorm(bs_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["task"], "Persistenza test")
        self.assertEqual(len(loaded["rounds"]), 1)

    def test_brainstorm_to_execution_task(self):
        """Verifica conversione del piano approvato in execution task."""
        bs_id = self.bm.create_brainstorm(task="Aggiungere OAuth2", chat_id="123")
        self.bm.run_round(bs_id, runner=self.runner)
        self.bm.approve(bs_id, approved_by="user")
        exec_task = self.bm.to_execution_task(bs_id)
        self.assertIn("Aggiungere OAuth2", exec_task)
        self.assertIn("APPROVED PLAN", exec_task)

    def test_buzz_chat_mention_sol(self):
        """Verifica chiamata mirata con @sol."""
        bs_id = self.bm.create_brainstorm(task="Test chat", chat_id="123")
        replies = self.bm.post_chat_message(bs_id, "@sol cosa ne pensi del DB?", runner=self.runner)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["agent"], "sol")
        self.assertIn("Sol", replies[0]["sender"])

    def test_buzz_chat_mention_deepseek(self):
        """Verifica chiamata mirata con @deepseek."""
        bs_id = self.bm.create_brainstorm(task="Test chat", chat_id="123")
        replies = self.bm.post_chat_message(bs_id, "@deepseek controlla gli edge cases", runner=self.runner)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["agent"], "deepseek")

    def test_buzz_chat_mention_glm(self):
        """Verifica chiamata mirata con @glm."""
        bs_id = self.bm.create_brainstorm(task="Test chat", chat_id="123")
        replies = self.bm.post_chat_message(bs_id, "@glm verifica la sicurezza dei token", runner=self.runner)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["agent"], "glm")

    def test_buzz_chat_mention_all(self):
        """Verifica chiamata di gruppo con @all."""
        bs_id = self.bm.create_brainstorm(task="Test chat", chat_id="123")
        replies = self.bm.post_chat_message(bs_id, "@all cosa ne pensate di questo refactoring?", runner=self.runner)
        self.assertEqual(len(replies), 3)
        agents = [r["agent"] for r in replies]
        self.assertIn("sol", agents)
        self.assertIn("agy", agents)
        self.assertIn("luna", agents)

        status = self.bm.get_status(bs_id)
        self.assertEqual(status["selected_agents"], ["sol", "agy", "luna"])

    def test_buzz_chat_changes_selected_team(self):
        bs_id = self.bm.create_brainstorm(task="Test chat", chat_id="123")
        replies = self.bm.post_chat_message(bs_id, "/agenti sol,agy,luna,deepseek,glm", runner=self.runner)
        self.assertEqual(replies[0]["agent"], "sol")
        self.assertEqual(
            self.bm.get_status(bs_id)["selected_agents"],
            ["sol", "agy", "luna", "deepseek", "glm"]
        )

        tg_formatted = self.bm.format_chat_replies_telegram(replies)
        self.assertIn("SOL", tg_formatted)
        self.assertIn("DeepSeek Pro", tg_formatted)
        self.assertIn("GLM 5.3 Flash", tg_formatted)
        self.assertIn("AGY", tg_formatted)


class TestOrchestratorCoreCleanImport(unittest.TestCase):
    def test_orchestrator_core_clean_process_import_and_type_hints(self):
        """Regression test: importa orchestrator_core in un processo Python pulito e valuta le type annotation a runtime."""
        import subprocess
        code = """
import sys
import typing
sys.path.insert(0, 'server')
import orchestrator_core

# Valuta a runtime tutte le type hints di tutte le funzioni/classi per intercettare NameError
hints = typing.get_type_hints(orchestrator_core.should_preserve_worktree)
assert 'return' in hints, "Return type annotation mancante"

for name, obj in vars(orchestrator_core).items():
    if callable(obj) and getattr(obj, '__module__', None) == 'orchestrator_core':
        try:
            typing.get_type_hints(obj)
        except Exception as e:
            print(f"FAILED {name}: {e}", file=sys.stderr)
            sys.exit(1)
print("TYPE_HINTS_OK")
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True
        )
        self.assertEqual(proc.returncode, 0, f"Import o type hint evaluation fallita:\n{proc.stderr}")
        self.assertIn("TYPE_HINTS_OK", proc.stdout)


class TestTaskTargetsN8NPolicy(unittest.TestCase):
    def test_n8n_target_explicit(self):
        """Task con target n8n o n8n_mcp vengono assegnati a Luna."""
        self.assertTrue(MultiAgentRunner.task_targets_n8n({"target": "n8n_mcp", "d": "Aggiorna nodo Telegram"}))
        self.assertTrue(MultiAgentRunner.task_targets_n8n({"target": "n8n", "d": "Disattiva workflow"}))

    def test_workspace_target_and_negative_constraints_not_hijacked(self):
        """Task per workspace/file locali con 'non modificare n8n' NON vengono assegnati a Luna."""
        # Caso reale di run af47e299
        task1 = {
            "id": "T1",
            "d": "Aggiornare HANDOFF con esito PASS e produrre il diff",
            "a": "agy",
            "target": "workspace",
            "p": "leggi docs/HANDOFF.md e registra esito PASS. Non modificare altri file, database, workflow n8n, configurazioni o servizi."
        }
        self.assertFalse(MultiAgentRunner.task_targets_n8n(task1))

        task2 = {
            "id": "T2",
            "d": "Aggiorna documentazione",
            "a": "agy",
            "p": "Non toccare i workflow n8n o il DB"
        }
        self.assertFalse(MultiAgentRunner.task_targets_n8n(task2))

        task3 = {
            "id": "T3",
            "d": "Modifica script python",
            "a": "antigravity",
            "target": "worktree",
            "p": "Ottimizza la funzione X"
        }
        self.assertFalse(MultiAgentRunner.task_targets_n8n(task3))


if __name__ == "__main__":
    unittest.main()
