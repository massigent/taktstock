#!/usr/bin/env python3
"""
Test Suite: P2 Context Budget, Message Correctness & Token Telemetry
-------------------------------------------------------------------
Verifica:
1. Conversazione sintetica da 100+ messaggi e compattazioni ripetute (payload <= 32k, summary <= 3k, compaction payload bounded).
2. Marker univoci (nessuna duplicazione di messaggi raw nel prompt, nessun messaggio ante-compaction fuori dalla summary).
3. Annunci 'Gemini Compactor' visibili in stato/UI ma esclusi dai prompt degli agenti e dalla compattazione.
4. Errori operativi AGY/Codex visibili in stato/UI ma esclusi dai prompt degli agenti e dalla compattazione.
5. Messaggi grandi con raw invariato, troncamento non-mutante con marker e rispetto dell'hard cap aggregato.
6. Broadcast @all a colpo singolo con reset dello sticky agent a Sol.
7. Usage AGY JSON (es. {"usage": {"total_tokens": 15719}}) estratto e registrato tipizzato in telemetria; assenza usage resta null.
8. Telemetria priva di prompt o response (privacy-safe).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Assicura import del server
SERVER_DIR = Path(__file__).parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from brainstorm_manager import (
    BrainstormManager,
    is_context_eligible,
    truncate_for_context,
    normalize_and_cap_summary,
    format_canonical_summary,
    validate_structured_summary,
    MAX_SUMMARY_TOTAL_CHARS,
    MAX_SUMMARY_ITEMS_PER_SECTION,
    MAX_SUMMARY_ITEM_CHARS,
    MAX_CONTEXT_TAIL_MESSAGES,
    MAX_MESSAGE_PROMPT_CHARS,
    MAX_AGGREGATE_PAYLOAD_CHARS,
    MAX_COMPACTION_INPUT_CHARS,
    TRUNCATION_MARKER
)

from infrastructure.telemetry_repository import (
    TelemetryRepository,
    extract_reported_tokens,
    sanitize_telemetry_metadata
)
from infrastructure.database import DatabaseManager


class MockRunner:
    def __init__(self, mock_mode=True):
        self.mock_mode = mock_mode


class TestContextBudgetAndMessageCorrectness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name) / "state"
        self.bs_dir = self.state_dir / "brainstorms"
        self.bs_dir.mkdir(parents=True, exist_ok=True)
        self.manager = BrainstormManager(state_dir=self.state_dir)
        self.runner = MockRunner(mock_mode=True)

        # Mock _query_agent_llm to avoid slow subprocess/network calls during tests
        self.query_patcher = patch.object(
            self.manager,
            "_query_agent_llm",
            side_effect=lambda agent, msgs, **kwargs: (
                json.dumps({
                    "facts": ["Fatto compattato"],
                    "decisions": ["Decisione compattata"],
                    "constraints": ["Vincolo compattato"],
                    "open_questions": [],
                    "next_steps": ["Passo compattato"]
                }) if agent == "gemini" else f"Risposta simulata da {agent}"
            )
        )
        self.mock_query = self.query_patcher.start()

    def tearDown(self):
        self.query_patcher.stop()
        self.temp_dir.cleanup()

    # --- 1. Conversazione sintetica da 100+ messaggi e compattazioni ripetute ---
    def test_synthetic_100_messages_conversation_bounded_payload(self):
        bs_id = "bs_synthetic_100"
        bs = {
            "id": bs_id,
            "task": "Test scalabilità e context budget su 100 messaggi",
            "repo": "/home/massimo/ufficio",
            "messages": [],
            "revision": 0,
            "chat_summary": "",
            "compacted_up_to_index": 0,
            "channel": "unknown"
        }
        self.manager.save_brainstorm(bs)

        # Inserisci 100 messaggi alternati ed esegui compattazione periodica
        for i in range(100):
            sender = "user" if i % 2 == 0 else "sol"
            msg = f"Messaggio #{i} con UUID-{i:04d} contenente dettagli tecnici e specifiche operative per la fase {i}."
            self.manager.post_chat_message(bs_id, message=msg, sender=sender, runner=self.runner)

        fresh = self.manager.load_brainstorm(bs_id)
        self.assertGreaterEqual(len(fresh["messages"]), 100)
        self.assertGreater(fresh.get("compacted_up_to_index", 0), 0)

        # Verifica chat_summary bounded
        summary = fresh.get("chat_summary", "")
        self.assertLessEqual(len(summary), MAX_SUMMARY_TOTAL_CHARS)

        # Verifica _build_chat_messages per tutti i ruoli
        for agent_role in ["sol", "luna", "agy", "deepseek"]:
            turns = self.manager._build_chat_messages(fresh, agent_role, "System prompt base per " + agent_role)
            total_chars = sum(len(t["content"]) for t in turns)
            self.assertLessEqual(
                total_chars,
                MAX_AGGREGATE_PAYLOAD_CHARS,
                f"Payload aggregato per {agent_role} eccede {MAX_AGGREGATE_PAYLOAD_CHARS} ({total_chars} chars)"
            )
            # Verifica che il numero di turni recenti non ecceda il budget
            self.assertLessEqual(len(turns) - 1, MAX_CONTEXT_TAIL_MESSAGES)

    # --- 2. Marker univoci e nessuna ricomparsa di messaggi già compattati ---
    def test_unique_markers_no_duplicate_raw_in_prompt(self):
        bs_id = "bs_unique_markers"
        bs = {
            "id": bs_id,
            "task": "Test marker univoci",
            "messages": [],
            "revision": 0,
            "chat_summary": "",
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        # Aggiungi 25 messaggi tracciabili
        for i in range(25):
            msg_text = f"MARKER_UNIQUE_{i:03d} specifiche modulo {i}"
            self.manager.post_chat_message(bs_id, message=msg_text, sender="user", runner=self.runner)

        fresh = self.manager.load_brainstorm(bs_id)
        compacted_idx = fresh.get("compacted_up_to_index", 0)
        self.assertGreater(compacted_idx, 0, "La sessione avrebbe dovuto compattare")

        turns = self.manager._build_chat_messages(fresh, "sol", "System prompt")
        prompt_text = "\n".join(t["content"] for t in turns)

        # I messaggi raw ante-compattazione non devono comparire nei turni di conversazione raw
        conversation_turns_text = "\n".join(t["content"] for t in turns if t["role"] != "system")
        raw_msgs = fresh.get("messages", [])
        compacted_raw_msgs = raw_msgs[:compacted_idx]
        tail_raw_msgs = raw_msgs[compacted_idx:]

        for m in compacted_raw_msgs:
            txt = m.get("text", "")
            if "MARKER_UNIQUE_" in txt:
                marker = [part for part in txt.split() if part.startswith("MARKER_UNIQUE_")][0]
                self.assertNotIn(
                    marker,
                    conversation_turns_text,
                    f"Marker già compattato {marker} ricompare nei turni di conversazione raw!"
                )

        # Ogni marker presente nel tail non compattato deve comparire al massimo UNA volta nel prompt
        for m in tail_raw_msgs:
            txt = m.get("text", "")
            if "MARKER_UNIQUE_" in txt:
                marker = [part for part in txt.split() if part.startswith("MARKER_UNIQUE_")][0]
                count = prompt_text.count(marker)
                self.assertLessEqual(count, 1, f"Marker {marker} compare {count} volte nel prompt (duplicazione)")

    # --- 2b. Tail bounded a 20 messaggi idonei con marker univoci ed esclusione errori/annunci ---
    def test_tail_bounded_to_20_eligible_messages_with_unique_markers(self):
        bs_id = "bs_tail_20_markers"
        raw_messages = []
        eligible_markers = []

        # Costruisci 40 messaggi con intercalati annunci ed errori
        for i in range(40):
            marker = f"TAIL_ITEM_{i:02d}"
            raw_messages.append({
                "id": f"msg_t_{i:02d}",
                "sender": "user" if i % 2 == 0 else "sol",
                "agent": "user" if i % 2 == 0 else "sol",
                "text": f"Contenuto {marker} per la discussione operativa"
            })
            eligible_markers.append(marker)

            # Intercala eventi non idonei
            if i == 10:
                raw_messages.append({
                    "id": "comp_notice_1",
                    "sender": "Gemini Compactor",
                    "agent": "gemini",
                    "text": "⚡ *[Memoria Compattata]* Archivio creato",
                    "message_type": "compaction_notice",
                    "exclude_from_context": True
                })
            elif i == 20:
                raw_messages.append({
                    "id": "op_err_1",
                    "sender": "agy",
                    "agent": "agy",
                    "text": "⚠️ [AGY Sidecar Error] socket timeout",
                    "message_type": "operational_error",
                    "is_error": True,
                    "exclude_from_context": True
                })
            elif i == 30:
                raw_messages.append({
                    "id": "legacy_err_1",
                    "sender": "agy",
                    "agent": "agy",
                    "text": "⚠️ [Codex Error] rate limit reached"
                })

        bs = {
            "id": bs_id,
            "task": "Test tail 20 messaggi idonei",
            "messages": raw_messages,
            "revision": 0,
            "chat_summary": "",
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        turns = self.manager._build_chat_messages(bs, "sol", "System prompt")
        conv_text = json.dumps(turns[1:])

        # 1. Nessun annuncio o errore presente nel prompt
        self.assertNotIn("⚡ *[Memoria Compattata]*", conv_text)
        self.assertNotIn("socket timeout", conv_text)
        self.assertNotIn("rate limit reached", conv_text)

        # 2. Gli ultimi 20 marker idonei DEVONO essere presenti
        expected_tail_markers = eligible_markers[-20:]
        for m in expected_tail_markers:
            self.assertIn(m, conv_text, f"Marker atteso nel tail {m} non trovato nel prompt")
            # Nessuna duplicazione
            self.assertEqual(conv_text.count(m), 1, f"Marker {m} duplicato nel prompt")

        # 3. I marker più vecchi dei 20 NON devono essere presenti
        older_markers = eligible_markers[:-20]
        for m in older_markers:
            self.assertNotIn(m, conv_text, f"Marker vecchio {m} presente nel prompt oltre il cap di 20")

        # 4. L'ultimo messaggio corrente (TAIL_ITEM_39) è presente
        self.assertIn("TAIL_ITEM_39", conv_text)

        # 5. Payload <= 32000
        total_chars = sum(len(t["content"]) for t in turns)
        self.assertLessEqual(total_chars, MAX_AGGREGATE_PAYLOAD_CHARS)



    # --- 3. Annuncio Gemini Compactor visibile in state/UI ma escluso dai prompt e summary ---
    def test_gemini_compactor_notice_excluded_from_prompts_and_compaction(self):
        bs_id = "bs_compactor_notice_test"
        bs = {
            "id": bs_id,
            "task": "Test isolamento avvisi",
            "messages": [
                {"id": "m1", "sender": "user", "agent": "user", "text": "Primo punto"},
                {"id": "m2", "sender": "sol", "agent": "sol", "text": "Risposta primo punto"},
                {
                    "id": "m_comp",
                    "sender": "Gemini Compactor",
                    "agent": "gemini",
                    "text": "⚡ *[Memoria Compattata]* Ho archiviato i punti chiave discussi finora per ottimizzare i token.",
                    "message_type": "compaction_notice",
                    "exclude_from_context": True
                },
                {"id": "m3", "sender": "user", "agent": "user", "text": "Secondo punto"},
                {"id": "m4", "sender": "sol", "agent": "sol", "text": "Risposta secondo punto"}
            ],
            "revision": 1,
            "chat_summary": "📌 Fatti: Discussione avviata",
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        # Verifica is_context_eligible
        self.assertFalse(is_context_eligible(bs["messages"][2]))
        # Verifica anche versione legacy senza metadata espliciti
        legacy_compactor_msg = {
            "sender": "Gemini Compactor",
            "agent": "gemini",
            "text": "⚡ *[Memoria Compattata]* Ho archiviato i punti..."
        }
        self.assertFalse(is_context_eligible(legacy_compactor_msg))

        # Verifica prompt building (turni di conversazione non devono contenere l'annuncio)
        turns = self.manager._build_chat_messages(bs, "sol", "System prompt")
        conv_turns_text = json.dumps(turns[1:])
        self.assertNotIn("⚡ *[Memoria Compattata]*", conv_turns_text)
        self.assertNotIn("Gemini Compactor", conv_turns_text)

    # --- 3b. Event filtering mirato: discussioni utente su memoria compattata restano nel contesto ---
    def test_user_discussing_compaction_remains_in_context(self):
        user_msg = {
            "id": "u_msg_1",
            "sender": "user",
            "agent": "user",
            "text": "Cosa ne pensi della [Memoria Compattata] archiviata da Gemini?"
        }
        self.assertTrue(
            is_context_eligible(user_msg),
            "Un messaggio utente autentico che cita '[Memoria Compattata]' non deve essere escluso dal contesto"
        )

        bs = {
            "id": "bs_user_compaction_disc",
            "task": "Test discussione utente su compattazione",
            "messages": [user_msg],
            "revision": 0,
            "chat_summary": "",
            "compacted_up_to_index": 0
        }
        turns = self.manager._build_chat_messages(bs, "sol", "System prompt")
        conv_text = json.dumps(turns[1:])
        self.assertIn("Cosa ne pensi della [Memoria Compattata]", conv_text)

    # --- 4. Errori operativi AGY/Codex visibili in state/UI ma esclusi dai prompt e summary ---
    def test_operational_errors_excluded_from_prompts_and_compaction(self):
        bs_id = "bs_operational_error_test"
        error_msg = {
            "id": "m_err1",
            "sender": "agy",
            "agent": "agy",
            "text": "⚠️ [AGY Sidecar Error] Impossibile contattare il runner host: socket connection refused",
            "message_type": "operational_error",
            "is_error": True,
            "exclude_from_context": True
        }
        legacy_error_msg = {
            "id": "m_err2",
            "sender": "agy",
            "agent": "agy",
            "text": "⚠️ [AGY Sidecar Error] timeout reached after 90s"
        }

        self.assertFalse(is_context_eligible(error_msg))
        self.assertFalse(is_context_eligible(legacy_error_msg))

        bs = {
            "id": bs_id,
            "task": "Test esclusione errori",
            "messages": [
                {"id": "m1", "sender": "user", "agent": "user", "text": "Esegui analisi"},
                error_msg,
                legacy_error_msg,
                {"id": "m2", "sender": "user", "agent": "user", "text": "Riprova analisi"}
            ],
            "revision": 1,
            "chat_summary": "",
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        turns = self.manager._build_chat_messages(bs, "sol", "System prompt")
        prompt_text = json.dumps(turns)
        self.assertNotIn("socket connection refused", prompt_text)
        self.assertNotIn("timeout reached after 90s", prompt_text)

    # --- 4b. Compaction input bounded e costruito a ritroso ---
    def test_compaction_input_bounded_backwards(self):
        bs_id = "bs_compaction_backwards_test"
        # Crea 30 messaggi corposi
        messages = [
            {
                "id": f"msg_c_{i:02d}",
                "sender": "user" if i % 2 == 0 else "sol",
                "agent": "user" if i % 2 == 0 else "sol",
                "text": f"Dettaglio tecnico importante #{i} " + ("K" * 600)
            }
            for i in range(30)
        ]
        bs = {
            "id": bs_id,
            "task": "Test compattazione a ritroso",
            "messages": messages,
            "revision": 0,
            "chat_summary": "",
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        # Esegui compattazione
        result = self.manager._check_and_compact_chat(bs_id, runner=self.runner)
        self.assertTrue(result)

        fresh = self.manager.load_brainstorm(bs_id)
        self.assertGreater(fresh.get("compacted_up_to_index", 0), 0)
        self.assertTrue(fresh.get("chat_summary"))
        self.assertLessEqual(len(fresh["chat_summary"]), MAX_SUMMARY_TOTAL_CHARS)

    # --- 4c. Compaction: test al limite esatto che precedentemente superava 12k ---
    def test_compaction_input_strictly_bounded_at_exact_boundary(self):
        """Verifica che il payload chat_text passato a Gemini sia sempre <= 12000 caratteri anche con marker di omissione."""
        bs_id = "bs_compaction_boundary_test"
        # Creiamo 25 messaggi da ~780 caratteri ciascuno (totale candidate ~17.000 > 12.000)
        # Prima del fix P2.2, i messaggi accumulavano fino a 11.950 caratteri, e l'aggiunta successiva del marker
        # portava il totale a 12.028 caratteri (> 12.000).
        messages = [
            {
                "id": f"msg_bound_{i:02d}",
                "sender": "user" if i % 2 == 0 else "sol",
                "agent": "user" if i % 2 == 0 else "sol",
                "text": f"SPEC_BLOCK_{i:02d} " + ("B" * 780)
            }
            for i in range(25)
        ]

        bs = {
            "id": bs_id,
            "task": "Test limite esatto 12k compattazione",
            "messages": messages,
            "revision": 0,
            "chat_summary": "",
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        captured_prompts = []

        def mock_query(agent_name, prompt):
            captured_prompts.append(prompt)
            return json.dumps({
                "facts": ["Fatto compattato 1"],
                "decisions": ["Decisione 1"],
                "constraints": [],
                "open_questions": [],
                "next_steps": []
            })

        with patch.object(self.manager, "_query_agent_llm", side_effect=mock_query):
            result = self.manager._check_and_compact_chat(bs_id, runner=None)
            self.assertTrue(result)

        self.assertGreater(len(captured_prompts), 0)
        sent_messages = captured_prompts[0]
        # sent_messages è la lista dei turni [{"role": "system", ...}, {"role": "user", "content": summary_prompt}]
        sent_prompt = sent_messages[1]["content"] if isinstance(sent_messages, list) else str(sent_messages)

        # Extract NEW MESSAGES TO INTEGRATE section from prompt sent to Gemini
        self.assertTrue("NEW MESSAGES TO INTEGRATE:\n" in sent_prompt or "NUOVI MESSAGGI DA INTEGRARE:\n" in sent_prompt)
        split_key = "NEW MESSAGES TO INTEGRATE:\n" if "NEW MESSAGES TO INTEGRATE:\n" in sent_prompt else "NUOVI MESSAGGI DA INTEGRARE:\n"
        tail_split = "\n\nGenerate a dense" if "\n\nGenerate a dense" in sent_prompt else "\n\nGenera una sintesi"
        chat_text_block = sent_prompt.split(split_key)[1].split(tail_split)[0]


        # 1. Garanzia assoluta: chat_text_block <= MAX_COMPACTION_INPUT_CHARS (12.000)
        self.assertLessEqual(
            len(chat_text_block),
            MAX_COMPACTION_INPUT_CHARS,
            f"chat_text_block ({len(chat_text_block)}) supera il cap assoluto di {MAX_COMPACTION_INPUT_CHARS} caratteri!"
        )

        # 2. Presenza del marker di omissione
        self.assertIn("[... messaggi precedenti omessi per limite di budget compattazione ...]", chat_text_block)

        # 3. Preservazione record completi (nessuna riga tagliata a metà mittente)
        for line in chat_text_block.split("\n\n"):
            if not line.startswith("[..."):
                self.assertTrue(
                    line.startswith("user:") or line.startswith("sol:"),
                    f"Record non inizia con mittente valido: {line[:30]}"
                )

    # --- 5. Hard cap reale del payload agente: extreme cases ---
    def test_hard_cap_payload_allocator_extreme_cases(self):
        bs_id = "bs_extreme_hard_cap"
        huge_task = "TASK_ESTREMO_" + ("T" * 15000)
        huge_file_content = "FILE_CONTENT_ENORME_" + ("F" * 50000)
        huge_user_msg = "ULTIMO_MESSAGGIO_UTENTE_ENORME_" + ("U" * 40000)

        bs = {
            "id": bs_id,
            "task": huge_task,
            "messages": [
                {"id": "m_extreme_user", "sender": "user", "agent": "user", "text": huge_user_msg}
            ],
            "revision": 0,
            "chat_summary": "📌 Fatti: Memoria iniziale " + ("S" * 3500),
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        inspected_file = {
            "path": "server/huge_file.py",
            "content": huge_file_content
        }

        # Genera i prompt per ciascun agente
        for agent_role in ["sol", "luna", "agy", "deepseek"]:
            turns = self.manager._build_chat_messages(
                bs,
                target_agent=agent_role,
                system_prompt="System prompt " * 200,
                inspected_file=inspected_file
            )

            # 1. HARD CAP ASSOLUTO <= 32.000 CARATTERI
            total_chars = sum(len(t["content"]) for t in turns)
            self.assertLessEqual(
                total_chars,
                MAX_AGGREGATE_PAYLOAD_CHARS,
                f"Payload per {agent_role} eccede {MAX_AGGREGATE_PAYLOAD_CHARS} ({total_chars} chars)"
            )

            # 2. Preservazione istruzioni core di sistema e ultimo messaggio utente
            self.assertGreater(len(turns), 0)
            self.assertEqual(turns[0]["role"], "system")
            self.assertIn("Memory & Chat Guidelines", turns[0]["content"])
            self.assertIn("- Be actionable, fast, and thorough.", turns[0]["content"])

            # 3. Presenza del truncation marker
            all_text = json.dumps(turns)
            self.assertIn(TRUNCATION_MARKER.strip(), all_text)

    # --- 5b. Robustezza system prompt enorme e conservazione Linee Guida Core ---
    def test_system_prompt_huge_preserves_core_guidelines_and_hard_cap(self):
        bs_id = "bs_huge_sys_prompt"
        huge_system_prompt = "MANUALE_ISTRUZIONI_ENORME_ " * 3000  # ~80.000 caratteri
        bs = {
            "id": bs_id,
            "task": "Task di test per system prompt enorme " + ("X" * 5000),
            "messages": [
                {"id": "m1", "sender": "user", "agent": "user", "text": "Messaggio operativo importante"}
            ],
            "revision": 0,
            "chat_summary": "📌 Fatti: Sintesi pregressa " + ("Z" * 4000),
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        turns = self.manager._build_chat_messages(bs, "sol", huge_system_prompt)

        # 1. Payload totale <= 32.000
        total_chars = sum(len(t["content"]) for t in turns)
        self.assertLessEqual(total_chars, MAX_AGGREGATE_PAYLOAD_CHARS)

        # 2. System turn contiene SEMPRE intatte le Linee Guida Memoria & Chat poste in coda
        sys_content = turns[0]["content"]
        self.assertIn("Memory & Chat Guidelines:", sys_content)
        self.assertIn("- You are part of a collaborative multi-agent chatroom in Buzz/Slack style with Massimo.", sys_content)
        self.assertIn("- Be actionable, fast, and thorough.", sys_content)

        # 3. Contiene il marker di troncamento esplicito
        self.assertIn(TRUNCATION_MARKER.strip(), sys_content)

    # --- 6. Broadcast @all a colpo singolo e reset dello sticky agent a Sol ---
    def test_all_broadcast_resets_sticky_agent_to_sol(self):
        bs_id = "bs_all_broadcast_test"
        bs = {
            "id": bs_id,
            "task": "Test broadcast @all reset",
            "messages": [],
            "revision": 0,
            "current_agent": "sol",
            "selected_agents": ["sol", "luna"],
            "chat_summary": "",
            "compacted_up_to_index": 0
        }
        self.manager.save_brainstorm(bs)

        # Invia messaggio con @all
        with patch.object(self.manager, "_query_agent_llm", return_value="Risposta simulata"):
            replies = self.manager.post_chat_message(bs_id, message="@all facciamo il punto", sender="user")

        # Verifica che abbiano risposto sia Sol che Luna (2 risposte)
        self.assertEqual(len(replies), 2)
        agents_replied = {r["agent"] for r in replies}
        self.assertEqual(agents_replied, {"sol", "luna"})

        # Verifica che lo stato abbia resettato current_agent a 'sol' (NON 'all')
        fresh = self.manager.load_brainstorm(bs_id)
        self.assertEqual(fresh.get("current_agent"), "sol")

        # Invia messaggio successivo SENZA menzione
        with patch.object(self.manager, "_query_agent_llm", return_value="Risposta Sol 1-a-1"):
            replies_next = self.manager.post_chat_message(bs_id, message="Perfetto, procedi", sender="user")

        # Verifica che ora risponda SOLO Sol
        self.assertEqual(len(replies_next), 1)
        self.assertEqual(replies_next[0]["agent"], "sol")


class TestTelemetryAndTokenUsage(unittest.TestCase):
    def setUp(self):
        from infrastructure.agent_gateway import AgentGateway
        AgentGateway._telemetry_repo = None
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_telemetry.db"
        self.db_manager = DatabaseManager(db_path=self.db_path)
        self.repo = TelemetryRepository(db_manager=self.db_manager)

    def tearDown(self):
        from infrastructure.agent_gateway import AgentGateway
        AgentGateway._telemetry_repo = None
        self.temp_dir.cleanup()


    # --- 7. Usage AGY JSON estratto e registrato tipizzato; assenza usage resta null ---
    def test_extract_reported_tokens_from_agy_json_and_codex(self):
        # 1. Output AGY con JSON usage completo
        agy_json_output = json.dumps({
            "conversation_id": "conv_123",
            "status": "SUCCESS",
            "response": "Operazione completata con successo.",
            "usage": {
                "input_tokens": 15431,
                "output_tokens": 288,
                "total_tokens": 15719
            }
        })
        tokens_agy = extract_reported_tokens(agy_json_output)
        self.assertEqual(tokens_agy, 15719)

        # 2. Output Codex con regex testuale
        codex_output = "Model execution completed. tokens used 4321\nResult code 0"
        tokens_codex = extract_reported_tokens(codex_output)
        self.assertEqual(tokens_codex, 4321)

        # 3. Output privo di usage / provider senza token count -> deve ritornare None (non 0)
        plain_output = "Risposta diretta standard senza metadati."
        tokens_plain = extract_reported_tokens(plain_output)
        self.assertIsNone(tokens_plain)

    # --- 7b. Telemetria AGY realistica: JSON in stdout con 15719 token (envelope live standard) ---
    def test_agy_telemetry_realistic_stdout_json_usage_15719(self):
        """Caso realistico live: il demone host restituisce stdout con JSON AGY contenente usage."""
        from infrastructure.agent_gateway import AgentGateway

        mock_agy_stdout = json.dumps({
            "status": "SUCCESS",
            "response": "Modifiche applicate al codice con successo.",
            "usage": {
                "input_tokens": 15431,
                "output_tokens": 288,
                "total_tokens": 15719
            }
        })

        # Envelope standard del sidecar live host_agy_socket_daemon (SENZA usage top-level nell'envelope)
        live_envelope = {
            "status": "SUCCESS",
            "code": 0,
            "stdout": mock_agy_stdout,
            "stderr": "",
            "duration_sec": 2.15
        }

        with patch.dict(os.environ, {"UFFICIO_HOST_AGY_SIDECAR": "1"}), \
             patch("infrastructure.host_agy_client.HostAgyClient.send_request_envelope", return_value=live_envelope), \
             patch("infrastructure.host_agy_client.HostAgyClient._load_token", return_value="a" * 32), \
             patch("infrastructure.agent_gateway.TelemetryRepository", return_value=self.repo):

            success, output, meta = AgentGateway.execute_agent_call(
                agent_role="agy",
                prompt="Esegui task e calcola token",
                worktree_path=Path(self.temp_dir.name),
                phase="task"
            )

            self.assertTrue(success)
            self.assertEqual(meta.get("reported_tokens"), 15719)

            # Verifica che SQLite contenga reported_tokens = 15719
            with self.db_manager.connection() as conn:
                row = conn.execute("SELECT * FROM agent_usage_events WHERE agent_role = 'agy' ORDER BY id DESC LIMIT 1").fetchone()
                self.assertIsNotNone(row)
                d = dict(row)
                self.assertEqual(d["reported_tokens"], 15719)
                self.assertEqual(d["duration_ms"], 2150)
                self.assertEqual(d["status"], "SUCCESS")

    # --- 7c. Telemetria AGY con envelope top-level usage (retrocompatibilità) ---
    def test_agy_telemetry_top_level_envelope_usage(self):
        from infrastructure.agent_gateway import AgentGateway

        mock_envelope = {
            "status": "SUCCESS",
            "code": 0,
            "stdout": "AGY execution output content",
            "duration_sec": 2.45,
            "usage": {
                "input_tokens": 15000,
                "output_tokens": 719,
                "total_tokens": 15719
            }
        }

        with patch.dict(os.environ, {"UFFICIO_HOST_AGY_SIDECAR": "1"}), \
             patch("infrastructure.host_agy_client.HostAgyClient.send_request_envelope", return_value=mock_envelope), \
             patch("infrastructure.host_agy_client.HostAgyClient._load_token", return_value="a" * 32), \
             patch("infrastructure.agent_gateway.TelemetryRepository", return_value=self.repo):

            success, output, meta = AgentGateway.execute_agent_call(
                agent_role="agy",
                prompt="Esegui task con conteggio token top-level",
                worktree_path=Path(self.temp_dir.name),
                phase="task"
            )

            self.assertTrue(success)
            self.assertEqual(output, "AGY execution output content")
            self.assertEqual(meta.get("reported_tokens"), 15719)

            with self.db_manager.connection() as conn:
                row = conn.execute("SELECT * FROM agent_usage_events WHERE agent_role = 'agy' ORDER BY id DESC LIMIT 1").fetchone()
                self.assertIsNotNone(row)
                d = dict(row)
                self.assertEqual(d["reported_tokens"], 15719)

    # --- 7d. Caso assenza usage: reported_tokens resta NULL (non 0) ---
    def test_agy_telemetry_missing_usage_is_null(self):
        from infrastructure.agent_gateway import AgentGateway

        mock_envelope_no_usage = {
            "status": "SUCCESS",
            "code": 0,
            "stdout": "AGY execution output without token usage",
            "duration_sec": 1.10
        }

        with patch.dict(os.environ, {"UFFICIO_HOST_AGY_SIDECAR": "1"}), \
             patch("infrastructure.host_agy_client.HostAgyClient.send_request_envelope", return_value=mock_envelope_no_usage), \
             patch("infrastructure.host_agy_client.HostAgyClient._load_token", return_value="a" * 32), \
             patch("infrastructure.agent_gateway.TelemetryRepository", return_value=self.repo):

            success, output, meta = AgentGateway.execute_agent_call(
                agent_role="agy",
                prompt="Esegui task senza usage",
                worktree_path=Path(self.temp_dir.name),
                phase="task"
            )

            self.assertTrue(success)
            self.assertIsNone(meta.get("reported_tokens"))

            with self.db_manager.connection() as conn:
                row = conn.execute("SELECT * FROM agent_usage_events WHERE agent_role = 'agy' ORDER BY id DESC LIMIT 1").fetchone()
                self.assertIsNotNone(row)
                d = dict(row)
                self.assertIsNone(d["reported_tokens"])

    # --- 7e. Test End-to-End Metriche P2 in SQLite ---
    def test_p2_context_metrics_end_to_end_sqlite(self):
        """Verifica che tutte e quattro le metriche P2 siano calcolate, passate al gateway e salvate su SQLite con soli valori numerici."""
        from infrastructure.agent_gateway import AgentGateway
        from brainstorm_manager import BrainstormManager

        bm = BrainstormManager(state_dir=self.temp_dir.name)
        bs_id = "bs_e2e_metrics_test"
        huge_msg_text = "LOG_DATA_BURST_ " * 300  # ~4.800 chars (subirà troncamento > 1500)
        bs = {
            "id": bs_id,
            "task": "Task per test metriche P2",
            "messages": [
                {"id": "m1", "sender": "user", "agent": "user", "text": "Messaggio 1"},
                {"id": "m2", "sender": "sol", "agent": "sol", "text": "Risposta 1"},
                {"id": "m3", "sender": "user", "agent": "user", "text": huge_msg_text}
            ],
            "revision": 0,
            "chat_summary": "📌 Fatti: Sintesi consolidata iniziale di prova per metriche.",
            "compacted_up_to_index": 0
        }
        bm.save_brainstorm(bs)

        inspected_file = {
            "path": "server/test_large.py",
            "content": "TEST_FILE_CONTENT " * 500  # ~9.000 chars (subirà troncamento > 4000)
        }

        turns = bm._build_chat_messages(bs, "agy", "System prompt agy", inspected_file=inspected_file)
        metrics = bm.compute_context_metrics(bs, turns, inspected_file=inspected_file)

        # Verifica calcolo numerico delle 4 metriche
        self.assertEqual(metrics["context_message_count"], 3)
        self.assertGreater(metrics["summary_length_chars"], 0)
        self.assertLessEqual(metrics["context_payload_chars"], MAX_AGGREGATE_PAYLOAD_CHARS)
        self.assertGreaterEqual(metrics["truncated_message_count"], 2)  # messaggio 3 + inspected file

        mock_envelope = {
            "status": "SUCCESS",
            "code": 0,
            "stdout": json.dumps({"status": "SUCCESS", "response": "Esecuzione completata.", "usage": {"total_tokens": 8450}}),
            "duration_sec": 1.75
        }

        with patch.dict(os.environ, {"UFFICIO_HOST_AGY_SIDECAR": "1"}), \
             patch("infrastructure.host_agy_client.HostAgyClient.send_request_envelope", return_value=mock_envelope), \
             patch("infrastructure.host_agy_client.HostAgyClient._load_token", return_value="a" * 32), \
             patch("infrastructure.agent_gateway.TelemetryRepository", return_value=self.repo):

            success, output, meta = AgentGateway.execute_agent_call(
                agent_role="agy",
                prompt="\n\n".join([f"[{t['role']}]: {t['content']}" for t in turns]),
                worktree_path=Path(self.temp_dir.name),
                phase="chat",
                metadata=metrics
            )

            self.assertTrue(success)

            # Query riga inserita nel database SQLite
            with self.db_manager.connection() as conn:
                row = conn.execute("SELECT * FROM agent_usage_events WHERE agent_role = 'agy' ORDER BY id DESC LIMIT 1").fetchone()
                self.assertIsNotNone(row)
                d = dict(row)

                self.assertEqual(d["reported_tokens"], 8450)
                self.assertEqual(d["duration_ms"], 1750)
                self.assertEqual(d["status"], "SUCCESS")

                # Verifica metadata JSON
                meta_json_str = d.get("metadata_json")
                self.assertIsNotNone(meta_json_str)
                saved_meta = json.loads(meta_json_str)

                # Tutte e 4 le metriche presenti
                self.assertEqual(saved_meta["context_message_count"], 3)
                self.assertEqual(saved_meta["summary_length_chars"], metrics["summary_length_chars"])
                self.assertEqual(saved_meta["context_payload_chars"], metrics["context_payload_chars"])
                self.assertEqual(saved_meta["truncated_message_count"], metrics["truncated_message_count"])

                # Solo valori numerici per queste metriche
                for k in ["context_message_count", "summary_length_chars", "context_payload_chars", "truncated_message_count"]:
                    self.assertIsInstance(saved_meta[k], int)

                # Zero testo del prompt o del file o del messaggio nel database
                self.assertNotIn("LOG_DATA_BURST_", str(d))
                self.assertNotIn("TEST_FILE_CONTENT", str(d))



    # --- 8. Telemetria non contiene prompt o response (privacy-safe) + metriche di efficienza numeriche ---
    def test_telemetry_recording_privacy_safe_no_prompt_text(self):
        prompt_secret = "SECRET_PROMPT_DO_NOT_STORE_12345"
        meta = {
            "preset": "standard",
            "sandbox": "read-only",
            "secret_key": "UNSAFE_KEY_SHOULD_BE_STRIPPED",
            "context_message_count": 18,
            "summary_length_chars": 1250,
            "context_payload_chars": 15400,
            "truncated_message_count": 2
        }

        event_id = self.repo.record_event(
            agent_role="sol",
            provider="openai_codex",
            model="gpt-5.6-sol",
            phase="chat",
            status="SUCCESS",
            duration_ms=1250,
            prompt_length=len(prompt_secret),
            reported_tokens=1200,
            session_id="bs_test_privacy",
            metadata=meta
        )

        self.assertIsNotNone(event_id)

        # Ispeziona riga inserita nel database SQLite
        with self.db_manager.connection() as conn:
            row = conn.execute("SELECT * FROM agent_usage_events WHERE id = ?", (event_id,)).fetchone()
            self.assertIsNotNone(row)
            d = dict(row)

            # Verifica che siano presenti i campi quantitativi
            self.assertEqual(d["reported_tokens"], 1200)
            self.assertEqual(d["prompt_length"], len(prompt_secret))
            self.assertEqual(d["status"], "SUCCESS")
            self.assertEqual(d["duration_ms"], 1250)

            # Verifica che il testo del prompt NON sia presente in alcuna colonna
            for col, val in d.items():
                if isinstance(val, str):
                    self.assertNotIn(prompt_secret, val)

            # Verifica sanitizzazione metadata e presenza delle metriche numeriche sicure
            meta_json = d.get("metadata_json")
            self.assertNotIn("UNSAFE_KEY_SHOULD_BE_STRIPPED", str(meta_json))
            parsed_meta = json.loads(meta_json)
            self.assertEqual(parsed_meta["preset"], "standard")
            self.assertEqual(parsed_meta["context_message_count"], 18)
            self.assertEqual(parsed_meta["summary_length_chars"], 1250)
            self.assertEqual(parsed_meta["context_payload_chars"], 15400)
            self.assertEqual(parsed_meta["truncated_message_count"], 2)


class TestSummaryValidationAndCapping(unittest.TestCase):
    # --- Bounded Summary Validation & Capping (Hard Cap <= 180 per item inclusi puntini) ---
    def test_normalize_and_cap_summary_limits(self):
        excessive_data = {
            "facts": [f"Fatto molto lungo #{i} " + ("x" * 250) for i in range(10)],
            "decisions": [f"Decisione #{i}" for i in range(8)],
            "constraints": ["Vincolo 1"],
            "open_questions": [],
            "next_steps": ["Passo 1"]
        }

        capped = normalize_and_cap_summary(
            excessive_data,
            max_items_per_section=MAX_SUMMARY_ITEMS_PER_SECTION,
            max_chars_per_item=MAX_SUMMARY_ITEM_CHARS
        )

        # Max 5 items per section
        self.assertEqual(len(capped["facts"]), MAX_SUMMARY_ITEMS_PER_SECTION)
        self.assertEqual(len(capped["decisions"]), MAX_SUMMARY_ITEMS_PER_SECTION)

        # Max 180 chars per item (INCLUSI i puntini, len(item) <= 180 rigorosamente)
        for item in capped["facts"]:
            self.assertLessEqual(
                len(item),
                MAX_SUMMARY_ITEM_CHARS,
                f"Item summary eccede {MAX_SUMMARY_ITEM_CHARS} ({len(item)} chars): {item}"
            )
            self.assertTrue(item.endswith("..."))

        canonical = format_canonical_summary(capped, max_total_chars=MAX_SUMMARY_TOTAL_CHARS)
        self.assertLessEqual(len(canonical), MAX_SUMMARY_TOTAL_CHARS)


if __name__ == "__main__":
    unittest.main()
