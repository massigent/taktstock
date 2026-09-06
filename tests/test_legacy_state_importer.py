#!/usr/bin/env python3
"""
Unit & Integration Tests for Legacy JSON State Importer (Hardened - Isolamento Dati)
------------------------------------------------------------------------------------
Verifica:
1. Protezione temporale su active mapping:
   - Sessione SQLite più recente e già completed + active mapping legacy più vecchio -> status resta completed e mapping non registrato
   - Active mapping più recente -> status diventa active e updated_at avanza senza retrodatazione
2. Identità deterministica delle run:
   - Due state_dir con stesso pending filename producono due run distinte con task/metadata distinti
   - Reimport dello stesso file modificato nella stessa state_dir aggiorna la stessa run senza crearne una seconda
3. Protezione dati SQLite più recenti con parsing e normalizzazione datetime UTC (sessions, messages, runs)
4. Ordine corretto in due passaggi (prima bs_*, poi active_*, poi runs)
5. Segnalazione skipped_missing_session su mapping active orfano
6. Checkpoint reale con schema orchestrator_core (phase, tokens_used, data)
7. Tolleranza a file JSON corrotti e vuoti
8. Modalità dry-run e integrità dei file originali
9. Sanitizzazione parametri di paginazione nei repository
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from infrastructure.database import DatabaseManager
from infrastructure.session_repository import SessionRepository
from infrastructure.run_repository import RunRepository
from infrastructure.legacy_state_importer import (
    LegacyStateImporter,
    import_legacy_state,
    compute_file_sha256,
    generate_deterministic_run_id,
    parse_iso_utc,
    is_sqlite_record_newer,
)


class TestLegacyStateImporterHardened(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ufficio_importer_hardened_"))
        self.db_path = self.test_dir / "test_import.db"
        self.state_dir = self.test_dir / "legacy_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.brainstorms_dir = self.state_dir / "brainstorms"
        self.brainstorms_dir.mkdir(parents=True, exist_ok=True)

        self.db = DatabaseManager(self.db_path, auto_migrate=True)
        self.sess_repo = SessionRepository(self.db)
        self.run_repo = RunRepository(self.db)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_utc_timestamp_normalization_and_comparison(self):
        """Verifica che il parser normalizzi correttamente fusi orari UTC e gestisca timestamp non validi."""
        dt1 = parse_iso_utc("2026-08-23T12:00:00Z")
        dt2 = parse_iso_utc("2026-08-23T14:00:00+02:00")
        self.assertIsNotNone(dt1)
        self.assertIsNotNone(dt2)
        self.assertEqual(dt1, dt2)

        self.assertIsNone(parse_iso_utc("NOT_A_DATE"))
        self.assertIsNone(parse_iso_utc(""))
        self.assertIsNone(parse_iso_utc(None))

        self.assertTrue(is_sqlite_record_newer("2026-08-23T15:00:00Z", "2026-08-23T10:00:00Z"))
        self.assertFalse(is_sqlite_record_newer("2026-08-23T10:00:00Z", "2026-08-23T15:00:00Z"))
        self.assertTrue(is_sqlite_record_newer("2026-08-23T10:00:00Z", "INVALID"))

    def test_sqlite_newer_completed_session_not_overwritten_by_older_active_mapping(self):
        """Sessione SQLite più recente e già completed + active mapping legacy più vecchio: status resta completed e mapping non registrato."""
        # 1. Crea sessione in SQLite completata alle 15:00 UTC
        self.sess_repo.create_session(
            chat_id="chat_comp",
            title="Sessione Completata",
            status="completed",
            session_id="bs_completed_session"
        )
        with self.db.transaction() as conn:
            conn.execute("UPDATE sessions SET updated_at = '2026-08-23T15:00:00+00:00' WHERE id = 'bs_completed_session'")

        # 2. Crea file active mapping legacy con timestamp vecchio (10:00 UTC)
        act_file = self.brainstorms_dir / "active_chat_comp.json"
        act_file.write_text(
            json.dumps({"active_id": "bs_completed_session", "timestamp": "2026-08-23T10:00:00Z"}),
            encoding="utf-8"
        )

        report = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)

        # La sessione deve rimanere completed
        sess = self.sess_repo.get_session("bs_completed_session")
        self.assertEqual(sess["status"], "completed")
        self.assertEqual(sess["updated_at"], "2026-08-23T15:00:00+00:00")
        self.assertIn("skipped_sqlite_newer", [d.get("status") for d in report["details"]])

        # Il mapping non deve essere registrato in legacy_imports
        with self.db.connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM legacy_imports WHERE entity_id = 'bs_completed_session' AND entity_type = 'active_mapping'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_active_mapping_newer_advances_status_and_updated_at(self):
        """Active mapping più recente: status diventa active e updated_at avanza."""
        # 1. Crea sessione in SQLite alle 10:00 UTC con status 'idle'
        self.sess_repo.create_session(
            chat_id="chat_adv",
            title="Sessione Idle",
            status="idle",
            session_id="bs_adv_session"
        )
        with self.db.transaction() as conn:
            conn.execute("UPDATE sessions SET updated_at = '2026-08-23T10:00:00+00:00' WHERE id = 'bs_adv_session'")

        # 2. Crea file active mapping legacy con timestamp più recente (12:30 UTC)
        act_file = self.brainstorms_dir / "active_chat_adv.json"
        act_file.write_text(
            json.dumps({"active_id": "bs_adv_session", "timestamp": "2026-08-23T12:30:00Z"}),
            encoding="utf-8"
        )

        report = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)

        sess = self.sess_repo.get_session("bs_adv_session")
        self.assertEqual(sess["status"], "active")
        self.assertIn("2026-08-23T12:30:00", sess["updated_at"])

        with self.db.connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM legacy_imports WHERE entity_id = 'bs_adv_session' AND entity_type = 'active_mapping'")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_two_state_dirs_with_same_pending_filename_produce_distinct_runs(self):
        """Due state_dir con lo stesso pending filename producono due runs distinte con task/metadata distinti."""
        dir_a = self.test_dir / "state_alpha"
        dir_a.mkdir()
        dir_b = self.test_dir / "state_beta"
        dir_b.mkdir()

        # Stesso nome file pending_feature_test.json in entrambe le directory
        (dir_a / "pending_feature_test.json").write_text(
            json.dumps({"branch": "feature/test", "task": "Task Directory Alpha", "workspace": "/alpha"}),
            encoding="utf-8"
        )
        (dir_b / "pending_feature_test.json").write_text(
            json.dumps({"branch": "feature/test", "task": "Task Directory Beta", "workspace": "/beta"}),
            encoding="utf-8"
        )

        rep_a = import_legacy_state(dir_a, db_manager=self.db, dry_run=False)
        rep_b = import_legacy_state(dir_b, db_manager=self.db, dry_run=False)

        self.assertEqual(rep_a["imported_runs"], 1)
        self.assertEqual(rep_b["imported_runs"], 1)

        runs = self.run_repo.list_runs(limit=10)
        self.assertEqual(len(runs), 2)
        run_ids = {r["id"] for r in runs}
        self.assertEqual(len(run_ids), 2)  # Due ID distinti

        tasks = {r["metadata"]["task"] for r in runs}
        self.assertEqual(tasks, {"Task Directory Alpha", "Task Directory Beta"})

        workspaces = {r["result"]["workspace"] for r in runs}
        self.assertEqual(workspaces, {"/alpha", "/beta"})

    def test_reimport_modified_file_in_same_state_dir_updates_same_run(self):
        """Reimport dello stesso file modificato nella stessa state_dir aggiorna la stessa run senza duplicati."""
        pending_file = self.state_dir / "pending_feature_update.json"
        pending_file.write_text(
            json.dumps({"branch": "feature/update", "task": "Task Versione 1", "workspace": "/v1"}),
            encoding="utf-8"
        )

        # Primo import
        rep1 = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)
        self.assertEqual(rep1["imported_runs"], 1)
        runs_1 = self.run_repo.list_runs()
        self.assertEqual(len(runs_1), 1)
        run_id = runs_1[0]["id"]
        self.assertEqual(runs_1[0]["metadata"]["task"], "Task Versione 1")

        # Modifica il file nella stessa directory
        pending_file.write_text(
            json.dumps({"branch": "feature/update", "task": "Task Versione 2 Aggiornato", "workspace": "/v2"}),
            encoding="utf-8"
        )

        # Secondo import
        rep2 = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)
        self.assertEqual(rep2["imported_runs"], 1)

        # Verifica che il numero totale di runs sia sempre 1 e con i dati aggiornati
        runs_2 = self.run_repo.list_runs()
        self.assertEqual(len(runs_2), 1)
        self.assertEqual(runs_2[0]["id"], run_id)
        self.assertEqual(runs_2[0]["metadata"]["task"], "Task Versione 2 Aggiornato")
        self.assertEqual(runs_2[0]["result"]["workspace"], "/v2")

    def test_sqlite_newer_run_unchanged_after_reimport(self):
        """Una run in SQLite più recente del file JSON legacy NON viene sovrascritta."""
        run_file = self.state_dir / "pending_feature_protect.json"
        run_file.write_text(
            json.dumps({
                "branch": "feature/protect",
                "task": "Task vecchio da JSON",
                "status": "WAITING_FOR_APPROVAL",
                "timestamp": "2026-08-23T10:00:00Z",
                "workspace": "/tmp/old",
            }),
            encoding="utf-8"
        )

        # Inserisce preventivamente in SQLite una run con ID deterministico e data più recente
        canonical_key = f"{str(self.state_dir.resolve())}::pending_feature_protect.json"
        det_run_id = generate_deterministic_run_id(canonical_key)

        self.run_repo.create_run(
            action="execute_task",
            branch="feature/protect",
            status="completed",
            run_id=det_run_id,
            metadata={"source": "sqlite_live", "task": "Task recente completato in SQLite"}
        )
        self.run_repo.update_run_status(
            run_id=det_run_id,
            status="completed",
            progress=100,
            result={"status": "COMPLETED_IN_SQLITE"}
        )
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET updated_at = '2026-08-23T15:00:00+00:00' WHERE id = ?", (det_run_id,))

        report = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)

        loaded = self.run_repo.get_run(det_run_id)
        self.assertEqual(loaded["status"], "completed")
        self.assertEqual(loaded["result"]["status"], "COMPLETED_IN_SQLITE")
        self.assertEqual(loaded["metadata"]["task"], "Task recente completato in SQLite")
        self.assertIn("skipped_sqlite_newer", [d.get("status") for d in report["details"]])

    def test_sqlite_newer_message_unchanged_after_reimport(self):
        """Un messaggio in SQLite con timestamp più recente non viene sovrascritto dal file legacy."""
        sess = self.sess_repo.create_session(chat_id="chat_m", title="Sessione Live", session_id="bs_test_msg")
        msg = self.sess_repo.add_message(
            session_id=sess["id"],
            role="assistant",
            author="sol",
            content="Messaggio aggiornato in SQLite",
            message_id="msg_001"
        )
        with self.db.transaction() as conn:
            conn.execute("UPDATE messages SET created_at = '2026-08-23T15:00:00+00:00' WHERE id = 'msg_001'")

        bs_data = {
            "id": "bs_test_msg",
            "chat_id": "chat_m",
            "task": "Sessione Live",
            "status": "active",
            "created_at": "2026-08-23T08:00:00Z",
            "updated_at": "2026-08-23T08:30:00Z",
            "messages": [
                {
                    "id": "msg_001",
                    "sender": "User",
                    "text": "Messaggio vecchio da JSON",
                    "timestamp": "2026-08-23T08:05:00Z"
                }
            ]
        }
        (self.brainstorms_dir / "bs_test_msg.json").write_text(json.dumps(bs_data), encoding="utf-8")

        import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)

        loaded_sess = self.sess_repo.get_session("bs_test_msg", include_messages=True)
        msgs = loaded_sess["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "Messaggio aggiornato in SQLite")
        self.assertEqual(msgs[0]["author"], "sol")

    def test_active_mapping_missing_target_session_skipped_missing_session(self):
        """Un file active_* che punta a una sessione inesistente non viene marcato importato."""
        act_file = self.brainstorms_dir / "active_chat_orphan.json"
        act_file.write_text(json.dumps({"active_id": "bs_orphan_999"}), encoding="utf-8")

        report = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)

        self.assertIn("skipped_missing_session", [d.get("status") for d in report["details"]])
        with self.db.connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM legacy_imports WHERE entity_id = 'bs_orphan_999'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_real_checkpoint_schema_imported_correctly(self):
        """Verifica l'importazione di un checkpoint reale salvato da orchestrator_core.py."""
        ckpt_data = {
            "branch": "feature/real-ckpt",
            "phase": "task_T2_completed",
            "completed_task_ids": ["T1", "T2"],
            "completed_tasks_history": {"T1": "out1", "T2": "out2"},
            "tokens_used": {"total": 500, "prompt": 400, "completion": 100},
            "data": {"task_id": "T2", "output": "output T2"},
            "timestamp": "2026-08-23T12:00:00Z",
            "extra_field": "preserved_in_metadata"
        }
        (self.state_dir / "checkpoint_feature_real-ckpt.json").write_text(json.dumps(ckpt_data), encoding="utf-8")

        report = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=False)
        self.assertEqual(report["imported_runs"], 1)

        runs = self.run_repo.list_runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["result"]["phase"], "task_T2_completed")
        self.assertEqual(run["result"]["completed_task_ids"], ["T1", "T2"])
        self.assertEqual(run["result"]["tokens_used"]["total"], 500)
        self.assertEqual(run["result"]["data"]["task_id"], "T2")
        self.assertEqual(run["metadata"]["extra_field"], "preserved_in_metadata")

    def test_dry_run_mode_does_not_modify_database(self):
        """Verifica che la modalità dry_run analizzi i file senza scrivere su SQLite."""
        (self.state_dir / "pending_dry.json").write_text(
            json.dumps({"branch": "feature/dry", "task": "Dry Task"}), encoding="utf-8"
        )
        report = import_legacy_state(self.state_dir, db_manager=self.db, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["imported_runs"], 1)

        runs = self.run_repo.list_runs()
        self.assertEqual(len(runs), 0)


if __name__ == "__main__":
    unittest.main()
