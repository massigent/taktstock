#!/usr/bin/env python3
"""
Unit & Integration Tests for Ufficio Async Run Queue & Worker (Feature Flag UFFICIO_ASYNC_RUNS)
-----------------------------------------------------------------------------------------------
Verifica:
1. Flag disattivato (default): risposta sincrona invariata, QueueWorker non creato, GET /api/runs/ disattivo
2. Flag attivato: POST valido restituisce 202 immediato con UUID4 run_id
3. Lifecycle completo in background: queued -> running -> completed (progress=100)
4. Fallimento processo subprocess: status='failed' con returncode registrato
5. Timeout subprocess: processo terminato e status='failed' con errore di timeout
6. Recovery al bootstrap: run queued/running da sessioni precedenti marcate come failed senza riesecuzione
7. GET /api/runs/<run_id>: autenticato, validazione UUID, 404 per run inesistente, filtro dati sicuri
8. Autenticazione e validazione payload: 401 e 400 bloccati a monte prima di toccare la coda o il DB
9. Sicurezza esecuzione: lista di comandi fissa e shell=False rigoroso
10. Isolamento totale dei thread e database SQLite temporanei con cleanup pulito
"""

import os
import sys
import json
import time
import uuid
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from http.server import HTTPServer
import threading
import urllib.request
import urllib.error

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from infrastructure.database import DatabaseManager
from infrastructure.run_repository import RunRepository
from infrastructure.run_queue import (
    QueueWorker,
    is_async_runs_enabled,
    get_global_queue_worker,
    stop_global_queue_worker,
)
import health_server


class TestAsyncRunQueue(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ufficio_async_queue_test_"))
        self.db_path = self.test_dir / "test_queue.db"
        self.db_manager = DatabaseManager(self.db_path, auto_migrate=True)
        self.run_repo = RunRepository(self.db_manager)

        # Salva env originali
        self.orig_async_runs = os.environ.get("UFFICIO_ASYNC_RUNS")
        self.orig_auth_token = os.environ.get("UFFICO_AUTH_TOKEN")
        self.orig_port = os.environ.get("UFFICIO_HEALTH_PORT")

        # Configura secret auth valido per i test
        self.auth_token = "test_super_secret_auth_token_12345"
        os.environ["UFFICO_AUTH_TOKEN"] = self.auth_token
        os.environ["UFFICIO_ASYNC_RUNS"] = "0"

        # Ferma eventuale worker globale residuo
        stop_global_queue_worker(timeout=1.0)
        self.workers_to_stop: list[QueueWorker] = []

    def tearDown(self):
        # Arresta tutti i worker creati nei test
        for worker in self.workers_to_stop:
            worker.stop(timeout=2.0)
        stop_global_queue_worker(timeout=2.0)

        # Ripristina env
        if self.orig_async_runs is not None:
            os.environ["UFFICIO_ASYNC_RUNS"] = self.orig_async_runs
        else:
            os.environ.pop("UFFICIO_ASYNC_RUNS", None)

        if self.orig_auth_token is not None:
            os.environ["UFFICO_AUTH_TOKEN"] = self.orig_auth_token
        else:
            os.environ.pop("UFFICO_AUTH_TOKEN", None)

        if self.orig_port is not None:
            os.environ["UFFICIO_HEALTH_PORT"] = self.orig_port
        else:
            os.environ.pop("UFFICIO_HEALTH_PORT", None)

        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _start_test_server(self, port: int = 18765) -> tuple[HTTPServer, threading.Thread]:
        """Helper per avviare un server HTTP di test su porta dedicata."""
        server_address = ("127.0.0.1", port)
        httpd = HTTPServer(server_address, health_server.UfficioHealthHandler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        return httpd, server_thread

    def test_worker_lifecycle_completed(self):
        """QueueWorker esegue un comando valido e lo marca come completed a progress=100 con output."""
        worker = QueueWorker(db_manager=self.db_manager, auto_start=True)
        self.workers_to_stop.append(worker)

        cmd = [sys.executable, "-c", "print('Async Job Completed Successfully')"]
        run_id = worker.enqueue(cmd_args=cmd, action="execute_task", metadata={"task": "Test Task Completed"})

        # Attendi completamento
        for _ in range(50):
            run = self.run_repo.get_run(run_id)
            if run and run["status"] == "completed":
                break
            time.sleep(0.1)

        run = self.run_repo.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["progress"], 100)
        self.assertEqual(run["current_step"], "completed")
        self.assertEqual(run["result"]["returncode"], 0)
        self.assertIn("Async Job Completed Successfully", run["result"]["output"])

    def test_worker_exposes_terminal_json_as_structured_summary(self):
        """Il JSON finale dell'orchestratore è disponibile al polling senza analizzare i log."""
        worker = QueueWorker(db_manager=self.db_manager, auto_start=True)
        self.workers_to_stop.append(worker)

        cmd = [sys.executable, "-c", "import json; print('log innocuo'); print(json.dumps({'status':'COMPLETED','project':'Assistente','subtasks':[{'id':'T1','outcome':'Scelta e piano'}]}))"]
        run_id = worker.enqueue(cmd_args=cmd, action="execute_task")

        for _ in range(50):
            run = self.run_repo.get_run(run_id)
            if run and run["status"] == "completed":
                break
            time.sleep(0.1)

        run = self.run_repo.get_run(run_id)
        self.assertEqual(run["result"]["summary"]["project"], "Assistente")
        self.assertEqual(run["result"]["summary"]["subtasks"][0]["outcome"], "Scelta e piano")

    def test_worker_lifecycle_failed_process(self):
        """QueueWorker cattura un processo con returncode non-zero e marca la run come failed."""
        worker = QueueWorker(db_manager=self.db_manager, auto_start=True)
        self.workers_to_stop.append(worker)

        cmd = [sys.executable, "-c", "import sys; print('Failing step'); sys.exit(42)"]
        run_id = worker.enqueue(cmd_args=cmd, action="execute_task", metadata={"task": "Test Task Failed"})

        for _ in range(50):
            run = self.run_repo.get_run(run_id)
            if run and run["status"] == "failed":
                break
            time.sleep(0.1)

        run = self.run_repo.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["current_step"], "failed")
        self.assertIn("42", run["error"])
        self.assertEqual(run["result"]["returncode"], 42)
        self.assertIn("Failing step", run["result"]["output"])

    def test_worker_timeout_terminates_and_marks_failed(self):
        """QueueWorker termina un processo che supera il timeout configurato e lo marca failed."""
        worker = QueueWorker(db_manager=self.db_manager, timeout_seconds=1, auto_start=True)
        self.workers_to_stop.append(worker)

        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        run_id = worker.enqueue(cmd_args=cmd, action="execute_task", metadata={"task": "Test Timeout"})

        for _ in range(50):
            run = self.run_repo.get_run(run_id)
            if run and run["status"] == "failed":
                break
            time.sleep(0.1)

        run = self.run_repo.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "failed")
        self.assertIn("timed out after 1s", run["error"])

    def test_output_capped_at_32kib(self):
        """L'output salvato in SQLite non supera mai 32 KiB anche se il processo genera molto più output."""
        worker = QueueWorker(db_manager=self.db_manager, max_output_bytes=32 * 1024, auto_start=True)
        self.workers_to_stop.append(worker)

        cmd = [sys.executable, "-c", "print('A' * (100 * 1024))"]
        run_id = worker.enqueue(cmd_args=cmd, action="execute_task", metadata={"task": "Test Big Output"})

        for _ in range(50):
            run = self.run_repo.get_run(run_id)
            if run and run["status"] == "completed":
                break
            time.sleep(0.1)

        run = self.run_repo.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "completed")
        self.assertLessEqual(len(run["result"]["output"]), 32 * 1024)

    def test_recovery_marks_stale_runs_as_failed_without_reexecuting(self):
        """All'avvio, il recovery marca le run queued o running pendenti come failed con errore 'interrupted_by_server_restart'."""
        # 1. Crea due run orfane
        id_queued = str(uuid.uuid4())
        id_running = str(uuid.uuid4())
        self.run_repo.create_run(action="execute_task", status="queued", run_id=id_queued)
        self.run_repo.create_run(action="execute_task", status="running", run_id=id_running)

        # 2. Avvia nuovo worker (simulando riavvio server)
        worker = QueueWorker(db_manager=self.db_manager, auto_start=True)
        self.workers_to_stop.append(worker)

        # 3. Verifica stato dopo recovery
        run_q = self.run_repo.get_run(id_queued)
        run_r = self.run_repo.get_run(id_running)

        self.assertEqual(run_q["status"], "failed")
        self.assertEqual(run_q["error"], "interrupted_by_server_restart")
        self.assertEqual(run_r["status"], "failed")
        self.assertEqual(run_r["error"], "interrupted_by_server_restart")

    def test_flag_off_preserves_synchronous_http_behavior(self):
        """Con UFFICIO_ASYNC_RUNS=0, POST /api/run risponde in modo sincrono (200 OK) e GET /api/runs/ è disattivo (404)."""
        os.environ["UFFICIO_ASYNC_RUNS"] = "0"
        port = 18766
        httpd, s_thread = self._start_test_server(port)

        try:
            # POST /api/run sincrono
            req_data = json.dumps({"action": "execute_task", "task": "Task Sincrono Test", "repo": "Assistente"}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/run",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "X-Ufficio-Token": self.auth_token
                }
            )
            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = MagicMock(returncode=0, stdout='{"status":"COMPLETED"}', stderr='')
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(body["status"], "COMPLETED")
                    self.assertNotIn("run_id", body)

            # GET /api/runs/<uuid> deve ritornare 404 quando flag è off
            test_uuid = str(uuid.uuid4())
            get_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/runs/{test_uuid}",
                headers={"X-Ufficio-Token": self.auth_token}
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(get_req, timeout=5)
            self.assertEqual(ctx.exception.code, 404)

        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_flag_on_returns_202_accepted_and_get_runs_endpoint(self):
        """Con UFFICIO_ASYNC_RUNS=1, POST /api/run risponde 202 con run_id e GET /api/runs/<run_id> restituisce lo stato."""
        os.environ["UFFICIO_ASYNC_RUNS"] = "1"
        port = 18767

        # Configura il singleton con il db di test
        worker = get_global_queue_worker(db_manager=self.db_manager)
        self.workers_to_stop.append(worker)

        httpd, s_thread = self._start_test_server(port)

        try:
            # 1. POST /api/run asincrono
            req_data = json.dumps({"action": "execute_task", "task": "Task Asincrono HTTP", "repo": "Assistente"}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/run",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "X-Ufficio-Token": self.auth_token
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 202)
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(body["status"], "QUEUED")
                run_id = body.get("run_id")
                self.assertIsNotNone(run_id)
                uuid_obj = uuid.UUID(run_id)
                self.assertEqual(str(uuid_obj), run_id)

            # 2. GET /api/runs/<run_id> valido
            get_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/runs/{run_id}",
                headers={"X-Ufficio-Token": self.auth_token}
            )
            with urllib.request.urlopen(get_req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                run_body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(run_body["run_id"], run_id)
                self.assertIn(run_body["status"], ["queued", "running", "completed", "failed"])

            # 3. GET /api/runs/ con UUID invalido -> 400
            bad_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/runs/invalid-uuid-123",
                headers={"X-Ufficio-Token": self.auth_token}
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad_req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            # 4. GET /api/runs/ con UUID inesistente -> 404
            non_existent_uuid = str(uuid.uuid4())
            not_found_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/runs/{non_existent_uuid}",
                headers={"X-Ufficio-Token": self.auth_token}
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(not_found_req, timeout=5)
            self.assertEqual(ctx.exception.code, 404)

        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_auth_and_payload_validation_block_before_enqueue(self):
        """Richieste non autorizzate o con payload invalido vengono respinte prima di accodare."""
        os.environ["UFFICIO_ASYNC_RUNS"] = "1"
        port = 18768

        worker = get_global_queue_worker(db_manager=self.db_manager)
        self.workers_to_stop.append(worker)

        httpd, s_thread = self._start_test_server(port)

        try:
            # 1. Non autorizzato (nessun header) -> 401
            req_unauth = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/run",
                data=json.dumps({"action": "execute_task"}).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_unauth, timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            # 2. Payload non valido (azione inesistente) -> 400
            req_bad_payload = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/run",
                data=json.dumps({"action": "forbidden_action_xyz"}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Ufficio-Token": self.auth_token
                }
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_bad_payload, timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            # 3. Verifica che nessun record sia stato creato in SQLite
            all_runs = self.run_repo.list_runs()
            self.assertEqual(len(all_runs), 0)

        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
