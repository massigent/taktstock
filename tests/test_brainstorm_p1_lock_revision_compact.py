#!/usr/bin/env python3
"""
P1 Tests – Persistenza affidabile della chat Buzz e compattazione fail-safe
---------------------------------------------------------------------------
Verifica:
1.  SessionFileLock: RLock in-process + fcntl.flock esclusivo su file 0600 per sessione.
2.  Revision incrementata solo al salvataggio riuscito; rollback su atomic_write_json fallito.
3.  CAS: compattazione scartata se la revision è cambiata durante la chiamata LLM.
4.  Gemini summary non valida (JSON malformato, rate limit, campo mancante) → compattazione
    annullata, compacted_up_to_index e messaggi restano invariati.
5.  Summary valida → compacted_up_to_index avanzato, messaggio Gemini Compactor appeso.
6.  get_chat_tail: coda multi-day corretta, bounded, con filtraggio since_date.
7.  Mapping canale: telegram, web, unknown con retrocompatibilità.
8.  post_chat_message: lock + save eseguiti correttamente per ogni round.
"""

import os
import subprocess
import sys
import json
import fcntl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from brainstorm_manager import (
    BrainstormManager,
    SessionFileLock,
    validate_structured_summary,
    format_canonical_summary,
    atomic_write_json,
    ALLOWED_CHANNELS,
    REQUIRED_SUMMARY_FIELDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bm(tmp: Path) -> BrainstormManager:
    return BrainstormManager(state_dir=tmp / "brainstorms")


def _add_messages(bs: dict, n: int, start_date: str = "2026-08-01") -> None:
    """Aggiunge n messaggi alla sessione con timestamp incrementali."""
    base = datetime.fromisoformat(start_date + "T10:00:00")
    for i in range(n):
        ts = (base + timedelta(hours=i)).isoformat()
        bs["messages"].append({
            "id": f"msg_{i}",
            "sender": "User",
            "agent": "user",
            "text": f"Messaggio numero {i} con dettagli tecnici sufficiente lunghezza.",
            "timestamp": ts,
        })


# ---------------------------------------------------------------------------
# 1. SessionFileLock
# ---------------------------------------------------------------------------


class _FakeRoundRunner:
    """Runner finto per run_round: LLM deterministici con hook di sincronizzazione."""

    def __init__(self, hold_event=None, barrier=None, hold_timeout=5):
        self.preset_config = {"reviewers": []}
        self.agent_policy = {"reviewers": {"automatic_selection_by_sol": False}}
        self.hold_event = hold_event
        self.barrier = barrier
        self.hold_timeout = hold_timeout
        self._analysis_done = False
        self.llm_started = threading.Event()

    def call_director(self, prompt, high_effort=False):
        if not self._analysis_done:
            self._analysis_done = True
            self.llm_started.set()
            if self.barrier is not None:
                try:
                    self.barrier.wait(timeout=self.hold_timeout)
                except Exception:
                    pass
            elif self.hold_event is not None:
                if not self.hold_event.wait(timeout=self.hold_timeout):
                    raise RuntimeError("hold_event non rilasciato (timeout)")
        return {
            "phase": "brainstorm",
            "analysis": "Analisi di test",
            "risks": ["Rischio test"],
            "strategy": "Strategia di test",
            "reviewers": [],
            "questions_for_user": ["Confermi?"],
        }

    def call_reviewer_deepseek_pro(self, prompt):
        return {"agent": "deepseek-pro", "opinion": "ok", "concerns": []}

    def call_reviewer_glm(self, prompt):
        return {"agent": "glm", "opinion": "ok", "alternatives": []}


class TestSessionFileLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp.name) / ".locks"
        self.lock_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lock_file_created_with_mode_0600(self):
        """Il file di lock deve essere creato con permessi 0600."""
        with SessionFileLock(self.lock_dir, "bs_test_0001"):
            lf = self.lock_dir / "bs_test_0001.lock"
            self.assertTrue(lf.exists())
            mode = oct(lf.stat().st_mode)[-4:]
            self.assertEqual(mode, "0600")

    def test_lock_is_exclusive_cross_thread_via_rlock(self):
        """Il RLock in-process impedisce a due thread di tenere il lock contemporaneamente."""
        lock1 = SessionFileLock(self.lock_dir, "bs_concurrent")
        lock2 = SessionFileLock(self.lock_dir, "bs_concurrent")
        results = []
        t1_holding = threading.Event()
        t2_can_start = threading.Event()

        def holder():
            with lock1:
                results.append("L1_in")
                t1_holding.set()        # segnala che L1 è dentro
                time.sleep(0.10)        # trattiene il lock
                results.append("L1_out")

        def waiter():
            t1_holding.wait(timeout=2)  # aspetta che L1 sia dentro
            with lock2:
                results.append("L2_in")

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start(); t2.start()
        t1.join(timeout=3); t2.join(timeout=3)

        # L2_in deve venire DOPO L1_out
        self.assertIn("L1_in", results)
        self.assertIn("L1_out", results)
        self.assertIn("L2_in", results)
        l1_out_idx = results.index("L1_out")
        l2_in_idx = results.index("L2_in")
        self.assertGreater(l2_in_idx, l1_out_idx,
                           f"L2_in ({l2_in_idx}) doveva venire dopo L1_out ({l1_out_idx}). Sequenza: {results}")

    def test_different_session_locks_are_independent(self):
        """Lock su sessioni diverse non si bloccano a vicenda."""
        lock_a = SessionFileLock(self.lock_dir, "bs_A")
        lock_b = SessionFileLock(self.lock_dir, "bs_B")
        acquired_both = False

        with lock_a:
            with lock_b:
                acquired_both = True

        self.assertTrue(acquired_both)

    def test_nested_two_instances_same_thread_no_deadlock(self):
        """Due istanze, stesso thread, lock annidato: termina senza deadlock."""
        lock1 = SessionFileLock(self.lock_dir, "bs_nested")
        lock2 = SessionFileLock(self.lock_dir, "bs_nested")
        with lock1:
            with lock2:
                pass
        with lock2:
            with lock1:
                pass

    def test_different_state_dirs_are_independent(self):
        """State_dir diversi con la stessa session_id non si serializzano."""
        other_dir = Path(self.tmp.name) / "other_locks"
        other_dir.mkdir(parents=True)
        lock_a = SessionFileLock(self.lock_dir, "bs_X")
        lock_b = SessionFileLock(other_dir, "bs_X")
        with lock_a:
            with lock_b:
                pass

    def test_different_state_dirs_no_serialization_across_threads(self):
        """Thread con lock su state_dir diversi non si bloccano a vicenda."""
        other_dir = Path(self.tmp.name) / "other_locks"
        other_dir.mkdir(parents=True)
        acquired = threading.Event()
        errors = []

        def worker():
            try:
                with SessionFileLock(other_dir, "bs_X"):
                    acquired.set()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        with SessionFileLock(self.lock_dir, "bs_X"):
            t.join(timeout=3)
        self.assertFalse(errors, f"Eccezioni nel worker: {errors}")
        self.assertTrue(acquired.is_set(), "Lock su state_dir diversi non devono serializzarsi")
        self.assertFalse(t.is_alive(), "Il worker non deve restare bloccato")

    def test_two_processes_real_flock_mutual_exclusion(self):
        """Due processi distinti: mutua esclusione reale via fcntl.flock."""
        session_id = "bs_multiproc"
        ready_path = Path(self.tmp.name) / "child_ready.txt"
        result_path = Path(self.tmp.name) / "child_result.json"

        child_code = f"""
import sys, time, json
sys.path.insert(0, {str(SERVER_DIR)!r})
from pathlib import Path
from brainstorm_manager import SessionFileLock

start = time.time()
with open({str(ready_path)!r}, "w", encoding="utf-8") as f:
    f.write("ready")
with SessionFileLock(Path({str(self.lock_dir)!r}), {session_id!r}):
    acquired_at = round(time.time() - start, 3)
with open({str(result_path)!r}, "w", encoding="utf-8") as f:
    json.dump({{"acquired_at": acquired_at}}, f)
"""

        with SessionFileLock(self.lock_dir, session_id):
            proc = subprocess.Popen([sys.executable, "-c", child_code])

            deadline = time.time() + 15
            while not ready_path.exists() and proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready_path.exists(), "Il figlio non ha raggiunto il lock (avvio/import falliti?)")
            self.assertIsNone(proc.poll(), "Il figlio non deve completare mentre il parent tiene il lock")
            self.assertFalse(result_path.exists(), "Il figlio non deve acquisire mentre il parent tiene il lock")
            time.sleep(0.3)
            self.assertFalse(result_path.exists(), "Il figlio ha acquisito durante la detenzione del lock parent")

        proc.wait(timeout=20)
        self.assertEqual(proc.returncode, 0, f"Il processo figlio è terminato con codice {proc.returncode}")
        data = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["acquired_at"], 0.25,
                                "Il figlio deve aver acquisito solo dopo il rilascio del parent")

    def test_rlock_allows_reentrant_acquisition_same_thread(self):
        """RLock in-process: due acquisizioni annidate reali (con due istanze) senza deadlock."""
        lock1 = SessionFileLock(self.lock_dir, "bs_reentrant")
        lock2 = SessionFileLock(self.lock_dir, "bs_reentrant")
        depth = [0]
        max_depth = [0]

        def reentrant_acquire():
            with lock1:
                depth[0] += 1
                max_depth[0] = max(max_depth[0], depth[0])
                with lock2:
                    depth[0] += 1
                    max_depth[0] = max(max_depth[0], depth[0])
                    depth[0] -= 1
                depth[0] -= 1

        t = threading.Thread(target=reentrant_acquire)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "Il thread di test è rimasto bloccato (deadlock)")
        self.assertEqual(depth[0], 0, "Depth non rientrato a zero dopo le acquisizioni annidate")
        self.assertEqual(max_depth[0], 2, "Le due acquisizioni annidate devono essere state raggiunte")


# ---------------------------------------------------------------------------
# 2. Revision / CAS
# ---------------------------------------------------------------------------

class TestRevisionAndCAS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_revision_increments_on_successful_save(self):
        """Ogni save_brainstorm riuscito incrementa revision di 1."""
        bs_id = self.bm.create_brainstorm("Test task", chat_id="tg_rev_test")
        bs = self.bm.load_brainstorm(bs_id)
        rev0 = bs["revision"]
        self.bm.save_brainstorm(bs)
        bs2 = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs2["revision"], rev0 + 1)

    def test_revision_not_incremented_on_failed_save(self):
        """Se atomic_write_json fallisce, revision nel dict viene ripristinata."""
        bs_id = self.bm.create_brainstorm("Test fail save")
        bs = self.bm.load_brainstorm(bs_id)
        rev_before = bs["revision"]

        with patch("brainstorm_manager.atomic_write_json", return_value=False):
            result = self.bm.save_brainstorm(bs)

        self.assertFalse(result)
        self.assertEqual(bs["revision"], rev_before, "La revision non deve avanzare su write fallito")

    def test_cas_discards_compaction_on_revision_change(self):
        """Se la revision cambia durante la chiamata LLM, la compattazione viene scartata."""
        bs_id = self.bm.create_brainstorm("CAS test task")
        bs = self.bm.load_brainstorm(bs_id)
        _add_messages(bs, 15)
        self.bm.save_brainstorm(bs)

        bs_fresh = self.bm.load_brainstorm(bs_id)
        snapshot_rev = bs_fresh["revision"]

        def gemini_call_that_modifies_session(agent, messages, **kwargs):
            # Simula una scrittura concorrente durante la chiamata Gemini
            concurrent = self.bm.load_brainstorm(bs_id)
            concurrent["messages"].append({"id": "concurrent_msg", "sender": "Sol", "agent": "sol",
                                            "text": "Messaggio concorrente.", "timestamp": datetime.now().isoformat()})
            self.bm.save_brainstorm(concurrent)
            return json.dumps({
                "facts": ["fatto 1"], "decisions": ["decisione 1"],
                "constraints": ["vincolo 1"], "open_questions": [], "next_steps": ["passo 1"]
            })

        with patch.object(self.bm, "_query_agent_llm", side_effect=gemini_call_that_modifies_session):
            result = self.bm._check_and_compact_chat(bs_id)

        self.assertFalse(result, "CAS deve scartare la compattazione se la revision è cambiata")
        bs_after = self.bm.load_brainstorm(bs_id)
        self.assertFalse(any(m.get("agent") == "gemini" for m in bs_after["messages"]),
                         "Nessun messaggio Gemini Compactor deve essere appeso")

    def test_revision_starts_at_1_after_create(self):
        """Dopo create_brainstorm la sessione è salvata con revision=1."""
        bs_id = self.bm.create_brainstorm("Rev init test")
        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs["revision"], 1)


# ---------------------------------------------------------------------------
# 3. Compattazione: validazione summary Gemini
# ---------------------------------------------------------------------------

class TestCompactionSummaryValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _create_session_with_n_messages(self, n: int = 15) -> str:
        bs_id = self.bm.create_brainstorm("Compact test task")
        bs = self.bm.load_brainstorm(bs_id)
        _add_messages(bs, n)
        self.bm.save_brainstorm(bs)
        return bs_id

    def test_invalid_json_summary_does_not_advance_index(self):
        """Se Gemini restituisce JSON malformato, compacted_up_to_index resta invariato."""
        bs_id = self._create_session_with_n_messages(15)
        bs_before = self.bm.load_brainstorm(bs_id)
        index_before = bs_before["compacted_up_to_index"]

        with patch.object(self.bm, "_query_agent_llm", return_value="questa non è json {{{"):
            result = self.bm._check_and_compact_chat(bs_id)

        self.assertFalse(result)
        bs_after = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs_after["compacted_up_to_index"], index_before)
        self.assertFalse(any(m.get("agent") == "gemini" for m in bs_after["messages"]))

    def test_rate_limit_error_in_summary_does_not_advance_index(self):
        """Se Gemini restituisce messaggio di rate limit, la compattazione è annullata."""
        bs_id = self._create_session_with_n_messages(15)
        bs_before = self.bm.load_brainstorm(bs_id)
        index_before = bs_before["compacted_up_to_index"]

        with patch.object(self.bm, "_query_agent_llm",
                          return_value="Error: rate_limit exceeded, retry after 60s"):
            result = self.bm._check_and_compact_chat(bs_id)

        self.assertFalse(result)
        bs_after = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs_after["compacted_up_to_index"], index_before)

    def test_missing_required_field_in_summary_does_not_advance_index(self):
        """Se manca un campo obbligatorio, compacted_up_to_index resta invariato."""
        bs_id = self._create_session_with_n_messages(15)
        bs_before = self.bm.load_brainstorm(bs_id)
        index_before = bs_before["compacted_up_to_index"]
        incomplete = {"facts": ["f1"], "decisions": ["d1"]}  # mancano constraints, open_questions, next_steps

        with patch.object(self.bm, "_query_agent_llm", return_value=json.dumps(incomplete)):
            result = self.bm._check_and_compact_chat(bs_id)

        self.assertFalse(result)
        bs_after = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs_after["compacted_up_to_index"], index_before)

    def test_valid_summary_advances_index_and_appends_gemini_message(self):
        """Una summary valida avanza compacted_up_to_index e appende il messaggio Gemini Compactor."""
        bs_id = self._create_session_with_n_messages(15)
        bs_before = self.bm.load_brainstorm(bs_id)
        index_before = bs_before["compacted_up_to_index"]
        n_msgs_before = len(bs_before["messages"])

        valid_summary = json.dumps({
            "facts": ["La sessione riguarda la persistenza P1."],
            "decisions": ["Implementare lock flock + revision/CAS."],
            "constraints": ["Repository-only, nessun deploy live."],
            "open_questions": ["Come gestire il multi-process su macOS?"],
            "next_steps": ["Scrivere test unitari per ogni componente."]
        })

        with patch.object(self.bm, "_query_agent_llm", return_value=valid_summary):
            result = self.bm._check_and_compact_chat(bs_id)

        self.assertTrue(result)
        bs_after = self.bm.load_brainstorm(bs_id)
        self.assertGreater(bs_after["compacted_up_to_index"], index_before)
        gemini_msgs = [m for m in bs_after["messages"] if m.get("agent") == "gemini"]
        self.assertEqual(len(gemini_msgs), 1)
        self.assertEqual(gemini_msgs[0]["sender"], "Gemini Compactor")
        self.assertIn("Memoria Compattata", gemini_msgs[0]["text"])

    def test_validate_structured_summary_accepts_valid_json(self):
        """validate_structured_summary accetta un JSON corretto con tutti i campi."""
        raw = json.dumps({
            "facts": ["fatto"], "decisions": ["decisione"],
            "constraints": ["vincolo"], "open_questions": ["domanda"], "next_steps": ["passo"]
        })
        valid, data, err = validate_structured_summary(raw)
        self.assertTrue(valid)
        self.assertIsNotNone(data)
        self.assertEqual(err, "")

    def test_validate_structured_summary_rejects_rate_limit(self):
        """validate_structured_summary rifiuta messaggi di rate limit."""
        valid, data, err = validate_structured_summary("Rate limit exceeded. Try again.")
        self.assertFalse(valid)
        self.assertIsNone(data)

    def test_validate_structured_summary_rejects_empty_fields(self):
        """Se tutti i campi hanno solo stringhe vuote, la summary è rifiutata."""
        raw = json.dumps({
            "facts": ["  ", ""], "decisions": [""],
            "constraints": [], "open_questions": [], "next_steps": []
        })
        valid, data, err = validate_structured_summary(raw)
        self.assertFalse(valid)

    def test_too_few_messages_skips_compaction(self):
        """Con meno di 10 messaggi non compattati, la compattazione non viene avviata."""
        bs_id = self.bm.create_brainstorm("Few msgs test")
        bs = self.bm.load_brainstorm(bs_id)
        _add_messages(bs, 5)
        self.bm.save_brainstorm(bs)

        with patch.object(self.bm, "_query_agent_llm") as mock_llm:
            result = self.bm._check_and_compact_chat(bs_id)
        self.assertFalse(result)
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# 4. get_chat_tail — multi-day e mapping canale
# ---------------------------------------------------------------------------

class TestGetChatTailMultiDay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_tail_returns_only_uncompacted_messages(self):
        """get_chat_tail ritorna solo i messaggi da compacted_up_to_index in poi."""
        bs_id = self.bm.create_brainstorm("Tail test")
        bs = self.bm.load_brainstorm(bs_id)
        _add_messages(bs, 20)
        bs["compacted_up_to_index"] = 10
        self.bm.save_brainstorm(bs)

        result = self.bm.get_chat_tail(bs_id)
        # I messaggi da indice 10 in poi (più il messaggio System iniziale)
        self.assertGreaterEqual(result["tail_count"], 10)
        self.assertEqual(result["compacted_up_to_index"], 10)

    def test_tail_bounded_by_max_msgs(self):
        """get_chat_tail limita la risposta a max_msgs messaggi."""
        bs_id = self.bm.create_brainstorm("Tail bounded test")
        bs = self.bm.load_brainstorm(bs_id)
        _add_messages(bs, 60)
        self.bm.save_brainstorm(bs)

        result = self.bm.get_chat_tail(bs_id, max_msgs=20)
        self.assertLessEqual(result["tail_count"], 20)

    def test_tail_since_date_filters_old_messages(self):
        """since_date esclude messaggi precedenti alla data specificata."""
        bs_id = self.bm.create_brainstorm("Tail since date test")
        bs = self.bm.load_brainstorm(bs_id)
        # Aggiungi 5 messaggi del giorno precedente + 5 del giorno corrente
        _add_messages(bs, 5, start_date="2026-07-01")
        _add_messages(bs, 5, start_date="2026-08-24")
        self.bm.save_brainstorm(bs)

        result = self.bm.get_chat_tail(bs_id, since_date="2026-08-24", max_msgs=100)
        for m in result["tail"]:
            ts = m.get("timestamp", "")
            if ts:
                self.assertGreaterEqual(ts[:10], "2026-08-24",
                                        f"Messaggio precedente alla data filtro trovato: {ts}")

    def test_tail_multiday_no_messages_dropped_by_date(self):
        """Senza since_date, messaggi di giorni diversi non vengono scartati."""
        bs_id = self.bm.create_brainstorm("Multi-day no filter")
        bs = self.bm.load_brainstorm(bs_id)
        _add_messages(bs, 5, start_date="2026-07-01")
        _add_messages(bs, 5, start_date="2026-08-24")
        self.bm.save_brainstorm(bs)

        result = self.bm.get_chat_tail(bs_id, max_msgs=100)
        # Tutti i 10 messaggi utente + il messaggio System iniziale = 11 totali
        self.assertEqual(result["total_messages"], 11)

    def test_tail_nonexistent_session_returns_empty(self):
        """Se la sessione non esiste, get_chat_tail ritorna dict vuoto."""
        result = self.bm.get_chat_tail("bs_nonexistent_00000000_000000")
        self.assertEqual(result, {})

    def test_tail_contains_revision_and_summary_flag(self):
        """Il risultato include revision e has_summary corretti."""
        bs_id = self.bm.create_brainstorm("Tail meta test")
        bs = self.bm.load_brainstorm(bs_id)
        bs["chat_summary"] = "Sintesi precedente disponibile."
        self.bm.save_brainstorm(bs)

        result = self.bm.get_chat_tail(bs_id)
        self.assertIn("revision", result)
        self.assertTrue(result["has_summary"])


# ---------------------------------------------------------------------------
# 5. Channel mapping: telegram / web / unknown
# ---------------------------------------------------------------------------

class TestChannelMapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_channel_telegram_when_chat_id_present(self):
        """create_brainstorm con chat_id imposta channel='telegram'."""
        bs_id = self.bm.create_brainstorm("TG test", chat_id="12345678")
        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs["channel"], "telegram")
        self.assertEqual(bs["chat_id"], "12345678")

    def test_channel_web_when_web_session_id_present(self):
        """create_brainstorm con web_session_id imposta channel='web'."""
        bs_id = self.bm.create_brainstorm("Web test", web_session_id="sess_abc123")
        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs["channel"], "web")
        self.assertEqual(bs["web_session_id"], "sess_abc123")

    def test_channel_unknown_when_no_id(self):
        """create_brainstorm senza chat_id né web_session_id imposta channel='unknown'."""
        bs_id = self.bm.create_brainstorm("Unknown channel test")
        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs["channel"], "unknown")

    def test_get_chat_tail_channel_from_session(self):
        """get_chat_tail espone il canale corretto della sessione; sessioni create nello stesso istante restano distinte."""
        bs_id_tg = self.bm.create_brainstorm("TG tail", chat_id="tg_789")
        bs_id_web = self.bm.create_brainstorm("Web tail", web_session_id="web_xyz")
        bs_id_unk = self.bm.create_brainstorm("Unknown tail")

        # Le tre sessioni, create a ridosso dello stesso istante, devono restare distinte
        self.assertEqual(len({bs_id_tg, bs_id_web, bs_id_unk}), 3, "Id sessione collidenti (stesso istante)")

        self.assertEqual(self.bm.get_chat_tail(bs_id_tg)["channel"], "telegram")
        self.assertEqual(self.bm.get_chat_tail(bs_id_web)["channel"], "web")
        self.assertEqual(self.bm.get_chat_tail(bs_id_unk)["channel"], "unknown")

    def test_channel_retrocompat_missing_field_defaults_to_unknown(self):
        """Sessioni legacy senza campo channel vengono lette come 'unknown'."""
        bs_id = self.bm.create_brainstorm("Legacy compat")
        path = self.bm.get_file_path(bs_id)
        raw = json.loads(path.read_text())
        del raw["channel"]
        path.write_text(json.dumps(raw))
        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs["channel"], "unknown")

    def test_active_brainstorm_mapping_telegram(self):
        """set_active_brainstorm con chat_id crea il file active_tg_<chat_id>.json."""
        bs_id = self.bm.create_brainstorm("TG mapping", chat_id="99999")
        tg_path = self.bm.get_active_tg_mapping_path("99999")
        self.assertIsNotNone(tg_path)
        self.assertTrue(tg_path.exists())
        data = json.loads(tg_path.read_text())
        self.assertEqual(data["active_id"], bs_id)

    def test_active_brainstorm_mapping_web(self):
        """set_active_brainstorm con web_session_id crea il file active_web_<id>.json."""
        bs_id = self.bm.create_brainstorm("Web mapping", web_session_id="sess_web_001")
        web_path = self.bm.get_active_web_mapping_path("sess_web_001")
        self.assertIsNotNone(web_path)
        self.assertTrue(web_path.exists())
        data = json.loads(web_path.read_text())
        self.assertEqual(data["active_id"], bs_id)

    def test_get_active_brainstorm_id_resolves_telegram(self):
        """get_active_brainstorm_id risolve correttamente la sessione attiva Telegram."""
        bs_id = self.bm.create_brainstorm("TG active resolve", chat_id="tg_resolve")
        resolved = self.bm.get_active_brainstorm_id(chat_id="tg_resolve")
        self.assertEqual(resolved, bs_id)

    def test_get_active_brainstorm_id_resolves_web(self):
        """get_active_brainstorm_id risolve correttamente la sessione attiva Web."""
        bs_id = self.bm.create_brainstorm("Web active resolve", web_session_id="web_resolve")
        resolved = self.bm.get_active_brainstorm_id(web_session_id="web_resolve")
        self.assertEqual(resolved, bs_id)


# ---------------------------------------------------------------------------
# 6. post_chat_message: lock + save atomici
# ---------------------------------------------------------------------------

class TestPostChatMessageLockAndSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_post_chat_saves_message_and_increments_revision(self):
        """post_chat_message salva il messaggio e incrementa revision."""
        bs_id = self.bm.create_brainstorm("Post chat test", chat_id="tg_post")
        bs_before = self.bm.load_brainstorm(bs_id)
        rev_before = bs_before["revision"]
        n_msgs_before = len(bs_before["messages"])

        with patch.object(self.bm, "_query_agent_llm", return_value="Risposta Sol di test."):
            replies = self.bm.post_chat_message(bs_id, "@sol analizza il progetto", sender="User")

        bs_after = self.bm.load_brainstorm(bs_id)
        self.assertGreater(bs_after["revision"], rev_before)
        self.assertGreater(len(bs_after["messages"]), n_msgs_before)
        self.assertTrue(len(replies) > 0)

    def test_concurrent_post_chat_messages_preserve_all_messages(self):
        """10 post_chat_message concorrenti: nessun messaggio perso, nessun duplicato, revision crescente."""
        bs_id = self.bm.create_brainstorm("Concurrent test")
        n = 10

        def sender(i):
            self.bm.post_chat_message(bs_id, f"@sol messaggio {i}", sender=f"User{i}")

        with patch.object(self.bm, "_query_agent_llm", return_value="Risposta di test"):
            threads = [threading.Thread(target=sender, args=(i,)) for i in range(n)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=20)

        self.assertFalse(any(th.is_alive() for th in threads), "Thread di post concorrenti ancora attivi (timeout)")

        bs_final = self.bm.load_brainstorm(bs_id)
        self.assertIsNotNone(bs_final)
        user_texts = [m["text"] for m in bs_final["messages"] if m.get("agent") == "user"]
        # Nota: il messaggio di sistema iniziale ha agent="sol" ma sender="System":
        # le risposte agente sono solo quelle con sender="Sol".
        agent_replies = [m for m in bs_final["messages"] if m.get("agent") == "sol" and m.get("sender") == "Sol"]
        for i in range(n):
            self.assertTrue(any(f"messaggio {i}" in t for t in user_texts), f"Messaggio utente {i} mancante")

        # Nessuna perdita: n messaggi utente e n risposte agente, oltre al messaggio di sistema iniziale
        self.assertEqual(len(user_texts), n, "Messaggi utente persi nella corsa concorrente")
        self.assertEqual(len(agent_replies), n, "Risposte agente perse nella corsa concorrente")

        # Nessun duplicato: id messaggio tutti distinti e conteggio esatto
        msg_ids = [m["id"] for m in bs_final["messages"]]
        self.assertEqual(len(msg_ids), len(set(msg_ids)), "Id messaggio duplicati dopo la corsa concorrente")
        self.assertEqual(len(msg_ids), 1 + 2 * n, f"Conteggio messaggi inatteso: {len(msg_ids)}")

        # Revision coerente: almeno una save per messaggio persistito
        self.assertGreaterEqual(bs_final["revision"], 1 + 2 * n, "Revision non cresciuta a sufficienza")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 9. ID univoci: sessioni e messaggi paralleli non collidono
# ---------------------------------------------------------------------------

class TestUniqueIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_sessions_and_messages_have_unique_ids(self):
        """Molte sessioni e messaggi creati in parallelo hanno tutti id distinti."""
        n = 20
        results = []

        def create_one(i):
            bs_id = self.bm.create_brainstorm(f"Task {i}", chat_id=f"tg_{i}")
            self.bm.post_chat_message(bs_id, f"@sol messaggio {i}", sender=f"User{i}")
            results.append(bs_id)

        with patch.object(self.bm, "_query_agent_llm", return_value="ok"):
            threads = [threading.Thread(target=create_one, args=(i,)) for i in range(n)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=30)

        self.assertFalse(any(th.is_alive() for th in threads), "Thread di creazione ancora attivi (timeout)")
        self.assertEqual(len(results), n, "Alcune sessioni non sono state create")
        self.assertEqual(len(set(results)), n, "Id sessione duplicati con creazione parallela")

        all_msg_ids = []
        for bs_id in results:
            bs = self.bm.load_brainstorm(bs_id)
            self.assertIsNotNone(bs)
            self.assertEqual(
                len(bs["messages"]), 3,
                f"Sessione {bs_id}: attesi 3 messaggi (init + utente + risposta), trovati {len(bs['messages'])}",
            )
            all_msg_ids.extend(m["id"] for m in bs["messages"])

        self.assertEqual(len(all_msg_ids), len(set(all_msg_ids)), "Id messaggio duplicati tra sessioni parallele")


# ---------------------------------------------------------------------------
# 10. Mutazioni concorrenti: run_round / approve / reject vs post_chat_message
# ---------------------------------------------------------------------------

class TestConcurrencyMutations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_run_round_and_post_chat_no_loss(self):
        """run_round e post_chat_message concorrenti: nessun messaggio perso, round unico, revision monotona."""
        bs_id = self.bm.create_brainstorm("RR vs chat", chat_id="rr_chat")
        hold = threading.Event()
        fake = _FakeRoundRunner(hold_event=hold)
        round_results = []

        def round_worker():
            try:
                round_results.append(self.bm.run_round(bs_id, user_feedback="feedback del round", runner=fake))
            except Exception as e:
                round_results.append(e)

        def chat_worker():
            with patch.object(self.bm, "_query_agent_llm", return_value="Risposta chat"):
                self.bm.post_chat_message(bs_id, "@sol messaggio durante il round", sender="UserChat")

        t_round = threading.Thread(target=round_worker)
        t_chat = threading.Thread(target=chat_worker)
        t_round.start()
        self.assertTrue(fake.llm_started.wait(timeout=5), "run_round non è entrato nella fase LLM (fuori lock)")
        t_chat.start()
        t_chat.join(timeout=15)
        self.assertFalse(t_chat.is_alive(), "post_chat_message bloccato mentre run_round è in fase LLM (deadlock?)")
        hold.set()
        t_round.join(timeout=15)
        self.assertFalse(t_round.is_alive(), "run_round ancora attivo (timeout)")

        self.assertFalse(round_results[0].get("conflict", False), "Il round non doveva andare in conflitto")
        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(len(bs["rounds"]), 1, "Deve esserci esattamente un round")
        texts = [m["text"] for m in bs["messages"]]
        self.assertTrue(any("feedback del round" in t for t in texts), "Feedback utente del round mancante")
        self.assertTrue(any("messaggio durante il round" in t for t in texts), "Messaggio chat concorrente mancante")
        self.assertTrue(any("Risposta chat" in t for t in texts), "Risposta chat concorrente mancante")
        self.assertTrue(any("Round 1 Summary" in t for t in texts), "Messaggio di sintesi del round mancante")

        msg_ids = [m["id"] for m in bs["messages"]]
        self.assertEqual(len(msg_ids), len(set(msg_ids)), "Id messaggio duplicati")
        self.assertGreaterEqual(bs["revision"], 5, "Revision non cresciuta in modo monotono")

    def test_concurrent_run_round_conflict_no_overwrite(self):
        """Due run_round concorrenti: solo uno committa, l'altro riceve un conflitto controllato."""
        bs_id = self.bm.create_brainstorm("RR conflict")
        barrier = threading.Barrier(2)
        fake1 = _FakeRoundRunner(barrier=barrier)
        fake2 = _FakeRoundRunner(barrier=barrier)
        results = []

        def worker(fake):
            try:
                results.append(self.bm.run_round(bs_id, runner=fake))
            except Exception as e:
                results.append(e)

        t1 = threading.Thread(target=worker, args=(fake1,))
        t2 = threading.Thread(target=worker, args=(fake2,))
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)
        self.assertFalse(t1.is_alive() or t2.is_alive(), "Thread run_round ancora attivi (deadlock?)")

        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(len(bs["rounds"]), 1, "Deve esserci esattamente un round")

        committed = [r for r in results if isinstance(r, dict) and not r.get("conflict")]
        conflicts = [r for r in results if isinstance(r, dict) and r.get("conflict")]
        self.assertEqual(len(committed), 1, "Esattamente un run_round deve committare il round")
        self.assertEqual(len(conflicts), 1, "L'altro run_round deve ricevere un conflitto controllato")
        self.assertEqual(conflicts[0]["expected_round"], 1)
        self.assertEqual(conflicts[0]["actual_rounds"], 1)

    def test_approve_vs_concurrent_post_chat_no_loss(self):
        """approve concorrente con post_chat_message: nessuna perdita, stato coerente, revision monotona."""
        bs_id = self.bm.create_brainstorm("Approve vs chat", chat_id="appr_chat")
        n = 8

        def post(i):
            self.bm.post_chat_message(bs_id, f"@sol messaggio {i}", sender=f"User{i}")

        def approver():
            time.sleep(0.05)
            self.bm.approve(bs_id, approved_by="admin")

        with patch.object(self.bm, "_query_agent_llm", return_value="Risposta di test"):
            threads = [threading.Thread(target=post, args=(i,)) for i in range(n)]
            threads.append(threading.Thread(target=approver))
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=20)

        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs["status"], "approved")
        self.assertEqual(bs["approved_by"], "admin")

        user_texts = [m["text"] for m in bs["messages"] if m.get("agent") == "user"]
        for i in range(n):
            self.assertTrue(any(f"messaggio {i}" in t for t in user_texts), f"Messaggio utente {i} mancante dopo approve")
        self.assertEqual(len(user_texts), n, "Messaggi utente persi dopo approve concorrente")

        msg_ids = [m["id"] for m in bs["messages"]]
        self.assertEqual(len(msg_ids), len(set(msg_ids)), "Id messaggio duplicati")
        self.assertEqual(len(msg_ids), 1 + 2 * n + 1, f"Conteggio messaggi inatteso: {len(msg_ids)}")
        self.assertGreaterEqual(bs["revision"], 1 + 3 * n, "Revision non cresciuta in modo monotono")

    def test_reject_vs_concurrent_post_chat_no_loss(self):
        """reject concorrente con post_chat_message: nessuna perdita, stato coerente, revision monotona."""
        bs_id = self.bm.create_brainstorm("Reject vs chat", chat_id="rej_chat")
        n = 8

        def post(i):
            self.bm.post_chat_message(bs_id, f"@sol messaggio {i}", sender=f"User{i}")

        def rejector():
            time.sleep(0.05)
            self.bm.reject(bs_id, reason="Riprogettare")

        with patch.object(self.bm, "_query_agent_llm", return_value="Risposta di test"):
            threads = [threading.Thread(target=post, args=(i,)) for i in range(n)]
            threads.append(threading.Thread(target=rejector))
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=20)

        bs = self.bm.load_brainstorm(bs_id)
        self.assertEqual(bs["status"], "rejected")
        self.assertEqual(bs["rejected_reason"], "Riprogettare")

        user_texts = [m["text"] for m in bs["messages"] if m.get("agent") == "user"]
        for i in range(n):
            self.assertTrue(any(f"messaggio {i}" in t for t in user_texts), f"Messaggio utente {i} mancante dopo reject")
        self.assertEqual(len(user_texts), n, "Messaggi utente persi dopo reject concorrente")

        msg_ids = [m["id"] for m in bs["messages"]]
        self.assertEqual(len(msg_ids), len(set(msg_ids)), "Id messaggio duplicati")
        self.assertEqual(len(msg_ids), 1 + 2 * n + 1, f"Conteggio messaggi inatteso: {len(msg_ids)}")
        self.assertGreaterEqual(bs["revision"], 1 + 3 * n, "Revision non cresciuta in modo monotono")



# ---------------------------------------------------------------------------
# P1D – Permessi 0600 atomici (sessione e mapping)
# ---------------------------------------------------------------------------

class TestAtomicWriteJsonMode0600(unittest.TestCase):
    """P1D: atomic_write_json deve creare/sovrascrivere file JSON con mode 0600,
    indipendentemente dalla umask, senza seguire symlink."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_session_file_created_with_mode_0600(self):
        """Un file di sessione nuovo viene creato con permessi 0600."""
        target = self.tmp_path / "bs_test_0001.json"
        ok = atomic_write_json(target, {"id": "bs_test_0001", "messages": []})
        self.assertTrue(ok)
        self.assertTrue(target.exists())
        mode = oct(target.stat().st_mode)[-4:]
        self.assertEqual(mode, "0600", f"Permessi attesi 0600, trovati {mode}")

    def test_overwrite_existing_file_sets_mode_0600(self):
        """Sovrascrivere un file con permessi più aperti li restringe a 0600."""
        target = self.tmp_path / "bs_overwrite.json"
        target.write_text("{}")
        os.chmod(str(target), 0o644)
        ok = atomic_write_json(target, {"id": "bs_overwrite", "messages": []})
        self.assertTrue(ok)
        mode = oct(target.stat().st_mode)[-4:]
        self.assertEqual(mode, "0600", f"Permessi dopo sovrascrittura: {mode}, attesi 0600")

    def test_mode_0600_unaffected_by_umask(self):
        """La umask non deve alterare i permessi del file scritto."""
        target = self.tmp_path / "bs_umask_test.json"
        old_umask = os.umask(0o022)
        try:
            ok = atomic_write_json(target, {"id": "bs_umask_test"})
        finally:
            os.umask(old_umask)
        self.assertTrue(ok)
        mode = oct(target.stat().st_mode)[-4:]
        self.assertEqual(mode, "0600", f"Umask ha influenzato i permessi: {mode}")

    def test_no_tmp_file_left_on_success(self):
        """Nessun file temporaneo .tmp_* deve rimanere dopo una scrittura riuscita."""
        target = self.tmp_path / "bs_clean.json"
        atomic_write_json(target, {"id": "bs_clean"})
        tmp_files = list(self.tmp_path.glob(".tmp_*"))
        self.assertEqual(tmp_files, [], f"File temporanei residui: {tmp_files}")


class TestBrainstormManagerFiles0600(unittest.TestCase):
    """P1D: BrainstormManager deve creare file sessione e mapping con mode 0600."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bm = _make_bm(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_json_created_with_mode_0600(self):
        """create_brainstorm crea il file sessione con permessi 0600."""
        bs_id = self.bm.create_brainstorm("P1D session test")
        path = self.bm.get_file_path(bs_id)
        self.assertTrue(path.exists())
        mode = oct(path.stat().st_mode)[-4:]
        self.assertEqual(mode, "0600", f"Sessione {bs_id}: permessi {mode}, attesi 0600")

    def test_active_tg_mapping_created_with_mode_0600(self):
        """create_brainstorm con chat_id crea active_tg_*.json con mode 0600."""
        bs_id = self.bm.create_brainstorm("P1D tg mapping", chat_id="tg_p1d_test")
        map_path = self.bm.get_active_tg_mapping_path("tg_p1d_test")
        self.assertIsNotNone(map_path)
        self.assertTrue(map_path.exists())
        mode = oct(map_path.stat().st_mode)[-4:]
        self.assertEqual(mode, "0600", f"Mapping TG: permessi {mode}, attesi 0600")

    def test_active_web_mapping_created_with_mode_0600(self):
        """create_brainstorm con web_session_id crea active_web_*.json con mode 0600."""
        bs_id = self.bm.create_brainstorm("P1D web mapping", web_session_id="web_p1d_test")
        map_path = self.bm.get_active_web_mapping_path("web_p1d_test")
        self.assertIsNotNone(map_path)
        self.assertTrue(map_path.exists())
        mode = oct(map_path.stat().st_mode)[-4:]
        self.assertEqual(mode, "0600", f"Mapping Web: permessi {mode}, attesi 0600")

    def test_save_brainstorm_preserves_mode_0600(self):
        """save_brainstorm mantiene i permessi 0600 ad ogni riscrittura."""
        bs_id = self.bm.create_brainstorm("P1D save mode test")
        bs = self.bm.load_brainstorm(bs_id)
        bs["messages"].append({"id": "m1", "sender": "User", "agent": "user",
                                "text": "test", "timestamp": datetime.now().isoformat()})
        self.bm.save_brainstorm(bs)
        path = self.bm.get_file_path(bs_id)
        mode = oct(path.stat().st_mode)[-4:]
        self.assertEqual(mode, "0600", f"Dopo save: permessi {mode}, attesi 0600")


if __name__ == "__main__":
    unittest.main()
