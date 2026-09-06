#!/usr/bin/env python3
"""
Unit & Integration Tests for BrainstormManager SQLite Shadow Persistence & Atomic JSON
---------------------------------------------------------------------------------------
Verifica:
1. Scrittura atomica JSON:
   - Errore prima o durante os.replace preserva il JSON precedente e non chiama shadow_save/SQLite
   - Scrittura riuscita produce file JSON validi e integri senza file temporanei residui
2. Shadow compare semantico completo:
   - Rilevamento divergenza sull'autore del messaggio (author mismatch)
   - Rilevamento divergenza su ruolo o testo del messaggio
   - Rilevamento divergenza su preset e final_plan anche se assenti/nulli da un lato
   - Record identici non producono alcuna divergenza
3. Tolleranza agli errori SQLite e priorità autorevole dei dati JSON
4. Active mapping atomico e replica in SQLite
5. Isolamento totale in directory temporanee
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
from infrastructure.session_repository import SessionRepository
from infrastructure.brainstorm_state_adapter import BrainstormStateAdapter
from brainstorm_manager import BrainstormManager, atomic_write_json


class TestBrainstormSQLiteShadow(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="taktstock_shadow_test_"))
        self.state_dir = self.test_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / "shadow_test.db"

        self.orig_shadow_write = os.environ.get("TAKTSTOCK_SQLITE_SHADOW_WRITE")
        self.orig_shadow_read = os.environ.get("TAKTSTOCK_SQLITE_SHADOW_READ")
        self.orig_legacy_write = os.environ.get("UFFICIO_SQLITE_SHADOW_WRITE")
        self.orig_legacy_read = os.environ.get("UFFICIO_SQLITE_SHADOW_READ")

        os.environ["TAKTSTOCK_SQLITE_SHADOW_WRITE"] = "0"
        os.environ["TAKTSTOCK_SQLITE_SHADOW_READ"] = "0"
        os.environ.pop("UFFICIO_SQLITE_SHADOW_WRITE", None)
        os.environ.pop("UFFICIO_SQLITE_SHADOW_READ", None)

    def tearDown(self):
        for var, orig in [
            ("TAKTSTOCK_SQLITE_SHADOW_WRITE", self.orig_shadow_write),
            ("TAKTSTOCK_SQLITE_SHADOW_READ", self.orig_shadow_read),
            ("UFFICIO_SQLITE_SHADOW_WRITE", self.orig_legacy_write),
            ("UFFICIO_SQLITE_SHADOW_READ", self.orig_legacy_read),
        ]:
            if orig is not None:
                os.environ[var] = orig
            else:
                os.environ.pop(var, None)

        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_default_safe_behavior_json_only_no_sqlite_db_created(self):
        """Con flag disattivati, BrainstormManager opera solo su JSON e non crea alcun file DB."""
        manager = BrainstormManager(state_dir=self.state_dir)
        self.assertIsNone(manager.state_adapter)

        bs_id = manager.create_brainstorm(
            task="Task base JSON only",
            preset="light",
            chat_id="chat_default_1",
            repo=None,
        )
        self.assertTrue(bs_id.startswith("bs_"))

        json_file = manager.get_file_path(bs_id)
        self.assertTrue(json_file.exists())
        self.assertFalse((self.state_dir / "taktstock.db").exists())
        self.assertFalse((self.state_dir / "ufficio.db").exists())

        loaded = manager.load_brainstorm(bs_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["task"], "Task base JSON only")

    def test_atomic_write_success_and_file_integrity(self):
        """La scrittura atomica produce file JSON completi, formattati e senza file temporanei residui."""
        target_path = self.state_dir / "test_atomic.json"
        data = {"id": "bs_atomic_1", "task": "Task Atomico", "rounds": [1, 2, 3]}

        success = atomic_write_json(target_path, data)
        self.assertTrue(success)
        self.assertTrue(target_path.exists())

        content = json.loads(target_path.read_text(encoding="utf-8"))
        self.assertEqual(content["task"], "Task Atomico")
        self.assertEqual(content["rounds"], [1, 2, 3])

        # Nessun file .tmp residuo nella directory
        tmp_files = list(self.state_dir.glob(".tmp_*"))
        self.assertEqual(len(tmp_files), 0)

    def test_atomic_write_failure_preserves_previous_json_and_skips_sqlite(self):
        """Se os.replace fallisce, il JSON precedente resta intatto e SQLite non viene invocato né modificato."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = BrainstormStateAdapter(db_manager=db, shadow_write=True, shadow_read=False)
        manager = BrainstormManager(state_dir=self.state_dir, state_adapter=adapter)

        # 1. Salva stato iniziale valido
        bs_id = manager.create_brainstorm(task="Task Iniziale Integro", chat_id="chat_atomic_err")
        json_file = manager.get_file_path(bs_id)
        initial_json_content = json_file.read_text(encoding="utf-8")

        # 2. Simula fallimento in os.replace durante il secondo salvataggio
        modified_data = json.loads(initial_json_content)
        modified_data["task"] = "Task Modificato Che Deve Fallire"

        with patch("os.replace", side_effect=OSError("Simulated Disk/Permissions Error")):
            manager.save_brainstorm(modified_data)

        # 3. Verifica che il file JSON precedente sia rimasto inalterato
        current_json_content = json_file.read_text(encoding="utf-8")
        self.assertEqual(json.loads(current_json_content)["task"], "Task Iniziale Integro")

        # 4. Verifica che SQLite sia rimasto allo stato iniziale (shadow_save non deve essere stato chiamato)
        sess_repo = SessionRepository(db)
        sql_sess = sess_repo.get_session(bs_id)
        self.assertEqual(sql_sess["title"], "Task Iniziale Integro")

        # 5. Nessun file temporaneo orfano
        orphans = list(self.state_dir.glob(".tmp_*")) + list((self.state_dir / "brainstorms").glob(".tmp_*"))
        self.assertEqual(len(orphans), 0)

    def test_shadow_write_replicates_session_and_messages_to_sqlite(self):
        """Con shadow write abilitato, dopo il salvataggio JSON la sessione e i messaggi vengono replicati in SQLite."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = BrainstormStateAdapter(db_manager=db, shadow_write=True, shadow_read=False)
        manager = BrainstormManager(state_dir=self.state_dir, state_adapter=adapter)

        bs_id = manager.create_brainstorm(
            task="Refactoring Architettura Shadow",
            preset="critical",
            chat_id="chat_shadow_1",
            repo=None,
        )

        bs_data = manager.load_brainstorm(bs_id)
        bs_data["messages"].append({
            "id": "msg_user_001",
            "sender": "User",
            "text": "Procediamo con la validazione",
            "timestamp": "2026-08-23T12:00:00Z",
        })
        manager.save_brainstorm(bs_data)

        json_file = manager.get_file_path(bs_id)
        self.assertTrue(json_file.exists())
        json_content = json.loads(json_file.read_text(encoding="utf-8"))
        self.assertEqual(len(json_content["messages"]), 2)

        sess_repo = SessionRepository(db)
        sql_sess = sess_repo.get_session(bs_id, include_messages=True)
        self.assertIsNotNone(sql_sess)
        self.assertEqual(sql_sess["title"], "Refactoring Architettura Shadow")
        self.assertEqual(sql_sess["chat_id"], "chat_shadow_1")

        msgs = sql_sess["messages"]
        self.assertEqual(len(msgs), 2)
        user_msg = next(m for m in msgs if m["id"] == "msg_user_001")
        self.assertEqual(user_msg["role"], "user")
        self.assertEqual(user_msg["content"], "Procediamo con la validazione")

    def test_shadow_write_sqlite_error_tolerance_does_not_break_json_flow(self):
        """Se SQLite va in errore durante la shadow write, il salvataggio JSON va a buon fine e nessuna eccezione viene sollevata."""
        mock_db = MagicMock(spec=DatabaseManager)
        mock_db.transaction.side_effect = Exception("Simulated SQLite Crash or Lock")

        adapter = BrainstormStateAdapter(db_manager=mock_db, shadow_write=True, shadow_read=False)
        manager = BrainstormManager(state_dir=self.state_dir, state_adapter=adapter)

        bs_id = manager.create_brainstorm(task="Task resilient to SQLite crash")

        json_file = manager.get_file_path(bs_id)
        self.assertTrue(json_file.exists())
        loaded = manager.load_brainstorm(bs_id)
        self.assertEqual(loaded["task"], "Task resilient to SQLite crash")

    def test_shadow_read_coherent_records_returns_json_with_no_diffs(self):
        """Shadow read con dati coerenti: restituisce il JSON e non registra divergenze."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = BrainstormStateAdapter(db_manager=db, shadow_write=True, shadow_read=True)
        manager = BrainstormManager(state_dir=self.state_dir, state_adapter=adapter)

        bs_id = manager.create_brainstorm(task="Task Shadow Coerente", chat_id="chat_coerente")
        loaded = manager.load_brainstorm(bs_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["task"], "Task Shadow Coerente")
        self.assertEqual(len(adapter.last_diffs), 0)

    def test_shadow_compare_detects_author_mismatch(self):
        """Shadow compare rileva divergenze sull'autore del messaggio."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = BrainstormStateAdapter(db_manager=db, shadow_write=True, shadow_read=True)
        manager = BrainstormManager(state_dir=self.state_dir, state_adapter=adapter)

        bs_id = manager.create_brainstorm(task="Task Author Check", chat_id="chat_auth")

        # Modifica l'autore in SQLite da 'sol' a 'glm'
        with db.transaction() as conn:
            conn.execute("UPDATE messages SET author = 'glm' WHERE session_id = ?", (bs_id,))

        manager.load_brainstorm(bs_id)
        diff_str = " ".join(adapter.last_diffs)
        self.assertIn("author mismatch", diff_str)

    def test_shadow_compare_detects_preset_or_final_plan_missing_in_sqlite(self):
        """Shadow compare rileva divergenza su preset e final_plan assenti in SQLite."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = BrainstormStateAdapter(db_manager=db, shadow_write=True, shadow_read=True)
        manager = BrainstormManager(state_dir=self.state_dir, state_adapter=adapter)

        bs_id = manager.create_brainstorm(task="Task Plan Check", preset="critical", chat_id="chat_plan")
        bs_data = manager.load_brainstorm(bs_id)
        bs_data["final_plan"] = "Piano approvato in 3 fasi"
        manager.save_brainstorm(bs_data)

        # Svuota i metadata in SQLite
        with db.transaction() as conn:
            conn.execute("UPDATE sessions SET metadata = '{}' WHERE id = ?", (bs_id,))

        manager.load_brainstorm(bs_id)
        diff_str = " ".join(adapter.last_diffs)
        self.assertIn("preset mismatch", diff_str)
        self.assertIn("final_plan mismatch", diff_str)

    def test_shadow_set_active_updates_sqlite_status(self):
        """set_active_brainstorm atomico aggiorna lo status della sessione in SQLite se shadow_write è attivo."""
        db = DatabaseManager(self.db_path, auto_migrate=True)
        adapter = BrainstormStateAdapter(db_manager=db, shadow_write=True, shadow_read=False)
        manager = BrainstormManager(state_dir=self.state_dir, state_adapter=adapter)

        bs_id = manager.create_brainstorm(task="Task Active Test", chat_id="chat_act")
        with db.transaction() as conn:
            conn.execute("UPDATE sessions SET status = 'idle' WHERE id = ?", (bs_id,))

        manager.set_active_brainstorm("chat_act", bs_id)

        active_json = manager.get_active_mapping_path("chat_act")
        self.assertTrue(active_json.exists())
        self.assertEqual(json.loads(active_json.read_text())["active_id"], bs_id)

        sess_repo = SessionRepository(db)
        sql_sess = sess_repo.get_session(bs_id)
        self.assertEqual(sql_sess["status"], "active")

    def test_config_json_true_enables_shadow_write_when_env_unset(self):
        """1. config.json con storage.sqlite_shadow_write=true abilita shadow write quando le variabili d'ambiente sono assenti."""
        os.environ.pop("TAKTSTOCK_SQLITE_SHADOW_WRITE", None)
        os.environ.pop("TAKTSTOCK_SQLITE_SHADOW_READ", None)
        os.environ.pop("UFFICIO_SQLITE_SHADOW_WRITE", None)
        os.environ.pop("UFFICIO_SQLITE_SHADOW_READ", None)

        fake_config = self.test_dir / "config.json"
        fake_config.write_text(json.dumps({
            "storage": {
                "sqlite_shadow_write": True,
                "sqlite_shadow_read": False
            }
        }), encoding="utf-8")

        adapter = BrainstormStateAdapter(config_path=fake_config)
        self.assertTrue(adapter.shadow_write)
        self.assertFalse(adapter.shadow_read)

    def test_explicit_env_false_overrides_config_true(self):
        """2. Variabile d'ambiente esplicita a '0'/'false' prevale su config.json impostato a true."""
        os.environ["TAKTSTOCK_SQLITE_SHADOW_WRITE"] = "0"
        os.environ["TAKTSTOCK_SQLITE_SHADOW_READ"] = "false"
        os.environ.pop("UFFICIO_SQLITE_SHADOW_WRITE", None)
        os.environ.pop("UFFICIO_SQLITE_SHADOW_READ", None)

        fake_config = self.test_dir / "config.json"
        fake_config.write_text(json.dumps({
            "storage": {
                "sqlite_shadow_write": True,
                "sqlite_shadow_read": True
            }
        }), encoding="utf-8")

        adapter = BrainstormStateAdapter(config_path=fake_config)
        self.assertFalse(adapter.shadow_write)
        self.assertFalse(adapter.shadow_read)

        mgr = BrainstormManager(state_dir=self.state_dir)
        self.assertIsNone(mgr.state_adapter)

    def test_invalid_or_missing_config_fails_closed(self):
        """3. config.json mancante, vuoto o malformato non abilita nulla (fail-closed a False)."""
        os.environ.pop("TAKTSTOCK_SQLITE_SHADOW_WRITE", None)
        os.environ.pop("TAKTSTOCK_SQLITE_SHADOW_READ", None)
        os.environ.pop("UFFICIO_SQLITE_SHADOW_WRITE", None)
        os.environ.pop("UFFICIO_SQLITE_SHADOW_READ", None)

        fake_config = self.test_dir / "non_existent_config.json"
        adapter = BrainstormStateAdapter(config_path=fake_config)
        self.assertFalse(adapter.shadow_write)
        self.assertFalse(adapter.shadow_read)

        corrupt_config = self.test_dir / "corrupt_config.json"
        corrupt_config.write_text("{not valid json!}", encoding="utf-8")
        adapter_corrupt = BrainstormStateAdapter(config_path=corrupt_config)
        self.assertFalse(adapter_corrupt.shadow_write)
        self.assertFalse(adapter_corrupt.shadow_read)

    def test_existing_env_true_enables_shadow_write(self):
        """4. Variabile d'ambiente esplicita a '1'/'true' abilita correttamente shadow write indipendentemente da config.json."""
        os.environ["TAKTSTOCK_SQLITE_SHADOW_WRITE"] = "1"
        os.environ["TAKTSTOCK_SQLITE_SHADOW_READ"] = "0"
        os.environ.pop("UFFICIO_SQLITE_SHADOW_WRITE", None)
        os.environ.pop("UFFICIO_SQLITE_SHADOW_READ", None)

        fake_config = self.test_dir / "config.json"
        fake_config.write_text(json.dumps({
            "storage": {
                "sqlite_shadow_write": False,
                "sqlite_shadow_read": False
            }
        }), encoding="utf-8")

        adapter = BrainstormStateAdapter(config_path=fake_config)
        self.assertTrue(adapter.shadow_write)
        self.assertFalse(adapter.shadow_read)

        mgr = BrainstormManager(state_dir=self.state_dir)
        self.assertIsNotNone(mgr.state_adapter)
        self.assertTrue(mgr.state_adapter.shadow_write)
        self.assertFalse(mgr.state_adapter.shadow_read)


if __name__ == "__main__":
    unittest.main()
