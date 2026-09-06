#!/usr/bin/env python3
"""
Unit Tests for /api/run and /api/execute Security Hardening
----------------------------------------------------------
Testa:
1. Rifiuto esplicito di payload con 'cliCommand' (es. python3 -c '...')
2. Rifiuto di campi ignoti o non consentiti nel JSON
3. Rifiuto di azioni non consentite o mancanti
4. Rifiuto di repository non presenti nel catalogo o URL arbitrari
5. Accettazione di payload chat/brainstorm/execute/approve/reject/clear validi
6. Risoluzione sicura del repository tramite ProjectsManager
7. Costruzione rigorosa e deterministica degli argomenti CLI per orchestrator_core.py
8. Compatibilità funzionale per menzioni chat (@sol, @agy, @luna, /agenti)
"""

import os
import sys
import io
import json
import base64
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from health_server import (
    validate_api_run_payload,
    build_orchestrator_cmd_args,
    resolve_catalog_repo,
    parse_command_text,
    resolve_active_project_for_chat,
    ALLOWED_API_ACTIONS,
    ALLOWED_API_FIELDS,
    ALLOWED_API_PRESETS,
    UfficioHealthHandler,
)


class TestApiRunPayloadValidation(unittest.TestCase):
    def test_clicommand_with_python_code_strictly_rejected(self):
        """1. cliCommand con python3 -c ... deve essere categoricamente rifiutato."""
        payload = {
            "cliCommand": "python3 -c 'import os; os.system(\"rm -rf /\")'",
            "action": "chat"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("cliCommand", err_msg)
        self.assertIsNone(data)

    def test_unknown_fields_rejected(self):
        """2. Campi ignoti (es. webhook_url, arbitrary_field) devono essere rifiutati."""
        payload = {
            "action": "chat",
            "message": "Ciao",
            "webhook_url": "https://attacker.com/leak",
            "extra_param": 123
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Campi non consentiti", err_msg)
        self.assertIn("webhook_url", err_msg)
        self.assertIsNone(data)

    def test_unknown_action_rejected(self):
        """3. Azioni non consentite devono essere rifiutate con errore 400."""
        payload = {
            "action": "system_exec",
            "message": "test"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Azione non valida", err_msg)
        self.assertIsNone(data)

    def test_missing_action_rejected(self):
        payload = {
            "message": "Manca action"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Azione non valida o mancante", err_msg)

    def test_invalid_types_rejected(self):
        """Tipi non stringa (es. int, list, dict) devono essere rifiutati."""
        payload = {
            "action": "chat",
            "message": ["lista", "non", "consentita"]
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("deve essere una stringa", err_msg)

    def test_non_cataloged_repository_rejected(self):
        """4. Repository non presenti nel catalogo devono essere rifiutati."""
        payload = {
            "action": "chat",
            "message": "ciao",
            "repo": "progetto_inesistente_xyz_123"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Repository non valido", err_msg)

    def test_arbitrary_url_repository_rejected(self):
        """URL arbitrari (es. https://github.com/...) devono essere rifiutati."""
        payload = {
            "action": "chat",
            "message": "ciao",
            "repo": "https://github.com/evil/repo.git"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Repository non valido", err_msg)

    def test_path_traversal_repository_rejected(self):
        """Tentativi di path traversal nel nome repo devono essere rifiutati."""
        payload = {
            "action": "chat",
            "message": "ciao",
            "repo": "../../etc/passwd"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Repository non valido", err_msg)

    def test_invalid_preset_rejected(self):
        payload = {
            "action": "chat",
            "preset": "super_mega_preset"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Preset non valido", err_msg)

    def test_valid_chat_payload_accepted(self):
        """5. Payload chat valido viene validato e normalizzato correttamente."""
        payload = {
            "action": "chat",
            "chat_id": "12345",
            "message": "@sol analizza l'architettura",
            "preset": "standard"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertTrue(is_valid)
        self.assertIsNone(err_msg)
        self.assertEqual(data["action"], "chat")
        self.assertEqual(data["chat_id"], "12345")
        self.assertEqual(data["message"], "@sol analizza l'architettura")
        self.assertEqual(data["preset"], "standard")

    def test_parse_command_text_sviluppa_and_task(self):
        """Verifica che /sviluppa e /task vengano parsati come execute_task con estrazione repo e preset."""
        res1 = parse_command_text("/sviluppa crea nuovo modulo --critical repo:Assistente")
        self.assertEqual(res1["action"], "execute_task")
        self.assertEqual(res1["text"], "crea nuovo modulo")
        self.assertEqual(res1["preset"], "critical")
        self.assertEqual(res1["repo"], "Assistente")
        self.assertFalse(res1["is_chat"])

        res2 = parse_command_text("/task verifica sicurezza light", default_preset="standard")
        self.assertEqual(res2["action"], "execute_task")
        self.assertIn("verifica sicurezza", res2["text"])
        self.assertFalse(res2["is_chat"])

    def test_execute_task_without_repo_strictly_rejected(self):
        """execute_task senza repo (e senza chat attiva con repo) deve fallire senza fallback a default workspace."""
        payload = {
            "action": "execute_task",
            "task": "Esegui migrazione DB"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Nessun progetto selezionato", err_msg)
        self.assertIsNone(data)

    def test_sviluppa_in_chat_payload_promoted_to_execute_task(self):
        """Un payload con action=chat ma testo /sviluppa viene promosso ad execute_task."""
        payload = {
            "action": "chat",
            "message": "/sviluppa ottimizza query SQL",
            "repo": "Assistente"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertTrue(is_valid)
        self.assertEqual(data["action"], "execute_task")
        self.assertEqual(data["task"], "ottimizza query SQL")
        self.assertEqual(data["repo"].lower(), "assistente")

    def test_sviluppa_in_chat_payload_without_repo_rejected(self):
        """Un payload con /sviluppa senza repo viene categoricamente bloccato."""
        payload = {
            "action": "chat",
            "message": "/sviluppa ottimizza query SQL"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Nessun progetto selezionato", err_msg)

    def test_explicit_chat_remains_chat(self):
        """Comandi chat espliciti (/chat, @sol, discussione libera) rimangono chat."""
        payload1 = {
            "action": "chat",
            "message": "/chat spiegami il pattern sidecar"
        }
        is_valid, err_msg, data = validate_api_run_payload(payload1)
        self.assertTrue(is_valid)
        self.assertEqual(data["action"], "chat")
        self.assertEqual(data["message"], "spiegami il pattern sidecar")

        payload2 = {
            "action": "chat",
            "message": "@sol qual è il piano per oggi?"
        }
        is_valid2, err_msg2, data2 = validate_api_run_payload(payload2)
        self.assertTrue(is_valid2)
        self.assertEqual(data2["action"], "chat")

    @patch("health_server.resolve_active_project_for_chat", return_value="Assistente")
    def test_execute_task_inherits_active_telegram_project(self, resolve_active_project):
        payload = {
            "action": "execute_task",
            "chat_id": "511090810",
            "task": "Controlla il progetto attivo",
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertTrue(is_valid)
        self.assertIsNone(err_msg)
        self.assertEqual(data["repo"], "Assistente")
        resolve_active_project.assert_called_once_with("511090810")

    @patch("health_server.resolve_active_project_for_chat", return_value=None)
    def test_execute_task_without_active_telegram_project_is_rejected(self, _resolve_active_project):
        payload = {
            "action": "execute_task",
            "chat_id": "511090810",
            "task": "Non usare il workspace dell'Ufficio",
        }
        is_valid, err_msg, data = validate_api_run_payload(payload)
        self.assertFalse(is_valid)
        self.assertIn("Nessun progetto selezionato", err_msg)
        self.assertIsNone(data)

class TestOrchestratorCommandBuilder(unittest.TestCase):
    def test_build_chat_command(self):
        data = {
            "action": "chat",
            "chat_id": "9999",
            "message": "Ciao Sol",
            "task": None,
            "repo": None,
            "preset": "standard"
        }
        cmd = build_orchestrator_cmd_args(data)
        self.assertEqual(cmd[0], "python3")
        self.assertIn("orchestrator_core.py", cmd[1])
        self.assertIn("--chat-id", cmd)
        self.assertIn("9999", cmd)
        self.assertIn("--preset", cmd)
        self.assertIn("standard", cmd)
        self.assertIn("--brainstorm-continue", cmd)
        self.assertIn("active", cmd)
        self.assertIn("--msg-b64", cmd)
        # Decodifica base64 per verificare il contenuto
        idx = cmd.index("--msg-b64") + 1
        decoded = base64.b64decode(cmd[idx].encode("utf-8")).decode("utf-8")
        self.assertEqual(decoded, "Ciao Sol")

    def test_build_brainstorm_command(self):
        data = {
            "action": "brainstorm",
            "chat_id": "8888",
            "message": None,
            "task": "Pianifica nuovo modulo webhook",
            "repo": "server_debian",
            "preset": "critical"
        }
        cmd = build_orchestrator_cmd_args(data)
        self.assertEqual(cmd[0], "python3")
        self.assertIn("--brainstorm", cmd)
        self.assertIn("--repo", cmd)
        self.assertIn("server_debian", cmd)
        self.assertIn("--task-b64", cmd)
        idx = cmd.index("--task-b64") + 1
        decoded = base64.b64decode(cmd[idx].encode("utf-8")).decode("utf-8")
        self.assertEqual(decoded, "Pianifica nuovo modulo webhook")

    def test_build_execute_task_command(self):
        data = {
            "action": "execute_task",
            "chat_id": "7777",
            "message": None,
            "task": "Esegui test sicurezza",
            "repo": None,
            "preset": "light"
        }
        cmd = build_orchestrator_cmd_args(data)
        self.assertIn("--task-b64", cmd)
        self.assertIn("--preset", cmd)
        self.assertIn("light", cmd)

    def test_build_execute_task_with_chat_mention_redirects_to_buzz_chat(self):
        """Menzioni come @luna, @agy, /agenti, /luna, /sol, /progetti in execute_task vengono instradate alla Buzz Chat."""
        data = {
            "action": "execute_task",
            "chat_id": "6666",
            "message": "@luna aggiorna workflow n8n",
            "task": None,
            "repo": None,
            "preset": "luna_flash"
        }
        cmd = build_orchestrator_cmd_args(data)
        self.assertIn("--brainstorm-continue", cmd)
        self.assertIn("active", cmd)
        self.assertIn("--msg-b64", cmd)

        # Test anche con slash command come /luna o /sol o /progetti
        for slash_cmd in ["/luna ciao", "/sol analizza architettura", "/progetti"]:
            data_slash = {
                "action": "execute_task",
                "chat_id": "6666",
                "message": slash_cmd,
                "task": None,
                "repo": None,
                "preset": "standard"
            }
            cmd_slash = build_orchestrator_cmd_args(data_slash)
            self.assertIn("--brainstorm-continue", cmd_slash, f"Fallito per {slash_cmd}")
            self.assertIn("active", cmd_slash)
            self.assertIn("--msg-b64", cmd_slash)

    def test_build_approve_command(self):
        data = {
            "action": "approve",
            "chat_id": "5555",
            "message": None,
            "task": None,
            "repo": None,
            "preset": None
        }
        cmd = build_orchestrator_cmd_args(data)
        self.assertIn("--brainstorm-approve", cmd)
        self.assertIn("active", cmd)

    def test_build_reject_command(self):
        data = {
            "action": "reject",
            "chat_id": "4444",
            "message": "Motivo del rifiuto",
            "task": None,
            "repo": None,
            "preset": None
        }
        cmd = build_orchestrator_cmd_args(data)
        self.assertIn("--brainstorm-reject", cmd)
        self.assertIn("active", cmd)
        self.assertIn("--msg-b64", cmd)

    def test_build_clear_command(self):
        data = {
            "action": "clear",
            "chat_id": "3333",
            "message": None,
            "task": None,
            "repo": None,
            "preset": None
        }
        cmd = build_orchestrator_cmd_args(data)
        self.assertIn("--chat-clear", cmd)
        self.assertIn("--chat-id", cmd)


class TestHttpHandlerApiRunEndpoint(unittest.TestCase):
    def _create_mock_handler(self, post_body_dict=None, raw_body=None, path="/api/run"):
        import io
        handler = MagicMock(spec=UfficioHealthHandler)
        handler.headers = {"Content-Type": "application/json"}
        handler.path = path

        if raw_body is not None:
            body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        elif post_body_dict is not None:
            body_bytes = json.dumps(post_body_dict).encode("utf-8")
        else:
            body_bytes = b""

        handler.headers["Content-Length"] = str(len(body_bytes))
        handler.rfile = io.BytesIO(body_bytes)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler._is_authenticated = MagicMock(return_value=True)
        handler.do_POST = UfficioHealthHandler.do_POST.__get__(handler, UfficioHealthHandler)
        return handler

    def test_endpoint_rejects_clicommand_with_http_400(self):
        payload = {"cliCommand": "python3 -c 'print(1)'"}
        handler = self._create_mock_handler(post_body_dict=payload)
        handler.do_POST()
        handler.send_response.assert_called_with(400)
        out = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("cliCommand", out)

    def test_endpoint_rejects_malformed_json_with_http_400(self):
        handler = self._create_mock_handler(raw_body="INVALID_JSON{broken")
        handler.do_POST()
        handler.send_response.assert_called_with(400)
        out = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("JSON non valido", out)

    def test_endpoint_rejects_empty_chat_with_http_400(self):
        """Test obbligatorio: chat senza message/task restituisce HTTP 400 e il runner non viene invocato."""
        payload = {"action": "chat", "chat_id": "12345", "message": "   "}
        handler = self._create_mock_handler(post_body_dict=payload)
        handler.do_POST()
        handler.send_response.assert_called_with(400)
        out = handler.wfile.getvalue().decode("utf-8")
        self.assertTrue("obbligatorio" in out or "vuoto" in out)

    def test_chat_with_luna_and_chat_id_has_msg_b64(self):
        """Test obbligatorio: chat valida con @luna e chat_id ha --msg-b64 presente."""
        payload = {
            "action": "chat",
            "chat_id": "511090810",
            "message": "@luna ci sei?",
            "preset": "standard"
        }
        is_valid, err_msg, sanitized = validate_api_run_payload(payload)
        self.assertTrue(is_valid)
        cmd_args = build_orchestrator_cmd_args(sanitized)
        self.assertIn("--msg-b64", cmd_args)
        self.assertIn("--chat-id", cmd_args)
        self.assertIn("511090810", cmd_args)
        # Verifica che il messaggio decodificato corrisponda esattamente a @luna ci sei?
        idx = cmd_args.index("--msg-b64")
        decoded = base64.b64decode(cmd_args[idx + 1]).decode("utf-8")
        self.assertEqual(decoded, "@luna ci sei?")

    def test_brainstorm_continue_active_without_session_exits_cleanly(self):
        """Test obbligatorio: --brainstorm-continue active senza sessione produce errore controllato senza traceback."""
        import subprocess
        test_chat_id = "test_empty_chat_987654"
        cmd = [
            sys.executable,
            str(SERVER_DIR / "orchestrator_core.py"),
            "--chat-id", test_chat_id,
            "--brainstorm-continue", "active",
            "--mock"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("Traceback", res.stderr)
        self.assertNotIn("Traceback", res.stdout)
        self.assertNotIn("Brainstorm active non trovato", res.stderr)
        self.assertIn("Nessuna sessione", res.stdout + res.stderr)

    def test_first_chat_without_session_creates_active_session_and_responds(self):
        """Test obbligatorio: prima chat senza sessione crea automaticamente sessione attiva e risponde."""
        import subprocess, tempfile
        test_chat_id = f"test_first_chat_{os.getpid()}"
        b64_msg = base64.b64encode(b"@sol ciao prima volta").decode("utf-8")
        cmd = [
            sys.executable,
            str(SERVER_DIR / "orchestrator_core.py"),
            "--chat-id", test_chat_id,
            "--msg-b64", b64_msg,
            "--mock"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("Traceback", res.stderr)
        # L'output stdout deve essere un json valido con brainstorm_id e replies
        data = json.loads(res.stdout)
        self.assertIn("brainstorm_id", data)
        self.assertIn("replies", data)
        self.assertTrue(len(data["replies"]) > 0)


class TestWebDashboardRoutingAndForms(unittest.TestCase):
    def test_dashboard_form_with_sviluppa_enqueues_execute_task(self):
        """Form dashboard con /sviluppa crea run execute_task con repo selezionato."""
        handler = MagicMock()
        handler.path = "/brainstorm/new"
        handler.wfile = io.BytesIO()
        handler._is_authenticated.return_value = True

        post_data = "task=%2Fsviluppa+crea+nuova+feature&repo=Assistente&preset=standard"
        handler.rfile = io.BytesIO(post_data.encode("utf-8"))
        handler.headers = {"Content-Length": str(len(post_data))}

        with patch("infrastructure.run_queue.get_global_queue_worker") as mock_worker_getter, \
             patch("infrastructure.run_queue.is_async_runs_enabled", return_value=True):
            mock_worker = MagicMock()
            mock_worker_getter.return_value = mock_worker

            UfficioHealthHandler.do_POST(handler)

            handler.send_response.assert_called_with(303)
            mock_worker.enqueue.assert_called_once()
            call_kwargs = mock_worker.enqueue.call_args.kwargs
            self.assertEqual(call_kwargs["action"], "execute_task")
            self.assertEqual(call_kwargs["repo"].lower(), "assistente")
            self.assertEqual(call_kwargs["preset"], "standard")

    def test_dashboard_form_without_repo_returns_400_with_error_page(self):
        """Form dashboard senza repo selezionato blocca l'avvio con errore 400 e messaggio chiaro."""
        handler = MagicMock()
        handler.path = "/brainstorm/new"
        handler.wfile = io.BytesIO()
        handler._is_authenticated.return_value = True

        post_data = "task=%2Fsviluppa+crea+nuova+feature&repo=&preset=standard"
        handler.rfile = io.BytesIO(post_data.encode("utf-8"))
        handler.headers = {"Content-Length": str(len(post_data))}

        UfficioHealthHandler.do_POST(handler)

        handler.send_response.assert_called_with(400)
        out = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("Seleziona un progetto valido", out)


if __name__ == "__main__":
    unittest.main()
