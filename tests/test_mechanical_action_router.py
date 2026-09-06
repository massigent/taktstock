#!/usr/bin/env python3
"""
Unit tests for explicit mechanical action routing, fail-closed validation, scoped allowlists,
and targeted list renumbering with strict idempotency on docs/HANDOFF.md.
Zero LLM calls, zero sidecars, pure local testing.
"""

import os
import sys
import json
import unittest
import tempfile
from pathlib import Path

# Add server directory to sys.path
server_dir = Path(__file__).resolve().parent.parent / "server"
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

from orchestrator_core import (
    MultiAgentRunner,
    SUPPORTED_MECHANICAL_ACTIONS,
    MECHANICAL_ACTION_ALLOWLIST,
    MECHANICAL_ALLOWED_WRITE_FILES,
    update_handoff_content_deterministic,
)


REAL_HANDOFF_SAMPLE = """# Stato più recente — WI-RITUALI-2 pre-apply (2026-08-21)

- **Fase:** DB Task 1/2 APPLICATO; n8n Task 3 revisionato GO ma NON applicato; Task 4 smoke definito ma NON eseguito.
- **DB live:** firma 3-arg `movimento_avvia_rituale(bigint,integer,integer)` presente con ACL `{aistack=X/aistack}`; firma 2-arg ancora live; dashboard espone `rituale_in_corso.creato_il`; zero `in_corso` per `511090810`.
- **Sub live:** `8PYy7lbyfjuwwK8u` attivo, `availableInMCP=false`, 41 nodi; nodeId `24b0aef9-94c5-4c52-a1e5-ee0bd6362854` ancora con query 2-arg.

**Prossimo agente, in ordine:**
1. Chiedere approvazione utente per Task 3, poi `validateOnly` e apply atomico del solo nodo `PG — Avvia Rituale`.
2. Eseguire Task 4 smoke minimo via `n8n_test_workflow` su Master `oyJX2al4JMmNz7lk`.
3. A smoke verde: riesportare `MiniApp__Allenamento.json`, validare e committare mirror + `WORKFLOW_LOG.md`.
4. Dopo il backend: frontend Task 6-9; deploy Cloudflare Task 10 SOLO con approvazione esplicita.
5. Lo snapshot `backups/live_pre_wi_rituali_2/8PYy7lbyfjuwwK8u_full.json` è già tracciato dal commit `7348582`.

---

# Stato precedente — Movimento runtime V1 (2026-08-20)

- **Stato:** runtime sedute Movimento live, frontend deployato e smoke UI reale concluso.
- **Regole:** runtime non deve chiamare log_seduta né salva_feedback_seduta; non usare localStorage per esiti runtime; non inventare durata/timestamp.
- **Prossimi passi aperti, in quest’ordine:**
  1. eseguire e registrare il controllo SQL finale: conteggi legacy devono restare allenamento_log=3 e allenamento_esercizio_feedback=10; allenamento_esecuzione_set deve restare 0;
  2. sincronizzare dal live il mirror MiniApp_Master_API.json per la modifica diretta EW → Respond e registrarla in WORKFLOW_LOG.md, senza sovrascrivere modifiche utente;
  3. solo dopo, valutare un work item separato per i rituali runtime: le funzioni DB esistono, ma le action n8n/frontend rituali non sono ancora collegate;
  4. non eliminare, spostare, ripristinare o includere file nuovi/modificati dell’utente.

---

## W2-A — Split MiniApp Shop
- **Scope estratto:** shop_activate, shop_deactivate.
- **Rollback e mirror:** baseline n8n pre-apply `3d2c5d0e-d4b3-4922-a766-e68af3d9109d`. Mirror sincronizzato nel commit `a44bb0f` (`MiniApp_Master_API.json`).

## Commit di oggi
| Commit | Contenuto |
|---|---|
| `5fdf64d` | WI-1 Script di validazione workflow e test harness |
| `79986ef` | Fix validatore (allowlist Logger Token, check target `mode: each`, SHA-256 letterale) |

## Avvisi critici per i prossimi agenti
1. **Backup restore**: un processo esterno sovrascrive periodicamente i file.
2. `MiniApp Master API_BACKUP.json` NON va eliminato.
3. Riesportazioni n8n = solo metadata o ID nodi nuovi: non è una regressione.
4. Validare ogni JSON toccato con `jq -e .`.
5. Snapshot recovery permanente.

## Prossimi passi consigliati (refactoring incrementale)
Prima di qualunque Gate frontend ulteriore: eseguire uno smoke test reale nella Telegram Mini App.

Piano completo con vincoli, criteri di accettazione e checklist:
1. **WI-0** Mappa del router `azione → route → sub → contratto` — **completata**.
2. **WI-1** Script di validazione automatica dei JSON — **completato**.
3. **WI-2** Deduplicazione orizzontale: logger unico.
4. **WI-3** Split dei domini core rimasti nel main.
5. **WI-4** Logica di dominio fuori da n8n verso `engine/`.
"""


class TestMechanicalActionRouter(unittest.TestCase):
    """Test routing esplicito delle azioni meccaniche e isolamento allowlist."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp_dir.name)
        (self.workspace / ".git").mkdir()
        (self.workspace / "docs").mkdir()
        self.handoff_file = self.workspace / "docs" / "HANDOFF.md"
        self.handoff_file.write_text(REAL_HANDOFF_SAMPLE, encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_reproduce_erroneous_run_not_routed_to_n8n_sync(self):
        """
        Riproduzione esatta della run errata 1787688358:
        Un task che parla di HANDOFF e mirror sync SENZA azione esplicita
        NON viene instradato a n8n sync o subtask meccanici automatici da regex generiche.
        """
        task_text = "Aggiorna docs/HANDOFF.md registrando il sync del mirror MiniApp Master API"
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="standard",
            mock_mode=True
        )
        self.assertFalse(runner.is_mechanical_task(task_text))

    def test_missing_action_in_mechanical_preset_fails_closed(self):
        """Se viene richiesto preset:mechanical ma nessuna azione è specificata, fallisce subito fail-closed."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="mechanical",
            mock_mode=True
        )
        task_text = "Aggiorna documentazione preset:mechanical"
        res = runner.run_full_workflow(task_text, branch_name="feature/test-fail-closed")

        self.assertEqual(res["status"], "BLOCKED_PREREQUISITE")
        self.assertIn("BLOCKED_UNSUPPORTED_MECHANICAL_ACTION", res["blocker_reason"])
        self.assertEqual(res["tokens_used"]["calls_count"]["total"], 0)

        # Zero file scritti
        self.assertFalse((self.workspace / "MiniApp_Master_API.json").exists())
        self.assertFalse((self.workspace / "WORKFLOW_LOG.md").exists())

    def test_unknown_action_fails_closed(self):
        """Un'azione sconosciuta (es: --action unknown-action) fallisce immediatamente prima di qualunque scrittura."""
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="mechanical",
            mock_mode=True
        )
        task_text = "Esegui sync --action unknown-action"
        res = runner.run_full_workflow(task_text, branch_name="feature/test-unknown-action")

        self.assertEqual(res["status"], "BLOCKED_PREREQUISITE")
        self.assertIn("BLOCKED_UNSUPPORTED_MECHANICAL_ACTION", res["blocker_reason"])
        self.assertIn("unknown-action", res["blocker_reason"])
        self.assertEqual(res["tokens_used"]["calls_count"]["total"], 0)

        # Zero scritture
        self.assertFalse((self.workspace / "MiniApp_Master_API.json").exists())
        self.assertFalse((self.workspace / "WORKFLOW_LOG.md").exists())

    def test_complete_handoff_mirror_sync_deterministic_execution(self):
        """
        L'azione `complete-handoff-mirror-sync`:
        - modifica solo docs/HANDOFF.md;
        - registra il sync MiniApp Master API del 2026-08-25 (commit 7e55f78) come completato;
        - non accede a n8n, non crea WORKFLOW_LOG.md o MiniApp_Master_API.json;
        - completa con 0 LLM e 0 token.
        """
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="mechanical",
            mock_mode=True
        )
        task_text = "Completa handoff mirror sync --action complete-handoff-mirror-sync"
        res = runner.run_full_workflow(task_text, branch_name="feature/test-handoff-sync")

        self.assertIn(res["status"], ["COMPLETED", "WAITING_FOR_APPROVAL"])
        self.assertEqual(res["tokens_used"]["calls_count"]["total"], 0)

        # Non deve aver creato MiniApp_Master_API.json o WORKFLOW_LOG.md
        self.assertFalse((self.workspace / "MiniApp_Master_API.json").exists())
        self.assertFalse((self.workspace / "WORKFLOW_LOG.md").exists())

        # Verifica aggiornamento di docs/HANDOFF.md
        content = self.handoff_file.read_text(encoding="utf-8")
        self.assertIn("`7e55f78`", content)
        self.assertIn("Sync mirror MiniApp Master API (`oyJX2al4JMmNz7lk`)", content)
        self.assertIn("2026-08-25", content)

        # Subtask generati devono essere 3 (preflight, handoff_update, validation) e nessun n8n export
        subtask_agents = [st.get("agent") or st.get("a") for st in res.get("subtasks", [])]
        self.assertTrue(all(a == "local_mechanical" for a in subtask_agents))
        subtask_descs = [st.get("description") or st.get("d", "") for st in res.get("subtasks", [])]
        self.assertTrue(any("HANDOFF" in d for d in subtask_descs))
        self.assertFalse(any("live n8n" in d.lower() for d in subtask_descs))

    def test_sync_n8n_mirror_execution_modifies_only_allowed_files(self):
        """
        L'azione `sync-n8n-mirror`:
        - modifica solo MiniApp_Master_API.json e WORKFLOW_LOG.md;
        - non tocca docs/HANDOFF.md.
        """
        initial_handoff = self.handoff_file.read_text(encoding="utf-8")
        runner = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="mechanical",
            mock_mode=True
        )
        task_text = "Sincronizza mirror live --action sync-n8n-mirror"
        res = runner.run_full_workflow(task_text, branch_name="feature/test-n8n-sync")

        self.assertIn(res["status"], ["COMPLETED", "WAITING_FOR_APPROVAL"])
        self.assertTrue((self.workspace / "MiniApp_Master_API.json").exists())
        self.assertTrue((self.workspace / "WORKFLOW_LOG.md").exists())

        # docs/HANDOFF.md deve essere rimasto inalterato
        self.assertEqual(self.handoff_file.read_text(encoding="utf-8"), initial_handoff)

    def test_action_allowlist_isolation(self):
        """Verifica che ogni azione possa scrivere solo i file della propria allowlist."""
        # 1. complete-handoff-mirror-sync autorizza solo docs/HANDOFF.md e HANDOFF.md
        runner_handoff = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="mechanical",
            mock_mode=True,
            mechanical_action="complete-handoff-mirror-sync"
        )
        runner_handoff.assert_allowlisted_write("docs/HANDOFF.md", action="complete-handoff-mirror-sync")
        runner_handoff.assert_allowlisted_write("HANDOFF.md", action="complete-handoff-mirror-sync")

        with self.assertRaises(PermissionError):
            runner_handoff.assert_allowlisted_write("MiniApp_Master_API.json", action="complete-handoff-mirror-sync")

        with self.assertRaises(PermissionError):
            runner_handoff.assert_allowlisted_write("WORKFLOW_LOG.md", action="complete-handoff-mirror-sync")

        # 2. sync-n8n-mirror autorizza solo MiniApp_Master_API.json e WORKFLOW_LOG.md
        runner_n8n = MultiAgentRunner(
            workspace_path=self.workspace,
            preset="mechanical",
            mock_mode=True,
            mechanical_action="sync-n8n-mirror"
        )
        runner_n8n.assert_allowlisted_write("MiniApp_Master_API.json", action="sync-n8n-mirror")
        runner_n8n.assert_allowlisted_write("WORKFLOW_LOG.md", action="sync-n8n-mirror")

        with self.assertRaises(PermissionError):
            runner_n8n.assert_allowlisted_write("docs/HANDOFF.md", action="sync-n8n-mirror")

        with self.assertRaises(PermissionError):
            runner_n8n.assert_allowlisted_write("HANDOFF.md", action="sync-n8n-mirror")


class TestRealHandoffStructureRenumberingAndIdempotency(unittest.TestCase):
    """Test della struttura reale di docs/HANDOFF.md: rimozione, rinumerazione consecutiva e idempotenza."""

    def test_removal_and_consecutive_renumbering_only_in_target_list(self):
        """
        Verifica che:
        - Il punto 2 sul mirror sync venga rimosso dall'elenco 'Prossimi passi aperti';
        - I punti residui di 'Prossimi passi aperti' siano rinumerati consecutivamente come 1, 2, 3;
        - Nessun altro elenco numerato del documento venga alterato;
        - I paragrafi storici contenenti 'mirror' rimangano intatti.
        """
        updated = update_handoff_content_deterministic(REAL_HANDOFF_SAMPLE)

        # 1. Verifica elenco target 'Prossimi passi aperti'
        self.assertIn("1. eseguire e registrare il controllo SQL finale", updated)
        self.assertNotIn("sincronizzare dal live il mirror MiniApp_Master_API.json", updated)
        self.assertIn("2. solo dopo, valutare un work item separato per i rituali runtime", updated)
        self.assertIn("3. non eliminare, spostare, ripristinare o includere file nuovi/modificati dell’utente.", updated)
        self.assertNotIn("4. non eliminare, spostare", updated)

        # 2. Verifica che 'Prossimo agente, in ordine:' conservi i punti 1, 2, 3, 4, 5 inalterati
        self.assertIn("1. Chiedere approvazione utente per Task 3", updated)
        self.assertIn("2. Eseguire Task 4 smoke minimo via `n8n_test_workflow`", updated)
        self.assertIn("3. A smoke verde: riesportare `MiniApp__Allenamento.json`", updated)
        self.assertIn("4. Dopo il backend: frontend Task 6-9", updated)
        self.assertIn("5. Lo snapshot `backups/live_pre_wi_rituali_2/8PYy7lbyfjuwwK8u_full.json`", updated)

        # 3. Verifica che 'Avvisi critici per i prossimi agenti' conservi 1..5 inalterati
        self.assertIn("1. **Backup restore**", updated)
        self.assertIn("2. `MiniApp Master API_BACKUP.json` NON va eliminato.", updated)
        self.assertIn("3. Riesportazioni n8n = solo metadata", updated)
        self.assertIn("4. Validare ogni JSON toccato con `jq -e .`.", updated)
        self.assertIn("5. Snapshot recovery permanente.", updated)

        # 4. Verifica che 'Piano completo... (WI-0..WI-4)' conservi 1..5 inalterati
        self.assertIn("1. **WI-0** Mappa del router", updated)
        self.assertIn("2. **WI-1** Script di validazione", updated)
        self.assertIn("3. **WI-2** Deduplicazione orizzontale", updated)
        self.assertIn("4. **WI-3** Split dei domini core", updated)
        self.assertIn("5. **WI-4** Logica di dominio fuori da n8n", updated)

        # 5. Verifica che sezioni storiche contenenti 'mirror' siano intatte
        self.assertIn("Mirror sincronizzato nel commit `a44bb0f`", updated)

        # 6. Verifica tabella commit e nota di completamento
        self.assertIn("| `7e55f78` | Sync mirror MiniApp Master API (`oyJX2al4JMmNz7lk`) via read-only MCP (2026-08-25) |", updated)
        self.assertIn("### Sincronizzazione Mirror MiniApp Master API (2026-08-25)", updated)

    def test_strict_idempotency_multiple_executions(self):
        """
        Verifica che rieseguire la funzione più volte consecutive:
        - Non duplichi la riga del commit '7e55f78' nella tabella;
        - Non duplichi la sezione '### Sincronizzazione Mirror MiniApp Master API (2026-08-25)';
        - Lasci la stringa identica (idempotenza perfetta: f(f(x)) == f(x)).
        """
        first_pass = update_handoff_content_deterministic(REAL_HANDOFF_SAMPLE)
        second_pass = update_handoff_content_deterministic(first_pass)
        third_pass = update_handoff_content_deterministic(second_pass)

        self.assertEqual(first_pass, second_pass)
        self.assertEqual(second_pass, third_pass)

        # Conteggio esatto: una sola occorrenza del commit 7e55f78 nella tabella e nella nota
        self.assertEqual(third_pass.count("`7e55f78`"), 2)  # 1 nella tabella commit, 1 nel blocco note
        self.assertEqual(third_pass.count("### Sincronizzazione Mirror MiniApp Master API (2026-08-25)"), 1)


if __name__ == "__main__":
    unittest.main()
