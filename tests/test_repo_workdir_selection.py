#!/usr/bin/env python3
"""
Unit Tests for Repository Selection, Workdir Configuration and Sandboxed Execution
---------------------------------------------------------------------------------
Testa:
1. Risoluzione corretta del repository Assistente tramite ProjectsManager (per nome, alias, path)
2. Inizializzazione corretta di MultiAgentRunner con workspace impostato sulla directory del progetto
3. Invocazione di call_codex_profile con cwd=self.workspace, Landlock sandbox e skip-git-repo-check
4. Invocazione di call_executor_agy con cwd=self.workspace
5. Divieto categorico di fallback a server/ quando è specificato un repository valido
"""

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from projects_manager import ProjectsManager
from orchestrator_core import MultiAgentRunner


class TestRepoAndWorkdirSelection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.progetti_dir = Path(self.temp_dir.name) / "progetti"
        self.progetti_dir.mkdir(parents=True, exist_ok=True)

        # Crea la cartella del progetto Assistente con un file fittizio
        self.assistente_dir = self.progetti_dir / "Assistente"
        self.assistente_dir.mkdir(parents=True, exist_ok=True)
        (self.assistente_dir / "AGENTS.md").write_text("# Assistente Project", encoding="utf-8")
        (self.assistente_dir / "package.json").write_text('{"name": "assistente", "description": "Assistente AI"}', encoding="utf-8")

        self.pm = ProjectsManager(base_dir=self.progetti_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_find_project_by_exact_name(self):
        """1. ProjectsManager trova Assistente per nome esatto."""
        p = self.pm.find_project("Assistente")
        self.assertIsNotNone(p)
        self.assertEqual(p["name"], "Assistente")
        self.assertEqual(Path(p["path"]).resolve(), self.assistente_dir.resolve())

    def test_find_project_by_alias(self):
        """2. ProjectsManager trova Assistente tramite alias minuscolo o repo:."""
        p_alias = self.pm.find_project("assistente")
        self.assertIsNotNone(p_alias)
        self.assertEqual(p_alias["name"], "Assistente")

        p_repo = self.pm.find_project("repo:Assistente")
        self.assertIsNotNone(p_repo)
        self.assertEqual(p_repo["name"], "Assistente")

    def test_find_project_by_full_path(self):
        """3. ProjectsManager trova Assistente tramite full path."""
        p = self.pm.find_project(str(self.assistente_dir))
        self.assertIsNotNone(p)
        self.assertEqual(p["name"], "Assistente")

    def test_runner_initializes_with_project_workspace_and_not_server_fallback(self):
        """4. MultiAgentRunner imposta self.workspace sulla cartella progetto, mai server/."""
        runner = MultiAgentRunner(
            workspace_path=self.assistente_dir,
            mock_mode=True
        )
        self.assertEqual(runner.workspace.resolve(), self.assistente_dir.resolve())
        self.assertNotEqual(runner.workspace.resolve(), SERVER_DIR.resolve())

    def test_call_codex_profile_uses_project_workspace_and_landlock_sandbox(self):
        """5. call_codex_profile passa cwd=self.workspace e flag sandbox minimi privilegi."""
        runner = MultiAgentRunner(
            workspace_path=self.assistente_dir,
            mock_mode=False
        )

        with patch.object(runner, "run_cmd", return_value='{"ok": true}') as mock_run_cmd:
            runner.call_codex_profile("sol", "Analizza il codice", reasoning_effort="low")

            mock_run_cmd.assert_called_once()
            called_cmd = mock_run_cmd.call_args[0][0]
            called_kwargs = mock_run_cmd.call_args[1]

            # Verifica che cwd sia self.workspace (Assistente)
            self.assertEqual(called_kwargs.get("cwd").resolve(), self.assistente_dir.resolve())

            # Verifica la presenza dei flag di sicurezza e sandboxing
            self.assertIn("--enable", called_cmd)
            self.assertIn("use_legacy_landlock", called_cmd)
            self.assertIn("--skip-git-repo-check", called_cmd)
            self.assertIn("-s", called_cmd)
            self.assertIn("read-only", called_cmd)
            self.assertIn('-c', called_cmd)
            self.assertIn('model_reasoning_effort="low"', called_cmd)

    def test_call_executor_agy_passes_workspace_to_gateway(self):
        """6. call_executor_agy passa self.workspace come worktree_path ad AgentGateway."""
        runner = MultiAgentRunner(
            workspace_path=self.assistente_dir,
            mock_mode=False
        )

        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call") as mock_gw:
            mock_gw.return_value = (True, "OK", {})
            runner.call_executor_agy("Esegui task")

            mock_gw.assert_called_once()
            call_kwargs = mock_gw.call_args[1]
            self.assertEqual(
                Path(call_kwargs.get("worktree_path")).resolve(),
                self.assistente_dir.resolve()
            )


    def test_docker_compose_hardened_no_ssh_no_gemini_no_unmasked_progetti(self):
        """7. Verifica che docker-compose.yml non monti ~/.ssh, ~/.gemini o /home/massimo/progetti globale."""
        compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
        if compose_path.exists():
            content = compose_path.read_text(encoding="utf-8")
            self.assertNotIn(".ssh_host", content)
            self.assertNotIn("/root/.ssh", content)
            self.assertNotIn("/root/.gemini", content)
            self.assertNotIn("- /home/massimo/progetti:", content)

    def test_call_codex_profile_workspace_write_when_explicitly_requested(self):
        """8. call_codex_profile imposta workspace-write quando richiesto da modifiche approvate."""
        runner = MultiAgentRunner(
            workspace_path=self.assistente_dir,
            mock_mode=False
        )
        runner.allow_workspace_write = True

        with patch.object(runner, "run_cmd", return_value='{"ok": true}') as mock_run_cmd:
            runner.call_codex_profile("sol", "Modifica codice approvata", sandbox_mode="workspace-write")

            mock_run_cmd.assert_called_once()
            called_cmd = mock_run_cmd.call_args[0][0]
            self.assertIn("-s", called_cmd)
            self.assertIn("workspace-write", called_cmd)


if __name__ == "__main__":
    unittest.main()
