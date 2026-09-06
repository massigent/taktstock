#!/usr/bin/env python3
"""
Unit & Integration Tests for SQLite Persistence Layer & Repositories
-------------------------------------------------------------------
Verifica:
1. Inizializzazione database con WAL mode, Foreign Keys ON e busy_timeout
2. Migrazioni di schema versionate (schema_migrations) e idempotenza
3. SessionRepository: creazione, stato, messaggi ordinati, ricerca attiva, lista
4. RunRepository: creazione queued, avanzamento stato/progresso, completamento, filtri
5. Vincoli Foreign Key (ON DELETE CASCADE per messaggi, ON DELETE SET NULL per runs)
6. Rollback transazionale atomico in caso di errore
7. Concorrenza multithread in scrittura con due connessioni simultanee (WAL mode)
8. Isolamento totale: solo database temporanei in test_dir, mai state/ufficio.db
"""

import os
import sys
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from typing import List

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from infrastructure.database import DatabaseManager, DatabaseError
from infrastructure.session_repository import SessionRepository
from infrastructure.run_repository import RunRepository


class TestSQLiteDatabaseAndMigrations(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ufficio_db_test_"))
        self.db_path = self.test_dir / "test_ufficio.db"
        self.db = DatabaseManager(self.db_path, auto_migrate=True)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_database_pragmas_wal_fk_busy_timeout(self):
        """Verifica che PRAGMA journal_mode=WAL, foreign_keys=ON e busy_timeout siano attivi."""
        with self.db.connection() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]

            self.assertEqual(journal_mode.lower(), "wal")
            self.assertEqual(foreign_keys, 1)
            self.assertGreaterEqual(busy_timeout, 5000)

    def test_schema_migrations_tracked_and_idempotent(self):
        """Verifica che le migrazioni vengano registrate e che rieseguire le migrazioni sia idempotente."""
        applied = self.db.get_applied_migrations()
        self.assertIn(1, applied)

        # Riesegue run_migrations: non deve generare errori né duplicati
        self.db.run_migrations()
        applied_again = self.db.get_applied_migrations()
        self.assertEqual(applied, applied_again)

    def test_transactional_rollback_on_error(self):
        """Verifica il rollback completo delle modifiche in caso di eccezione durante una transazione."""
        sess_repo = SessionRepository(self.db)
        sess = sess_repo.create_session(chat_id="chat_rollback_test", title="Test Rollback")

        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("msg-trans-1", sess["id"], "user", "Messaggio prima del crash", "2026-01-01T00:00:00Z")
                )
                raise RuntimeError("Errore simulato nella transazione")

        # Il messaggio non deve essere presente nel DB a causa del rollback
        retrieved = sess_repo.get_session(sess["id"], include_messages=True)
        self.assertEqual(len(retrieved["messages"]), 0)


class TestSessionRepository(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ufficio_sess_test_"))
        self.db_path = self.test_dir / "test_sessions.db"
        self.db = DatabaseManager(self.db_path, auto_migrate=True)
        self.repo = SessionRepository(self.db)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_create_and_get_session(self):
        """Crea una sessione con metadati e la recupera correttamente."""
        meta = {"preset": "critical", "tags": ["auth", "security"]}
        sess = self.repo.create_session(
            chat_id="123456",
            title="Sessione Hardening",
            metadata=meta
        )

        self.assertTrue(sess["id"])
        self.assertEqual(sess["chat_id"], "123456")
        self.assertEqual(sess["title"], "Sessione Hardening")
        self.assertEqual(sess["status"], "active")
        self.assertEqual(sess["metadata"]["preset"], "critical")

        loaded = self.repo.get_session(sess["id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["id"], sess["id"])
        self.assertEqual(loaded["metadata"]["tags"], ["auth", "security"])

    def test_update_session_status_and_metadata_merge(self):
        """Aggiorna lo stato della sessione e fa il merge incrementale dei metadati."""
        sess = self.repo.create_session(chat_id="100", title="Test Update", metadata={"step": 1})
        updated = self.repo.update_session_status(sess["id"], status="completed", metadata={"step": 2, "cost": 0.05})
        self.assertTrue(updated)

        loaded = self.repo.get_session(sess["id"])
        self.assertEqual(loaded["status"], "completed")
        self.assertEqual(loaded["metadata"]["step"], 2)
        self.assertEqual(loaded["metadata"]["cost"], 0.05)

    def test_add_messages_and_ordered_retrieval(self):
        """Aggiunge messaggi multipli e verifica l'ordinamento cronologico crescente."""
        sess = self.repo.create_session(chat_id="chat_msg_test", title="Chat Multi-Agent")

        m1 = self.repo.add_message(sess["id"], role="user", content="Prima domanda", author="Massimo")
        m2 = self.repo.add_message(sess["id"], role="assistant", content="Risposta di Sol", author="Sol")
        m3 = self.repo.add_message(sess["id"], role="assistant", content="Review di DeepSeek", author="ds-pro")

        loaded = self.repo.get_session(sess["id"], include_messages=True)
        messages = loaded["messages"]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["id"], m1["id"])
        self.assertEqual(messages[0]["author"], "Massimo")
        self.assertEqual(messages[1]["id"], m2["id"])
        self.assertEqual(messages[1]["author"], "Sol")
        self.assertEqual(messages[2]["id"], m3["id"])
        self.assertEqual(messages[2]["author"], "ds-pro")

    def test_get_active_session_by_chat_id(self):
        """Recupera la sessione attiva più recente per un chat_id."""
        s1 = self.repo.create_session(chat_id="999", title="Vecchia attiva")
        self.repo.update_session_status(s1["id"], status="completed")

        s2 = self.repo.create_session(chat_id="999", title="Nuova attiva")

        active = self.repo.get_active_session_by_chat_id("999")
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], s2["id"])
        self.assertEqual(active["title"], "Nuova attiva")

    def test_list_sessions_filters(self):
        """Testa filtri per chat_id, status e paginazione."""
        self.repo.create_session(chat_id="A", title="Sess A1", status="active")
        self.repo.create_session(chat_id="A", title="Sess A2", status="completed")
        self.repo.create_session(chat_id="B", title="Sess B1", status="active")

        list_a = self.repo.list_sessions(chat_id="A")
        self.assertEqual(len(list_a), 2)

        list_active = self.repo.list_sessions(status="active")
        self.assertEqual(len(list_active), 2)

        list_paged = self.repo.list_sessions(limit=1, offset=0)
        self.assertEqual(len(list_paged), 1)

    def test_delete_session_cascades_to_messages(self):
        """Verifica che l'eliminazione di una sessione elimini a cascata tutti i messaggi associati (FK CASCADE)."""
        sess = self.repo.create_session(chat_id="del_chat", title="Sessione da eliminare")
        self.repo.add_message(sess["id"], role="user", content="Messaggio figlio")

        deleted = self.repo.delete_session(sess["id"])
        self.assertTrue(deleted)

        self.assertIsNone(self.repo.get_session(sess["id"]))
        with self.db.connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (sess["id"],))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_foreign_key_violation_on_orphan_message(self):
        """Verifica che l'aggiunta di un messaggio con session_id inesistente sollevi DatabaseError."""
        with self.assertRaises(DatabaseError):
            self.repo.add_message(session_id="non-existent-session-id", role="user", content="Messaggio orfano")


class TestRunRepository(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ufficio_runs_test_"))
        self.db_path = self.test_dir / "test_runs.db"
        self.db = DatabaseManager(self.db_path, auto_migrate=True)
        self.sess_repo = SessionRepository(self.db)
        self.run_repo = RunRepository(self.db)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_create_and_get_run(self):
        """Crea una run in stato queued e ne verifica il recupero."""
        sess = self.sess_repo.create_session(chat_id="run_chat", title="Sessione Run")
        run = self.run_repo.create_run(
            action="execute_task",
            session_id=sess["id"],
            preset="critical",
            repo="ufficio",
            branch="feature/sqlite",
            metadata={"task": "Implementare SQLite"}
        )

        self.assertTrue(run["id"])
        self.assertEqual(run["action"], "execute_task")
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["progress"], 0)
        self.assertEqual(run["preset"], "critical")
        self.assertEqual(run["repo"], "ufficio")
        self.assertEqual(run["branch"], "feature/sqlite")
        self.assertEqual(run["metadata"]["task"], "Implementare SQLite")

        loaded = self.run_repo.get_run(run["id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["id"], run["id"])

    def test_update_run_progress_and_completion(self):
        """Aggiorna progressivamente la run fino a stato completed con result e completed_at."""
        run = self.run_repo.create_run(action="brainstorm")

        # Step 1: avanzamento
        self.run_repo.update_run_status(
            run["id"],
            status="running",
            progress=50,
            current_step="Decomposizione subtask"
        )
        loaded = self.run_repo.get_run(run["id"])
        self.assertEqual(loaded["status"], "running")
        self.assertEqual(loaded["progress"], 50)
        self.assertEqual(loaded["current_step"], "Decomposizione subtask")
        self.assertIsNone(loaded["completed_at"])

        # Step 2: completamento
        res_data = {"status": "COMPLETED", "files_changed": ["server/infrastructure/database.py"]}
        self.run_repo.update_run_status(
            run["id"],
            status="completed",
            progress=100,
            current_step="Validazione superata",
            result=res_data
        )
        completed = self.run_repo.get_run(run["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(completed["result"]["status"], "COMPLETED")
        self.assertIsNotNone(completed["completed_at"])

    def test_list_runs_and_filters(self):
        """Elenca le run con filtri per status, action e session_id."""
        sess = self.sess_repo.create_session(chat_id="run_filter_chat")
        r1 = self.run_repo.create_run(action="chat", session_id=sess["id"], status="completed")
        r2 = self.run_repo.create_run(action="execute_task", session_id=sess["id"], status="running")
        r3 = self.run_repo.create_run(action="brainstorm", status="queued")

        list_sess = self.run_repo.list_runs(session_id=sess["id"])
        self.assertEqual(len(list_sess), 2)

        list_running = self.run_repo.list_runs(status="running")
        self.assertEqual(len(list_running), 1)
        self.assertEqual(list_running[0]["id"], r2["id"])

        list_action = self.run_repo.list_runs(action="brainstorm")
        self.assertEqual(len(list_action), 1)
        self.assertEqual(list_action[0]["id"], r3["id"])

    def test_delete_session_sets_run_session_id_to_null(self):
        """Verifica che ON DELETE SET NULL funzioni: cancellare la sessione non cancella la run ma azzera session_id."""
        sess = self.sess_repo.create_session(chat_id="null_test_chat")
        run = self.run_repo.create_run(action="execute_task", session_id=sess["id"])

        self.sess_repo.delete_session(sess["id"])

        loaded_run = self.run_repo.get_run(run["id"])
        self.assertIsNotNone(loaded_run)
        self.assertIsNone(loaded_run["session_id"])


class TestSQLiteConcurrentWrites(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ufficio_concurrency_test_"))
        self.db_path = self.test_dir / "test_concurrency.db"
        self.db = DatabaseManager(self.db_path, auto_migrate=True)
        self.sess_repo = SessionRepository(self.db)
        self.run_repo = RunRepository(self.db)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_concurrent_writes_in_wal_mode(self):
        """
        Verifica che due connessioni/thread possano scrivere contemporaneamente su tabelle/record
        diversi senza sollevare 'sqlite3.OperationalError: database is locked'.
        """
        errors: List[Exception] = []
        threads_count = 10
        writes_per_thread = 15

        def worker_session(thread_idx: int):
            try:
                for i in range(writes_per_thread):
                    sess = self.sess_repo.create_session(
                        chat_id=f"thread_chat_{thread_idx}",
                        title=f"Session {thread_idx}-{i}",
                        metadata={"worker": thread_idx, "iter": i}
                    )
                    self.sess_repo.add_message(
                        session_id=sess["id"],
                        role="user",
                        content=f"Message {i} from thread {thread_idx}"
                    )
            except Exception as e:
                errors.append(e)

        def worker_runs(thread_idx: int):
            try:
                for i in range(writes_per_thread):
                    run = self.run_repo.create_run(
                        action="execute_task",
                        preset="light",
                        metadata={"worker": thread_idx, "iter": i}
                    )
                    self.run_repo.update_run_status(
                        run["id"],
                        status="completed",
                        progress=100,
                        result={"step": i}
                    )
            except Exception as e:
                errors.append(e)

        threads = []
        for t_idx in range(threads_count // 2):
            threads.append(threading.Thread(target=worker_session, args=(t_idx,)))
            threads.append(threading.Thread(target=worker_runs, args=(t_idx + 50,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errori rilevati durante scritture concorrenti: {errors}")

        # Verifica totale record scritti
        sessions = self.sess_repo.list_sessions(limit=1000)
        runs = self.run_repo.list_runs(limit=1000)
        expected_sessions = (threads_count // 2) * writes_per_thread
        expected_runs = (threads_count // 2) * writes_per_thread

        self.assertEqual(len(sessions), expected_sessions)
        self.assertEqual(len(runs), expected_runs)


if __name__ == "__main__":
    unittest.main()
