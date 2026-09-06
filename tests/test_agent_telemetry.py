#!/usr/bin/env python3
"""
Unit Tests for Agent Usage Telemetry (Phase 1 Observability)
------------------------------------------------------------
Test obbligatori:
1. Privacy: nessun salvataggio di prompt, risposte, chiavi, auth o percorsi assoluti.
2. Affidabilità: errori DB/logging non bloccanti; nessuna eccezione propagata.
3. Integrità: esattamente una riga append-only per ogni invocazione agente.
4. Token reali: reported_tokens popolato solo se esposto dal runtime, altrimenti NULL.
5. Aggregazione: correttezza del sommario e del report CLI su 7 giorni con escalation.
"""

import os
import sys
import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from infrastructure.database import DatabaseManager
from infrastructure.telemetry_repository import (
    TelemetryRepository,
    extract_reported_tokens,
    sanitize_telemetry_metadata
)
from infrastructure.agent_gateway import AgentGateway


class TestAgentTelemetry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_telemetry.db"
        self.db_manager = DatabaseManager(db_path=self.db_path, auto_migrate=True)
        self.repo = TelemetryRepository(db_manager=self.db_manager)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_privacy_guarantees(self):
        """1. Privacy: non salvare prompt, risposte, token, auth, path assoluti, stderr o output tool."""
        sensitive_prompt = "SEGRETO: prompt riservato con password123 e apiKey=xyz"
        sensitive_metadata = {
            "preset": "critical",
            "reasoning_effort": "high",
            "prompt_raw": sensitive_prompt,  # Chiave non consentita
            "auth_token": "bearer_super_secret_token_123",  # Chiave non consentita
            "host_path": "/Users/massimo/Documents/secret_project/file.py",  # Chiave non consentita
            "stderr": "Traceback inside file.py",  # Chiave non consentita
            "sandbox": "read-only"  # Chiave consentita
        }

        event_id = self.repo.record_event(
            agent_role="sol",
            provider="openai_codex",
            model="gpt-5.6-sol",
            phase="chat",
            status="SUCCESS",
            duration_ms=1500,
            prompt_length=len(sensitive_prompt),
            reported_tokens=2500,
            escalation_reason="critical_preset",
            metadata=sensitive_metadata
        )
        self.assertIsNotNone(event_id)

        # Verifica diretta sul database SQLite
        with self.db_manager.connection() as conn:
            row = conn.execute("SELECT * FROM agent_usage_events WHERE id = ?", (event_id,)).fetchone()
            self.assertIsNotNone(row)
            row_dict = dict(row)

            # Verifica campi memorizzati
            self.assertEqual(row_dict["prompt_length"], len(sensitive_prompt))
            self.assertEqual(row_dict["reported_tokens"], 2500)
            self.assertEqual(row_dict["escalation_reason"], "critical_preset")

            # Verifica assenza assoluta di stringhe sensibili
            for col_val in row_dict.values():
                val_str = str(col_val)
                self.assertNotIn("password123", val_str)
                self.assertNotIn("bearer_super_secret_token_123", val_str)
                self.assertNotIn("/Users/massimo/Documents", val_str)
                self.assertNotIn("Traceback", val_str)

            # Verifica metadata_json sanitizzato
            meta_json = row_dict["metadata_json"]
            self.assertIn('"preset":"critical"', meta_json)
            self.assertIn('"sandbox":"read-only"', meta_json)
            self.assertNotIn("auth_token", meta_json)
            self.assertNotIn("prompt_raw", meta_json)

    def test_non_blocking_on_database_error(self):
        """2. Affidabilità: qualunque errore DB viene intercettato e non interrompe l'esecuzione."""
        # Simula rottura del database manager
        mock_db = MagicMock()
        mock_db.transaction.side_effect = sqlite3.OperationalError("database is locked (simulato)")

        broken_repo = TelemetryRepository(db_manager=mock_db)
        # La registrazione deve restituire None senza sollevare eccezioni
        res = broken_repo.record_event(
            agent_role="sol",
            provider="openai_codex",
            model="gpt-5.6-sol",
            prompt_length=50
        )
        self.assertIsNone(res)

        # Testa anche attraverso AgentGateway
        with patch.object(AgentGateway, "get_telemetry_repository", return_value=broken_repo):
            telemetry_res = AgentGateway.record_telemetry(
                agent_role="sol",
                provider="openai_codex",
                model="gpt-5.6-sol",
                prompt_length=50
            )
            self.assertIsNone(telemetry_res)

    def test_single_row_per_call_in_agent_gateway(self):
        """3. Integrità: esattamente una riga append-only per ogni invocazione in AgentGateway."""
        with patch.dict(os.environ, {"UFFICIO_HOST_CODEX_SIDECAR": "1"}):
            with patch.object(AgentGateway, "get_telemetry_repository", return_value=self.repo):
                mock_client = MagicMock()
                mock_client.send_request.return_value = "Risposta mock\n\ntokens used\n3420"

                with patch("infrastructure.host_codex_client.HostCodexClient", return_value=mock_client):
                    success, out, meta = AgentGateway.execute_agent_call(
                        agent_role="sol",
                        prompt="Test prompt",
                        preset="standard"
                    )
                    self.assertTrue(success)

        with self.db_manager.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_usage_events").fetchone()[0]
            self.assertEqual(count, 1)

            row = conn.execute("SELECT * FROM agent_usage_events").fetchone()
            self.assertEqual(row["agent_role"], "sol")
            self.assertEqual(row["model"], "gpt-5.6-sol")
            self.assertEqual(row["provider"], "openai_codex")
            self.assertEqual(row["status"], "SUCCESS")
            self.assertEqual(row["reported_tokens"], 3420)
            self.assertEqual(row["prompt_length"], len("Test prompt"))

    def test_token_extraction_rules(self):
        """4. Token: estrae reported_tokens solo se attendibile, altrimenti NULL senza inventare o stimare."""
        out_with_tokens = "Risultato analisi.\ntokens used\n4150\nCompletato."
        self.assertEqual(extract_reported_tokens(out_with_tokens), 4150)

        out_without_tokens = "Risultato analisi senza metadati di token."
        self.assertIsNone(extract_reported_tokens(out_without_tokens))

        # Estrazione da metadata JSON API
        meta_with_usage = {"usage": {"total_tokens": 1280}}
        self.assertEqual(extract_reported_tokens("", meta_with_usage), 1280)

        meta_empty = {}
        self.assertIsNone(extract_reported_tokens("", meta_empty))

    def test_aggregation_and_cli_reporting(self):
        """5. Aggregazione: verifica calcolo medie, success/fail e raggruppamento escalation."""
        # Inserimento eventi sintetici
        # Sol: 2 success (1000ms e 2000ms -> media 1500ms), reported_tokens 1000 + 2000 = 3000
        self.repo.record_event(
            agent_role="sol",
            provider="openai_codex",
            model="gpt-5.6-sol",
            status="SUCCESS",
            duration_ms=1000,
            reported_tokens=1000,
            escalation_reason="critical_preset"
        )
        self.repo.record_event(
            agent_role="sol",
            provider="openai_codex",
            model="gpt-5.6-sol",
            status="SUCCESS",
            duration_ms=2000,
            reported_tokens=2000,
            escalation_reason="critical_preset"
        )

        # Luna: 1 success (800ms, 1500 tokens), 1 error (400ms, None tokens)
        self.repo.record_event(
            agent_role="luna",
            provider="openai_codex",
            model="gpt-5.6-terra",
            status="SUCCESS",
            duration_ms=800,
            reported_tokens=1500
        )
        self.repo.record_event(
            agent_role="luna",
            provider="openai_codex",
            model="gpt-5.6-terra",
            status="ERROR",
            duration_ms=400,
            reported_tokens=None,
            error_category="oauth_expired"
        )

        # Deepseek: 1 success con escalation
        self.repo.record_event(
            agent_role="ds-flash",
            provider="deepseek",
            model="deepseek-coder-flash",
            status="SUCCESS",
            duration_ms=500,
            reported_tokens=800,
            escalation_reason="code_review"
        )

        summary = self.repo.get_usage_summary(days=7)
        self.assertEqual(summary["total_calls"], 5)
        self.assertEqual(summary["total_reported_tokens"], 5300)

        # Verifica agenti
        agents_dict = {a["agent_role"]: a for a in summary["agents"]}
        self.assertIn("sol", agents_dict)
        self.assertEqual(agents_dict["sol"]["total_calls"], 2)
        self.assertEqual(agents_dict["sol"]["success_calls"], 2)
        self.assertEqual(agents_dict["sol"]["failed_calls"], 0)
        self.assertEqual(agents_dict["sol"]["avg_duration_ms"], 1500.0)
        self.assertEqual(agents_dict["sol"]["total_reported_tokens"], 3000)

        self.assertIn("luna", agents_dict)
        self.assertEqual(agents_dict["luna"]["total_calls"], 2)
        self.assertEqual(agents_dict["luna"]["success_calls"], 1)
        self.assertEqual(agents_dict["luna"]["failed_calls"], 1)
        self.assertEqual(agents_dict["luna"]["avg_duration_ms"], 600.0)
        self.assertEqual(agents_dict["luna"]["total_reported_tokens"], 1500)

        # Verifica escalation
        esc_dict = {e["escalation_reason"]: e["count"] for e in summary["escalations"]}
        self.assertEqual(esc_dict["critical_preset"], 2)
        self.assertEqual(esc_dict["code_review"], 1)

        # Verifica formattazione report CLI
        cli_report = self.repo.format_cli_report(days=7)
        self.assertIn("REPORT UTILIZZO AGENTI", cli_report)
        self.assertIn("gpt-5.6-sol", cli_report)
        self.assertIn("gpt-5.6-terra", cli_report)
        self.assertIn("critical_preset: 2", cli_report)
        self.assertIn("TOTALI: 5 chiamate | 5,300 token riportati", cli_report)


if __name__ == "__main__":
    unittest.main()
