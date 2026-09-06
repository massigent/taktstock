#!/usr/bin/env python3
"""
Unit & Integration Tests for MultiAgentRunner SQLite Shadow Persistence
-----------------------------------------------------------------------
Verifica:
1. Flag disattivato (default / UFFICIO_SQLITE_RUN_SHADOW_WRITE=0):
   - Nessun DatabaseManager/RunStateAdapter attivo, nessun DB creato
   - run_id NON compare in webhook payload, summary, pending JSON e runs_history.jsonl
2. Flag attivato (UFFICIO_SQLITE_RUN_SHADOW_WRITE=1 / adapter iniettato):
   - run_id identico compare in summary, approval_res, pending JSON, webhook data e SQLite
   - Lifecycle completo running -> completed a 100% in SQLite
   - Lifecycle modifiche in waiting_for_approval a 90% in SQLite
3. Tolleranza agli errori SQLite: il runner completa regolarmente se SQLite fallisce
4. Fallimento workflow gestito: run registrata come 'failed' con errore strutturato
5. Preservazione di checkpoint_*.json e coerenza esecuzione
6. Isolamento completo in directory temporanee
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from infrastructure.database import DatabaseManager
from infrastructure.run_repository import RunRepository
from infrastructure.run_state_adapter import RunStateAdapter
from orchestrator_core import MultiAgentRunner


class TestOrchestratorSQLiteShadow(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ufficio_run_shadow_test_"))
        self.state_dir = self.test_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = self.test_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / "shadow_runs.db"

        self.orig_shadow_write = os.environ.get("UFFICIO_SQLITE_RUN_SHADOW_WRITE")
        os.environ["UFFICIO_SQLITE_RUN_SHADOW_WRITE"] = "0"

    def tearDown(self):
        if self.orig_shadow_write is not None:
            os.environ["UFFICIO_SQLITE_RUN_SHADOW_WRITE"] = self.orig_shadow_write
        else:
            os.environ.pop("UFFICIO_SQLITE_RUN_SHADOW_WRITE", None)

        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_default_safe_behavior_no_sqlite_db_created(self):
        """Con flag a 0, MultiAgentRunner non istanzia RunStateAdapter e non crea file DB."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            preset="light"
        )
        self.assertIsNone(runner.run_adapter)
        self.assertIsNotNone(runner.run_id)

        result = runner.run_full_workflow("Task mock senza shadow", branch_name="feature/no-shadow")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertFalse(self.db_path.exists())
        self.assertFalse((self.state_dir / "ufficio.db").exists())

    def test_flag_off_strictly_excludes_run_id_from_external_contracts_and_json(self):
        """Con shadow flag disattivato, run_id NON deve comparire in webhook, summary, pending JSON e runs_history.jsonl."""
        # 1. Test su run completata con intercettazione send_notification
        with patch("orchestrator_core.send_notification") as mock_notify:
            runner = MultiAgentRunner(
                workspace_path=self.workspace_dir,
                state_dir=self.state_dir,
                mock_mode=True,
                preset="light"
            )
            result = runner.run_full_workflow("Task clean legacy contract", branch_name="feature/legacy-clean")

            # Verifica assenza run_id dal summary restituito
            self.assertNotIn("run_id", result)

            # Verifica assenza run_id da runs_history.jsonl
            history_file = self.state_dir / "runs_history.jsonl"
            self.assertTrue(history_file.exists())
            history_lines = [json.loads(line) for line in history_file.read_text().strip().split("\n")]
            for entry in history_lines:
                self.assertNotIn("run_id", entry)

            # Verifica assenza run_id da tutti i payload inviati a send_notification
            self.assertGreater(mock_notify.call_count, 0)
            for call_args in mock_notify.call_args_list:
                _, _, _, data = call_args[0][:4]
                if isinstance(data, dict):
                    self.assertNotIn("run_id", data)

        # 2. Test su run in waiting_for_approval con modifiche simulate
        mock_diff = MagicMock()
        mock_diff.get_diff_data.return_value = {
            "has_changes": True,
            "stat": "+5 -1",
            "files_changed": ["main.py"],
            "insertions": 5,
            "deletions": 1
        }
        mock_diff.generate_html_diff.return_value = "/tmp/diffs/diff.html"
        mock_diff.format_telegram_summary.return_value = "Sintesi diff"

        runner_appr = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            require_approval=True,
            diff_manager=mock_diff
        )
        appr_res = runner_appr.run_full_workflow("Task pending check", branch_name="feature/pending-clean")

        # Verifica che approval_res e pending_*.json NON contengano run_id
        self.assertNotIn("run_id", appr_res)
        pending_file = self.state_dir / "pending_feature_pending-clean.json"
        self.assertTrue(pending_file.exists())
        pending_data = json.loads(pending_file.read_text())
        self.assertNotIn("run_id", pending_data)

    def test_flag_on_includes_consistent_run_id_across_all_artifacts(self):
        """Con shadow flag attivo, lo stesso run_id compare in webhook, summary, pending JSON, runs_history.jsonl e SQLite."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = RunStateAdapter(db_manager=db, shadow_write=True)

        with patch("orchestrator_core.send_notification") as mock_notify:
            runner = MultiAgentRunner(
                workspace_path=self.workspace_dir,
                state_dir=self.state_dir,
                mock_mode=True,
                preset="light",
                run_adapter=adapter
            )
            run_id = runner.run_id

            result = runner.run_full_workflow("Task shadow contracts check", branch_name="feature/shadow-contracts")

            # 1. Verifica summary
            self.assertEqual(result.get("run_id"), run_id)

            # 2. Verifica runs_history.jsonl
            history_file = self.state_dir / "runs_history.jsonl"
            history_lines = [json.loads(line) for line in history_file.read_text().strip().split("\n")]
            matching_entries = [e for e in history_lines if e.get("run_id") == run_id]
            self.assertEqual(len(matching_entries), 1)

            # 3. Verifica notifiche webhook
            notifs_with_data = [call_args[0][3] for call_args in mock_notify.call_args_list if call_args[0][3]]
            self.assertGreater(len(notifs_with_data), 0)
            for data in notifs_with_data:
                self.assertEqual(data.get("run_id"), run_id)

            # 4. Verifica SQLite
            run_repo = RunRepository(db)
            sql_run = run_repo.get_run(run_id)
            self.assertIsNotNone(sql_run)
            self.assertEqual(sql_run["id"], run_id)
            self.assertEqual(sql_run["status"], "completed")

    def test_mock_completed_run_shadow_lifecycle(self):
        """Con shadow write abilitato, la run avanza e completa con status='completed' e progress=100 in SQLite."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = RunStateAdapter(db_manager=db, shadow_write=True)

        runner = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            preset="light",
            run_adapter=adapter
        )

        run_id = runner.run_id
        result = runner.run_full_workflow("Task shadow completato", branch_name="feature/shadow-comp")

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["run_id"], run_id)

        # Verifica SQLite
        run_repo = RunRepository(db)
        sql_run = run_repo.get_run(run_id)
        self.assertIsNotNone(sql_run)
        self.assertEqual(sql_run["status"], "completed")
        self.assertEqual(sql_run["progress"], 100)
        self.assertEqual(sql_run["current_step"], "completed")
        self.assertEqual(sql_run["branch"], "feature/shadow-comp")
        self.assertEqual(sql_run["metadata"]["task"], "Task shadow completato")
        self.assertEqual(sql_run["metadata"]["source"], "shadow")

    def test_run_with_changes_waiting_for_approval(self):
        """Con modifiche non committate e require_approval, lo stato in SQLite diventa waiting_for_approval."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = RunStateAdapter(db_manager=db, shadow_write=True)

        # Mock del DiffReviewManager per simulare modifiche
        mock_diff = MagicMock()
        mock_diff.get_diff_data.return_value = {
            "has_changes": True,
            "stat": "+10 -2",
            "files_changed": ["server/app.py"],
            "insertions": 10,
            "deletions": 2
        }
        mock_diff.generate_html_diff.return_value = "/tmp/diffs/diff_test.html"
        mock_diff.format_telegram_summary.return_value = "Sintesi diff"

        runner = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            require_approval=True,
            diff_manager=mock_diff,
            run_adapter=adapter
        )

        run_id = runner.run_id
        result = runner.run_full_workflow("Task approval required", branch_name="feature/shadow-approval")

        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(result["run_id"], run_id)

        # Verifica pending_*.json
        pending_file = self.state_dir / "pending_feature_shadow-approval.json"
        self.assertTrue(pending_file.exists())
        pending_json = json.loads(pending_file.read_text())
        self.assertEqual(pending_json["run_id"], run_id)

        # Verifica SQLite
        run_repo = RunRepository(db)
        sql_run = run_repo.get_run(run_id)
        self.assertIsNotNone(sql_run)
        self.assertEqual(sql_run["status"], "waiting_for_approval")
        self.assertEqual(sql_run["progress"], 90)
        self.assertEqual(sql_run["current_step"], "waiting_approval")
        self.assertEqual(sql_run["result"]["status"], "WAITING_FOR_APPROVAL")

    def test_sqlite_error_tolerance_does_not_break_runner(self):
        """Se SQLite va in errore (es. lock o crash), MultiAgentRunner completa regolarmente."""
        mock_db = MagicMock(spec=DatabaseManager)
        mock_db.transaction.side_effect = Exception("Simulated SQLite Error")

        adapter = RunStateAdapter(db_manager=mock_db, shadow_write=True)
        runner = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            run_adapter=adapter
        )

        result = runner.run_full_workflow("Task resiliente a crash SQLite", branch_name="feature/resilient")
        self.assertEqual(result["status"], "COMPLETED")

    def test_workflow_exception_records_failed_run_in_sqlite(self):
        """Se il workflow fallisce con eccezione, la run in SQLite viene registrata come 'failed' con errore strutturato."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = RunStateAdapter(db_manager=db, shadow_write=True)

        runner = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            run_adapter=adapter
        )

        run_id = runner.run_id

        # Simula fallimento in decompose
        with patch.object(runner, "decompose", side_effect=ValueError("Simulated Workflow Crash")):
            with self.assertRaises(ValueError):
                runner.run_full_workflow("Task crashante", branch_name="feature/crash")

        # Verifica che SQLite abbia registrato il fallimento
        run_repo = RunRepository(db)
        sql_run = run_repo.get_run(run_id)
        self.assertIsNotNone(sql_run)
        self.assertEqual(sql_run["status"], "failed")
        self.assertEqual(sql_run["current_step"], "failed")
        self.assertIn("Simulated Workflow Crash", sql_run["error"])

    def test_single_consistent_run_id_across_updates(self):
        """Una sola run_id coerente viene mantenuta in tutti gli step del ciclo di vita."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = RunStateAdapter(db_manager=db, shadow_write=True)

        runner = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            run_adapter=adapter
        )

        run_id = runner.run_id
        runner.run_full_workflow("Task ID consistency", branch_name="feature/id-check")

        run_repo = RunRepository(db)
        all_runs = run_repo.list_runs()
        self.assertEqual(len(all_runs), 1)
        self.assertEqual(all_runs[0]["id"], run_id)

    def test_no_effect_on_checkpoint_and_json_files(self):
        """La shadow write SQLite non altera i file di checkpoint e il loro contenuto."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = RunStateAdapter(db_manager=db, shadow_write=True)

        runner = MultiAgentRunner(
            workspace_path=self.workspace_dir,
            state_dir=self.state_dir,
            mock_mode=True,
            run_adapter=adapter
        )

        runner.run_full_workflow("Task checkpoint verification", branch_name="feature/ckpt-check")

        # Verifica che i checkpoint siano presenti e validi
        ckpt_file = self.state_dir / "checkpoint_feature_ckpt-check.json"
        self.assertTrue(ckpt_file.exists())
        ckpt_data = json.loads(ckpt_file.read_text())
        self.assertEqual(ckpt_data["branch"], "feature/ckpt-check")
        self.assertIn("completed_task_ids", ckpt_data)


if __name__ == "__main__":
    unittest.main()
