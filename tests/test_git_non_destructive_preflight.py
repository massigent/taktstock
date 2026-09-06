#!/usr/bin/env python3
"""
Unit and Regression Tests for:
Git Non-Destructive Preflight for Selected Project (Taktstock Orchestrator)
- Verifies clean repository status (no uncommitted/untracked files)
- Executes 'git fetch --prune origin'
- Updates exclusively with 'git pull --ff-only'
- Blocks with BLOCKED_GIT_SYNC on dirty / ahead / divergent branches with clear instructions
- Never uses reset, stash, clean, or force
- Records initial and final commit in report summaries and Telegram messages
- Runs in isolated temporary state/repo directories
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
    check_git_preflight_sync,
    format_workflow_telegram_message,
    TokensDict
)


class TestGitNonDestructivePreflight(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="taktstock_git_preflight_test_")
        self.remote_dir = Path(self.temp_dir) / "remote_origin.git"
        self.local_repo = Path(self.temp_dir) / "local_repo"
        self.state_dir = Path(self.temp_dir) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # 1. Inizializza repository bare remoto (origin) con HEAD su refs/heads/main
        subprocess.run(["git", "init", "--bare", str(self.remote_dir)], capture_output=True, check=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=str(self.remote_dir), capture_output=True, check=True)

        # 2. Inizializza repository locale e primo commit su main
        self.local_repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(self.local_repo), capture_output=True, check=True)
        subprocess.run(["git", "checkout", "-B", "main"], cwd=str(self.local_repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(self.local_repo), check=True)
        subprocess.run(["git", "config", "user.email", "test@taktstock.local"], cwd=str(self.local_repo), check=True)
        (self.local_repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.local_repo), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.local_repo), check=True)

        # Collega a remote origin e push main
        subprocess.run(["git", "remote", "add", "origin", str(self.remote_dir)], cwd=str(self.local_repo), check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(self.local_repo), check=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_head(self, repo_path: Path) -> str:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_path), capture_output=True, text=True, check=True)
        return res.stdout.strip()

    def test_preflight_passes_on_clean_and_synced_repo(self):
        """1. Preflight ha successo su un repository pulito e allineato a origin."""
        ok, err, init_c = check_git_preflight_sync(self.local_repo, branch="main")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(init_c, self._get_head(self.local_repo))

    def test_preflight_blocks_when_repo_is_dirty_with_untracked_files(self):
        """2. Preflight blocca con BLOCKED_GIT_SYNC se sono presenti file untracked, senza cancellarli."""
        untracked_file = self.local_repo / "untracked_script.py"
        untracked_file.write_text("print('dirty')\n", encoding="utf-8")

        ok, err, init_c = check_git_preflight_sync(self.local_repo, branch="main")
        self.assertFalse(ok)
        self.assertIn("BLOCKED_GIT_SYNC", err)
        self.assertIn("untracked_script.py", err)
        self.assertIn("Istruzioni per sbloccare", err)

        # Verifica non distruttività: il file DEVE essere ancora presente su disco
        self.assertTrue(untracked_file.exists(), "Il preflight NON deve eliminare i file locali (no clean)!")

    def test_preflight_blocks_when_repo_is_dirty_with_modified_files(self):
        """3. Preflight blocca con BLOCKED_GIT_SYNC se ci sono modifiche non committate, senza fare reset o stash."""
        readme = self.local_repo / "README.md"
        readme.write_text("# Test Repo (Uncommitted changes)\n", encoding="utf-8")

        ok, err, init_c = check_git_preflight_sync(self.local_repo, branch="main")
        self.assertFalse(ok)
        self.assertIn("BLOCKED_GIT_SYNC", err)
        self.assertIn("README.md", err)

        # Verifica non distruttività: le modifiche NON devono essere cancellate (no reset/stash)
        self.assertEqual(readme.read_text(encoding="utf-8"), "# Test Repo (Uncommitted changes)\n")

    def test_preflight_fast_forwards_behind_branch_with_ff_only(self):
        """4. Preflight esegue 'git fetch --prune origin' e aggiorna solo con 'git pull --ff-only' quando behind."""
        # Crea un secondo clone per spingere un nuovo commit su origin
        second_clone = Path(self.temp_dir) / "second_clone"
        subprocess.run(["git", "clone", "-b", "main", str(self.remote_dir), str(second_clone)], capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Peer Dev"], cwd=str(second_clone), check=True)
        subprocess.run(["git", "config", "user.email", "peer@taktstock.local"], cwd=str(second_clone), check=True)
        (second_clone / "feature.txt").write_text("Remote feature\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.txt"], cwd=str(second_clone), check=True)
        subprocess.run(["git", "commit", "-m", "remote commit"], cwd=str(second_clone), check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=str(second_clone), check=True)

        new_remote_head = self._get_head(second_clone)
        old_local_head = self._get_head(self.local_repo)
        self.assertNotEqual(old_local_head, new_remote_head)

        # Esegui preflight sul repo locale: deve fare fetch e pull --ff-only
        ok, err, init_c = check_git_preflight_sync(self.local_repo, branch="main")
        self.assertTrue(ok)
        self.assertIsNone(err)

        # Verifica che il repo locale sia stato aggiornato al nuovo HEAD
        self.assertEqual(self._get_head(self.local_repo), new_remote_head)
        self.assertEqual(init_c, new_remote_head)
        self.assertTrue((self.local_repo / "feature.txt").exists())

    def test_preflight_blocks_when_local_branch_is_ahead(self):
        """5. Preflight blocca con BLOCKED_GIT_SYNC se il branch locale ha commit non inviati (ahead)."""
        (self.local_repo / "local_patch.txt").write_text("Local work\n", encoding="utf-8")
        subprocess.run(["git", "add", "local_patch.txt"], cwd=str(self.local_repo), check=True)
        subprocess.run(["git", "commit", "-m", "unpushed local commit"], cwd=str(self.local_repo), check=True)

        ok, err, init_c = check_git_preflight_sync(self.local_repo, branch="main")
        self.assertFalse(ok)
        self.assertIn("BLOCKED_GIT_SYNC", err)
        self.assertIn("ahead", err.lower())
        self.assertIn("git push", err)

    def test_preflight_blocks_when_local_branch_is_divergent(self):
        """6. Preflight blocca con BLOCKED_GIT_SYNC se il branch locale e origin sono divergenti."""
        # 1. Commit locale non inviato
        (self.local_repo / "local_patch.txt").write_text("Local commit\n", encoding="utf-8")
        subprocess.run(["git", "add", "local_patch.txt"], cwd=str(self.local_repo), check=True)
        subprocess.run(["git", "commit", "-m", "local commit"], cwd=str(self.local_repo), check=True)

        # 2. Commit remoto concorrente
        second_clone = Path(self.temp_dir) / "second_clone_div"
        subprocess.run(["git", "clone", "-b", "main", str(self.remote_dir), str(second_clone)], capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Peer Dev"], cwd=str(second_clone), check=True)
        subprocess.run(["git", "config", "user.email", "peer@taktstock.local"], cwd=str(second_clone), check=True)
        (second_clone / "remote_patch.txt").write_text("Remote commit\n", encoding="utf-8")
        subprocess.run(["git", "add", "remote_patch.txt"], cwd=str(second_clone), check=True)
        subprocess.run(["git", "commit", "-m", "remote conflicting history"], cwd=str(second_clone), check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=str(second_clone), check=True)

        ok, err, init_c = check_git_preflight_sync(self.local_repo, branch="main")
        self.assertFalse(ok)
        self.assertIn("BLOCKED_GIT_SYNC", err)
        self.assertIn("divergente", err.lower())
        self.assertIn("git pull --rebase", err)

    def test_preflight_blocks_when_fetch_fails(self):
        """7. Preflight blocca con BLOCKED_GIT_SYNC se git fetch --prune fallisce (es. remote irraggiungibile)."""
        # Imposta URL remoto non valido
        subprocess.run(["git", "remote", "set-url", "origin", "/non/existent/path/repo.git"], cwd=str(self.local_repo), check=True)

        ok, err, init_c = check_git_preflight_sync(self.local_repo, branch="main")
        self.assertFalse(ok)
        self.assertIn("BLOCKED_GIT_SYNC", err)
        self.assertIn("git fetch --prune origin", err)

    def test_run_full_workflow_blocks_on_dirty_repository_and_reports_commits(self):
        """8. run_full_workflow blocca all'avvio con BLOCKED_GIT_SYNC se il repository è dirty e registra commit."""
        (self.local_repo / "untracked.py").write_text("print(1)\n", encoding="utf-8")

        runner = MultiAgentRunner(
            workspace_path=self.local_repo,
            mock_mode=False,
            preset="quick",
            state_dir=self.state_dir
        )

        result = runner.run_full_workflow("Audit di prova", branch_name="main", do_push=False)
        self.assertEqual(result["status"], "BLOCKED_GIT_SYNC")
        self.assertIn("BLOCKED_GIT_SYNC", result["blocker_reason"])
        self.assertIsNotNone(result.get("initial_commit"))
        self.assertIsNotNone(result.get("final_commit"))

    def test_run_full_workflow_records_initial_and_final_commit_on_completed(self):
        """9. run_full_workflow registra commit iniziale e finale nel summary di un workflow completato."""
        runner = MultiAgentRunner(
            workspace_path=self.local_repo,
            mock_mode=False,
            preset="quick",
            state_dir=self.state_dir
        )

        head_before = self._get_head(self.local_repo)
        with patch.object(runner, "brainstorm", return_value={"analysis": "ok"}), \
             patch.object(runner, "call_director", return_value={"tasks": [{"id": "T1", "a": "agy", "d": "Task 1", "p": "P1"}]}), \
             patch.object(runner, "call_executor_agy", return_value="Fatto"):

            result = runner.run_full_workflow("Task completato", branch_name="main", do_push=False)

            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["initial_commit"], head_before)
            self.assertEqual(result["final_commit"], head_before)

            # Verifica formattazione Telegram con commit
            tg_msg = format_workflow_telegram_message("Taktstock ha completato il lavoro!", result, is_strict=False)
            self.assertIn("Commit:", tg_msg)
            self.assertIn(head_before[:8], tg_msg)


if __name__ == "__main__":
    unittest.main()
