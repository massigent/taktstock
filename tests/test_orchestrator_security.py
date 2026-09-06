#!/usr/bin/env python3
"""
Unit & Integration Tests for Orchestrator Execution Security, Worktree Isolation & Cleanup
-----------------------------------------------------------------------------------------
Verifica:
1. Assenza totale del flag --dangerously-skip-permissions da orchestrator_core.py e orchestrator.sh
2. Disabilitazione di default del git push (push solo su do_push=True esplicito)
3. Preservazione del worktree e stato WAITING_FOR_APPROVAL con modifiche tracked, staged e untracked
4. Aggiunta esplicita dei soli file revisionati (nessun uso di 'git add .')
5. Isolamento totale dei test: nessuna scrittura nella directory di stato reale o sotto HOME
6. Preservazione del worktree in caso di eccezione/crash e simulazione completa del cleanup di main()
7. Corretto parsing di rename/copy in porcelain -z (dest_path)
8. Statistiche diff per staged e untracked senza falsi diff sintetici
9. Fail-safe: preservazione del worktree in caso di errore nel comando Git
"""

import os
import sys
import shutil
import inspect
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from orchestrator_core import (
    MultiAgentRunner,
    parse_args,
    should_preserve_worktree,
    STATE_DIR,
)
from diff_review import DiffReviewManager
from worktree_manager import GitWorktreeManager


def setup_temp_git_repo(path: Path) -> Path:
    """Inizializza un repository Git reale in una directory temporanea."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@runner.local"], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("# Project\nInitial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=str(path), check=True, capture_output=True)
    return path


class TestAgyInvocationSafety(unittest.TestCase):
    def test_no_dangerously_skip_permissions_in_orchestrator_core(self):
        """Verifica che il metodo call_executor_agy non contenga il flag --dangerously-skip-permissions."""
        source = inspect.getsource(MultiAgentRunner.call_executor_agy)
        self.assertNotIn("--dangerously-skip-permissions", source)

    def test_no_dangerously_skip_permissions_in_orchestrator_sh(self):
        """Verifica che lo script orchestrator.sh non contenga il flag --dangerously-skip-permissions."""
        sh_path = Path(__file__).resolve().parent.parent / "orchestrator.sh"
        if sh_path.exists():
            content = sh_path.read_text(encoding="utf-8")
            self.assertNotIn("--dangerously-skip-permissions", content)

    def test_call_executor_agy_source_has_no_local_agy_invocation(self):
        """Verifica statica: il source di call_executor_agy non deve contenere costruzione
        di comando agy locale né self.run_cmd."""
        source = inspect.getsource(MultiAgentRunner.call_executor_agy)
        # '["agy",' identifica la costruzione di una command list locale: ["agy", "-p", ...]
        self.assertNotIn('["agy",', source, "Trovata costruzione di comando agy locale nel source")
        self.assertNotIn("self.run_cmd", source, "Trovato self.run_cmd nel source di call_executor_agy")
        self.assertNotIn("--dangerously-skip-permissions", source)


    def test_call_executor_agy_legacy_mode_raises_runtime_error_without_subprocess(self):
        """LEGACY_MODE (sidecar disabilitato) deve sollevare RuntimeError senza subprocess."""
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), mock_mode=False)
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call") as mock_gw:
            mock_gw.return_value = (False, "LEGACY_MODE", {})
            with patch("subprocess.run") as mock_subproc:
                with patch.object(runner, "run_cmd") as mock_run_cmd:
                    with self.assertRaises(RuntimeError) as cm:
                        runner.call_executor_agy("Test prompt")
                    self.assertIn("Fallimento esecuzione AGY", str(cm.exception))
                    mock_subproc.assert_not_called()
                    mock_run_cmd.assert_not_called()

    def test_call_executor_agy_import_error_raises_runtime_error(self):
        """Se l'import di AgentGateway fallisce, deve sollevare RuntimeError senza fallback locale."""
        runner = MultiAgentRunner(workspace_path=Path("/tmp"), mock_mode=False)
        with patch("infrastructure.agent_gateway.AgentGateway.execute_agent_call",
                   side_effect=ImportError("No module named 'infrastructure.agent_gateway'")):
            with patch("subprocess.run") as mock_subproc:
                with patch.object(runner, "run_cmd") as mock_run_cmd:
                    with self.assertRaises(RuntimeError):
                        runner.call_executor_agy("Test prompt")
                    mock_subproc.assert_not_called()
                    mock_run_cmd.assert_not_called()


class TestWorktreeSecurityAndIsolation(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="taktstock_sec_test_"))
        self.workspace = self.test_dir / "workspace"
        self.workspace.mkdir()
        self.state_dir = self.test_dir / "state"
        self.state_dir.mkdir()
        self.diffs_dir = self.test_dir / "diffs"
        self.diffs_dir.mkdir()
        self.diff_manager = DiffReviewManager(self.diffs_dir)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_run_full_workflow_default_do_push_is_false(self):
        """La signature di run_full_workflow deve avere do_push=False di default."""
        sig = inspect.signature(MultiAgentRunner.run_full_workflow)
        self.assertIn("do_push", sig.parameters)
        self.assertEqual(sig.parameters["do_push"].default, False)

    def test_cli_parser_defaults_push_to_false(self):
        """Il parser CLI deve avere il push disabilitato di default."""
        with patch("sys.argv", ["orchestrator_core.py", "test_task"]):
            args = parse_args()
            self.assertFalse(args.push)

    def test_should_preserve_worktree_function_logic(self):
        """Test unitario per la funzione decisionale should_preserve_worktree."""
        setup_temp_git_repo(self.workspace)

        # 1. Clean workspace senza approval o keep -> False
        preserve, reason = should_preserve_worktree(self.workspace, result={"status": "COMPLETED"}, keep_requested=False)
        self.assertFalse(preserve)

        # 2. Keep esplicito -> True
        preserve, reason = should_preserve_worktree(self.workspace, result=None, keep_requested=True)
        self.assertTrue(preserve)
        self.assertIn("Keep", reason)

        # 3. Stato WAITING_FOR_APPROVAL -> True
        preserve, reason = should_preserve_worktree(self.workspace, result={"status": "WAITING_FOR_APPROVAL"}, keep_requested=False)
        self.assertTrue(preserve)
        self.assertIn("WAITING_FOR_APPROVAL", reason)

        # 4. Modifiche non committate (dirty) -> True anche se result è None (dopo crash/eccezione)
        (self.workspace / "dirty_file.txt").write_text("modifica\n", encoding="utf-8")
        preserve, reason = should_preserve_worktree(self.workspace, result=None, keep_requested=False)
        self.assertTrue(preserve)
        self.assertIn("fail-safe", reason)

    def test_should_preserve_worktree_failsafe_on_git_error(self):
        """Se il controllo git status fallisce o incontra errore in un repo Git, preserva il worktree per sicurezza."""
        setup_temp_git_repo(self.workspace)
        with patch("subprocess.run", side_effect=RuntimeError("Git subprocess failure")):
            preserve, reason = should_preserve_worktree(self.workspace, result=None, keep_requested=False)
            self.assertTrue(preserve)
            self.assertTrue(GitWorktreeManager.is_worktree_dirty(self.workspace))

    def test_integration_tracked_file_modified_preserves_worktree(self):
        """Integrazione Git reale: modifica a file tracked imposta WAITING_FOR_APPROVAL e isola lo stato."""
        setup_temp_git_repo(self.workspace)
        (self.workspace / "README.md").write_text("# Project Updated\nModifica tracked\n", encoding="utf-8")

        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            diff_manager=self.diff_manager,
            state_dir=self.state_dir,
            preset="light",
            mock_mode=True
        )

        result = runner.run_full_workflow(
            task_description="Aggiorna README",
            branch_name="feature/tracked-test",
            do_push=False
        )

        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(result["workspace"], str(self.workspace))
        self.assertIn("README.md", result["files_changed"])

        # Verifica isolamento totale: lo stato è stato scritto in self.state_dir e NON nella home reale
        pending_file = self.state_dir / "pending_feature_tracked-test.json"
        self.assertTrue(pending_file.exists(), "Il pending file deve essere scritto nel test state_dir isolato")

    def test_integration_untracked_file_created_preserves_worktree(self):
        """Integrazione Git reale: nuovo file untracked imposta WAITING_FOR_APPROVAL e dirty=True."""
        setup_temp_git_repo(self.workspace)
        (self.workspace / "new_service.py").write_text("print('Nuovo modulo')\n", encoding="utf-8")

        self.assertTrue(GitWorktreeManager.is_worktree_dirty(self.workspace))

        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            diff_manager=self.diff_manager,
            state_dir=self.state_dir,
            preset="light",
            mock_mode=True
        )

        result = runner.run_full_workflow(
            task_description="Crea nuovo servizio",
            branch_name="feature/untracked-test",
            do_push=False
        )

        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertIn("new_service.py", result["files_changed"])
        self.assertTrue(GitWorktreeManager.is_worktree_dirty(self.workspace))

    def test_integration_git_rename_file_detects_new_path(self):
        """Integrazione Git reale: git status -z su rename estrae e conserva il nuovo percorso di destinazione."""
        setup_temp_git_repo(self.workspace)
        (self.workspace / "old_module.py").write_text("def old(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "old_module.py"], cwd=str(self.workspace), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_module"], cwd=str(self.workspace), check=True, capture_output=True)

        # Rinomina con git mv
        subprocess.run(["git", "mv", "old_module.py", "renamed_module.py"], cwd=str(self.workspace), check=True, capture_output=True)

        status_info = self.diff_manager.get_status_files(self.workspace)
        diff_data = self.diff_manager.get_diff_data(self.workspace)

        self.assertIn("renamed_module.py", status_info["all"])
        self.assertIn("renamed_module.py", diff_data["files_changed"])
        self.assertNotIn("old_module.py", diff_data["files_changed"])

    def test_integration_git_staged_file_detected(self):
        """Integrazione Git reale: file staged inclusi correttamente in get_status_files e get_diff_data."""
        setup_temp_git_repo(self.workspace)
        (self.workspace / "staged_file.py").write_text("print('staged')\n", encoding="utf-8")
        subprocess.run(["git", "add", "staged_file.py"], cwd=str(self.workspace), check=True, capture_output=True)

        status_info = self.diff_manager.get_status_files(self.workspace)
        diff_data = self.diff_manager.get_diff_data(self.workspace)

        self.assertIn("staged_file.py", status_info["staged"])
        self.assertIn("staged_file.py", diff_data["files_changed"])
        self.assertTrue(diff_data["has_changes"])

    def test_integration_untracked_file_no_fake_raw_diff(self):
        """Verifica che per gli untracked non vengano inventati finti blocchi di codice nel raw diff."""
        setup_temp_git_repo(self.workspace)
        (self.workspace / "notes.txt").write_text("Line 1\nLine 2\n", encoding="utf-8")

        diff_data = self.diff_manager.get_diff_data(self.workspace)
        self.assertIn("notes.txt", diff_data["files_changed"])
        self.assertIn("untracked", diff_data["stat"])
        # Non deve contenere falsi diff sintetici inventati che sembrino codice diff reale
        self.assertNotIn("+notes.txt (nuovo file)", diff_data["raw_diff"])

    def test_main_cleanup_simulation_after_exception_dirty_worktree_preserved(self):
        """Simulazione completa del blocco finally di main(): dopo un'eccezione il worktree dirty NON viene rimosso."""
        setup_temp_git_repo(self.workspace)
        (self.workspace / "critical_work.py").write_text("# Lavoro non committato\n", encoding="utf-8")

        # Mock worktree manager
        mock_wt_manager = MagicMock(spec=GitWorktreeManager)
        repo_master_path = self.test_dir / "master_repo"

        # Simula il verificarsi di un'eccezione durante run_full_workflow
        result = None
        exception_occurred = False
        try:
            raise RuntimeError("Eccezione imprevista durante l'orchestrazione")
        except Exception:
            exception_occurred = True

        self.assertTrue(exception_occurred)

        # Logica del finally di main()
        res_obj = result if ('result' in locals() and isinstance(result, dict)) else None
        should_preserve, reason = should_preserve_worktree(
            workspace_path=self.workspace,
            result=res_obj,
            keep_requested=False
        )

        self.assertTrue(should_preserve, "Il worktree dirty deve essere preservato anche se result è None")

        if not should_preserve:
            mock_wt_manager.remove_worktree(repo_master_path, self.workspace, force=True)

        # Verifica che remove_worktree NON sia stato chiamato e che il file sia intatto
        mock_wt_manager.remove_worktree.assert_not_called()
        self.assertTrue((self.workspace / "critical_work.py").exists())

    def test_explicit_file_git_add_used_instead_of_git_add_all(self):
        """Verifica che durante il commit approvato vengano aggiunti solo i singoli file e MAI 'git add .'"""
        setup_temp_git_repo(self.workspace)
        (self.workspace / "app.py").write_text("# app code\n", encoding="utf-8")

        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            diff_manager=self.diff_manager,
            state_dir=self.state_dir,
            preset="light",
            mock_mode=False
        )

        executed_cmds = []

        def mock_run_cmd(cmd):
            executed_cmds.append(cmd)
            if "status" in cmd:
                return "?? app.py"
            return "ok"

        runner.run_cmd = mock_run_cmd
        runner.brainstorm = MagicMock(return_value={})
        runner.decompose = MagicMock(return_value=[])
        runner.validate = MagicMock(return_value={"status": "done"})

        with patch("orchestrator_core.check_git_preflight_sync", return_value=(True, None, "init_hash")):
            result = runner.run_full_workflow(
                task_description="Task approvato",
                branch_name="feature/approved-test",
                do_push=True
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn(["git", "add", "."], executed_cmds)
        self.assertIn(["git", "add", "--", "app.py"], executed_cmds)


if __name__ == "__main__":
    unittest.main()
