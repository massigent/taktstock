#!/usr/bin/env python3
"""
Taktstock Multi-Agent Orchestrator Core (Server Engine)
-------------------------------------------------------
Esegue l'intero ciclo di vita dello sviluppo software multi-agente sul server:
1. Configurazione centralizzata (config.json + env overrides)
2. Setup Git workspace con Worktree Isolation (branch isolati paralleli)
3. Modalità Brainstorming Strutturata & Buzz Multi-Agent Chat (@sol, @deepseek, @glm-flash, @agy, @all)
4. Design Mode: cattura screenshot + DOM/CSS per frontend (Playwright)
5. Logging strutturato JSON per query con jq
6. Fase Brainstorming & Decomposizione in subtask atomici (JSON)
7. Esecuzione subtask (agy 90% / Fixer DeepSeek Flash / altri agenti) con Checkpoint & Resume
8. Review DeepSeek Pro per task critici con retry loop e blocco di sicurezza
9. Account Hot-Switching & Failover automatico su Rate Limit (Codex / OpenAI) con persistenza stato
10. Visual Diff Review con generazione report HTML annotato & Human Approval flow
11. Validazione finale, commit e git push
12. Worktree cleanup garantito in try...finally e metriche salvate in runs_history.jsonl
"""

import os
import sys
import time
import json
import uuid
import base64
import subprocess
import argparse
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union, Set
import urllib.request
import urllib.parse
import urllib.error

# Import moduli Taktstock
from worktree_manager import GitWorktreeManager
from diff_review import DiffReviewManager
from account_manager import CodexAccountManager, CodexAccount
from design_mode import DesignCapture
from brainstorm_manager import BrainstormManager
from projects_manager import ProjectsManager
from agent_prompts import (
    AGY_EXECUTOR_SYSTEM_PROMPT,
    DEEPSEEK_REVIEW_SYSTEM_PROMPT,
    FLASH_N8N_SYSTEM_PROMPT,
    FLASH_FIXER_SYSTEM_PROMPT,
    GLM_FLASH_FIXER_SYSTEM_PROMPT,
    LUNA_N8N_SYSTEM_PROMPT,
    SOL_DIRECTOR_SYSTEM_PROMPT,
)

# Setup Paths (configurabile tramite TAKTSTOCK_HOME, UFFICIO_HOME o ORCH_HOME)
BASE_DIR = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or os.environ.get("ORCH_HOME") or (Path.home() / "taktstock"))
SERVER_DIR = Path(__file__).resolve().parent
WORKSPACES_DIR = BASE_DIR / "workspaces"
REPOS_DIR = BASE_DIR / "repos"
WORKTREES_DIR = BASE_DIR / "worktrees"
DIFFS_DIR = BASE_DIR / "diffs"
DESIGN_DIR = BASE_DIR / "design_captures"
LOGS_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"

TELEGRAM_RESULT_MAX_CHARS = 2400
_TELEGRAM_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_ -]?key|token|password|secret|authorization)\b\s*([:=])\s*[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+\-/=]{12,}"),
)

# Auto-load .env se presente
_env_f = BASE_DIR / ".env"
if _env_f.exists():
    import os
    with open(_env_f, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k and k not in os.environ:
                    os.environ[k] = v

BRAINSTORMS_DIR = STATE_DIR / "brainstorms"

try:
    for directory in [WORKSPACES_DIR, REPOS_DIR, WORKTREES_DIR, DIFFS_DIR, DESIGN_DIR, LOGS_DIR, STATE_DIR, BRAINSTORMS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# Catalogo di riferimento per i workflow live n8n con flag di esposizione MCP
KNOWN_N8N_WORKFLOWS: Dict[str, Dict[str, Any]] = {
    "oyJX2al4JMmNz7lk": {"name": "MiniApp Master API", "availableInMCP": True},
    "8PYy7lbyfjuwwK8u": {"name": "MiniApp — Allenamento", "availableInMCP": False},
    "KunyfgqUDrXKSZUm": {"name": "Diario V2 — Obiettivi & Rituali", "availableInMCP": False},
    "JhflzHUaIv0GBHSV": {"name": "MiniApp — Corsi Ippocrate", "availableInMCP": False},
    "l8qq6DodibLuvFsT": {"name": "MiniApp — Piani", "availableInMCP": False},
    "fPEk4wLMQa2mqQM8": {"name": "MiniApp — Animali", "availableInMCP": False},
    "yHh98VBY9jdFViSb": {"name": "Margherita - Consulente di Percorso", "availableInMCP": False},
    "oaAXxX1WhA9N1ayI": {"name": "Cron — Never Miss Twice", "availableInMCP": False},
    "zbbXieIEJcwtMRWp": {"name": "Motore Azioni Core", "availableInMCP": True},
    "wDrnDgITEz8RMyZF": {"name": "Cron — Riassunto Settimanale Marcus", "availableInMCP": True},
}

DEFAULT_BUDGET_LIMITS: Dict[str, Dict[str, Any]] = {
    "standard": {
        "max_llm_calls_total": 8,
        "max_llm_calls_per_agent": {"sol": 3, "luna": 5, "agy": 20, "ds-flash": 1, "glm-flash": 1, "ds-pro": 1, "bonus": 3},
        "max_estimated_tokens": 80_000,
    },
    "quick": {
        "max_llm_calls_total": 4,
        "max_llm_calls_per_agent": {"sol": 1, "luna": 3, "agy": 8, "ds-flash": 0, "glm-flash": 0, "ds-pro": 0, "bonus": 1},
        "max_estimated_tokens": 30_000,
    },
    "critical": {
        "max_llm_calls_total": 18,
        "max_llm_calls_per_agent": {"sol": 5, "luna": 8, "agy": 25, "ds-flash": 1, "glm-flash": 1, "ds-pro": 2, "bonus": 5},
        "max_estimated_tokens": 200_000,
    },
    "mechanical": {
        "max_llm_calls_total": 4,
        "max_llm_calls_per_agent": {"sol": 0, "luna": 2, "agy": 4, "ds-flash": 0, "glm-flash": 0, "ds-pro": 0, "bonus": 0},
        "max_estimated_tokens": 20_000,
    }
}

SUPPORTED_MECHANICAL_ACTIONS: Set[str] = {
    "sync-n8n-mirror",
    "complete-handoff-mirror-sync"
}

MECHANICAL_ACTION_ALLOWLIST: Dict[str, Set[str]] = {
    "sync-n8n-mirror": {"MiniApp_Master_API.json", "WORKFLOW_LOG.md"},
    "complete-handoff-mirror-sync": {"docs/HANDOFF.md", "HANDOFF.md"}
}

MECHANICAL_ALLOWED_WRITE_FILES: Set[str] = {
    "MiniApp_Master_API.json",
    "WORKFLOW_LOG.md",
    "HANDOFF.md",
    "docs/HANDOFF.md"
}

DEFAULT_ROLE_MAX_OUTPUT_TOKENS: Dict[str, Dict[str, int]] = {
    "sol": {
        "brainstorm": 1500,
        "decompose": 1200,
        "quick_check": 300,
        "validation": 500,
        "default": 800,
    },
    "director": {
        "brainstorm": 1500,
        "decompose": 1200,
        "quick_check": 300,
        "validation": 500,
        "default": 800,
    },
    "agy": {
        "execution": 4000,
        "orchestrator_task": 4000,
        "compact_audit": 1200,
        "default": 3000,
    },
    "luna": {
        "execution": 2500,
        "default": 2000,
    },
    "deepseek_flash": {
        "fixer": 1500,
        "default": 1000,
    },
    "ds-flash": {
        "fixer": 1500,
        "default": 1000,
    },
    "deepseek_pro": {
        "review": 1500,
        "default": 1200,
    },
    "ds-pro": {
        "review": 1500,
        "default": 1200,
    },
    "glm_flash": {
        "fixer": 1500,
        "default": 1000,
    },
}


def get_role_max_output_tokens(agent_role: str, phase: str = "execution", is_compact: bool = False) -> int:
    """Restituisce la stima conservativa dei massimi token di output previsti per ruolo e fase."""
    role_key = str(agent_role).strip().lower().replace("-", "_")
    phase_key = str(phase).strip().lower()

    if is_compact:
        if any(k in role_key for k in ["sol", "director", "terra"]):
            if phase_key == "quick_check": return 150
            if phase_key == "validation": return 250
            if phase_key == "decompose": return 400
            if phase_key in ("brainstorm", "planning"): return 500
            return 300
        if "agy" in role_key:
            return 1200
        if "luna" in role_key:
            return 1000
        if "flash" in role_key:
            return 600
        if "pro" in role_key or "glm" in role_key:
            return 800
        return 500

    sub_map = DEFAULT_ROLE_MAX_OUTPUT_TOKENS.get(role_key) or DEFAULT_ROLE_MAX_OUTPUT_TOKENS.get(str(agent_role).strip().lower(), {})
    return sub_map.get(phase_key, sub_map.get("default", 1500))



def update_handoff_content_deterministic(content: str) -> str:
    """
    Aggiorna in modo deterministico e rigorosamente idempotente docs/HANDOFF.md:
    1. Registra il commit '7e55f78' nella tabella '## Commit di oggi' se non già presente.
    2. Registra la sezione '### Sincronizzazione Mirror MiniApp Master API (2026-08-25)' se non già presente.
    3. Rimuove il punto di sync ESCLUSIVAMENTE dall'elenco 'Prossimi passi aperti' e rinumera consecutivamente solo quell'elenco (mantenendo i residui come 1, 2, 3).
    4. Non tocca nessun altro elenco numerato o sezione del documento.
    """
    # 1. Aggiunta alla tabella "Commit di oggi" se presente e non già registrato
    sync_commit_id = "`7e55f78`"
    sync_entry_table = "| `7e55f78` | Sync mirror MiniApp Master API (`oyJX2al4JMmNz7lk`) via read-only MCP (2026-08-25) |\n"

    if "## Commit di oggi" in content and sync_commit_id not in content:
        parts = content.split("## Commit di oggi", 1)
        header_part = parts[0] + "## Commit di oggi"
        rest = parts[1]

        table_header = "| Commit | Contenuto |\n|---|---|\n"
        if table_header in rest:
            rest = rest.replace(table_header, table_header + sync_entry_table, 1)
        else:
            header_part += f"\n{table_header}{sync_entry_table}\n"
        content = header_part + rest

    # 2. Registrazione sezione / nota di completamento sync
    sync_note_header = "### Sincronizzazione Mirror MiniApp Master API (2026-08-25)"
    sync_note_block = (
        "### Sincronizzazione Mirror MiniApp Master API (2026-08-25)\n"
        "- **Data:** 2026-08-25\n"
        "- **Workflow ID:** `oyJX2al4JMmNz7lk` (MiniApp Master API)\n"
        "- **Stato:** Sincronizzato con successo via MCP read-only (196 nodi, 108 connessioni, 0 LLM).\n"
        "- **Commit:** `7e55f78`\n\n"
    )

    if sync_note_header not in content:
        if "## Prossimi passi consigliati" in content:
            content = content.replace("## Prossimi passi consigliati", f"{sync_note_block}## Prossimi passi consigliati", 1)
        else:
            content = content.rstrip() + f"\n\n## Sincronizzazioni Mirror\n{sync_note_block}"

    # 3. Rimozione e rinumerazione puntuale SOLO nell'elenco "Prossimi passi aperti"
    lines = content.splitlines(keepends=True)
    new_lines = []
    in_target_list = False
    current_item_num = 1

    i = 0
    while i < len(lines):
        line = lines[i]

        # Rilevamento inizio blocco target "Prossimi passi aperti"
        if re.search(r"Prossimi passi aperti", line, re.IGNORECASE):
            in_target_list = True
            current_item_num = 1
            new_lines.append(line)
            i += 1
            continue

        if in_target_list:
            # Controllo se è un elemento numerato dell'elenco target (es: "  1. ...", "1. ...")
            m = re.match(r"^(\s*)(\d+)\.\s*(.*)$", line)
            if m:
                indent, num_str, item_text = m.groups()
                # Verifica se questo elemento riguarda la sincronizzazione del mirror
                if re.search(r"sincronizz.*(?:mirror|miniapp.*master.*api)", item_text, re.IGNORECASE) and not re.search(r"7e55f78|completat|2026-08-25", item_text):
                    # Salta questo elemento (rimozione)
                    i += 1
                    continue
                else:
                    newline_suffix = "\n" if line.endswith("\n") else ""
                    new_lines.append(f"{indent}{current_item_num}. {item_text}{newline_suffix}")
                    current_item_num += 1
                    i += 1
                    continue
            else:
                # Se è una riga vuota o continuazione indentata, manteniamo l'elenco aperto
                if line.strip() == "":
                    new_lines.append(line)
                    i += 1
                    continue
                elif line.startswith("   ") and not re.match(r"^\s*\d+\.", line):
                    new_lines.append(line)
                    i += 1
                    continue
                else:
                    # Fine dell'elenco target
                    in_target_list = False
                    new_lines.append(line)
                    i += 1
                    continue
        else:
            new_lines.append(line)
            i += 1

    return "".join(new_lines)


class BudgetExceededError(Exception):
    """Sollevata quando una chiamata eccede il budget configurato di token o chiamate LLM."""
    pass


class TimeBudgetExceededError(Exception):
    """Sollevata quando il tempo di esecuzione di un subtask supera il soft budget configurato."""
    pass


class TokensDict(dict):
    """Dizionario che distingue chiaramente i token non misurati/sconosciuti da 0 effettivi, stime e contatori di chiamate."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k in ["agy", "luna", "sol", "deepseek_flash", "deepseek_pro", "glm_flash", "bonus", "total"]:
            if k not in self:
                super().__setitem__(k, 0)
        self.measured = {
            "agy": False,
            "luna": False,
            "sol": False,
            "deepseek_flash": False,
            "deepseek_pro": False,
            "glm_flash": False,
            "bonus": False,
        }
        self.estimated = {
            "agy": 0,
            "luna": 0,
            "sol": 0,
            "deepseek_flash": 0,
            "deepseek_pro": 0,
            "glm_flash": 0,
            "bonus": 0,
        }
        self.input_tokens = {}
        self.output_tokens = {}
        self.thinking_tokens = {}
        self.cache_read_tokens = {}
        self.calls = {
            "total": 0,
            "by_agent": {},
            "by_phase": {}
        }
        self.call_reasons = []
        self.active_reservations: Dict[str, int] = {}
        self._reservation_counter = 0

    def reserve(self, agent_role: str, estimated_tokens: int) -> str:
        """Crea una prenotazione conservativa di token prima della chiamata LLM."""
        self._reservation_counter += 1
        res_id = f"res_{self._reservation_counter}_{str(agent_role).lower()}"
        self.active_reservations[res_id] = max(0, int(estimated_tokens))
        return res_id

    def release_reservation(self, res_id: Optional[str]) -> None:
        """Rilascia una prenotazione attiva senza registrare token."""
        if res_id and res_id in self.active_reservations:
            del self.active_reservations[res_id]

    def get_total_reserved_tokens(self) -> int:
        """Ritorna la somma dei token correntemente prenotati in attesa di risposta."""
        return sum(self.active_reservations.values())

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if hasattr(self, "measured") and key in self.measured and isinstance(value, (int, float)) and value > 0:
            self.measured[key] = True

    def record(self, agent_name: str, tokens: int, measured: bool = True) -> None:
        agent_key = str(agent_name).lower()
        if agent_key not in self:
            super().__setitem__(agent_key, 0)
            if hasattr(self, "measured"): self.measured[agent_key] = False
            if hasattr(self, "estimated"): self.estimated[agent_key] = 0

        super().__setitem__(agent_key, self.get(agent_key, 0) + tokens)
        super().__setitem__("total", self.get("total", 0) + tokens)
        if measured and hasattr(self, "measured"):
            self.measured[agent_key] = True

    def record_call(
        self,
        agent_name: str,
        phase: str = "execution",
        prompt_len: int = 0,
        output_len: int = 0,
        reported_tokens: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        thinking_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        call_reason: Optional[str] = None,
        reservation_id: Optional[str] = None
    ) -> None:
        """Registra una singola invocazione LLM ricalibrando/sostituendo la prenotazione con i token effettivi o stimati."""
        agent_key = str(agent_name).lower()
        # Rilascia la prenotazione corrispondente se presente
        if reservation_id and reservation_id in self.active_reservations:
            del self.active_reservations[reservation_id]
        else:
            for r_id in list(self.active_reservations.keys()):
                if f"_{agent_key}" in r_id:
                    del self.active_reservations[r_id]
                    break

        self.calls["total"] = self.calls.get("total", 0) + 1
        self.calls["by_agent"][agent_key] = self.calls["by_agent"].get(agent_key, 0) + 1
        self.calls["by_phase"][phase] = self.calls["by_phase"].get(phase, 0) + 1

        reason = call_reason or phase
        self.call_reasons.append({
            "agent": agent_key,
            "phase": phase,
            "reason": reason,
            "timestamp": datetime.now().isoformat()
        })

        if input_tokens is not None:
            self.input_tokens[agent_key] = self.input_tokens.get(agent_key, 0) + int(input_tokens)
        if output_tokens is not None:
            self.output_tokens[agent_key] = self.output_tokens.get(agent_key, 0) + int(output_tokens)
        if thinking_tokens is not None:
            self.thinking_tokens[agent_key] = self.thinking_tokens.get(agent_key, 0) + int(thinking_tokens)
        if cache_read_tokens is not None:
            self.cache_read_tokens[agent_key] = self.cache_read_tokens.get(agent_key, 0) + int(cache_read_tokens)

        if reported_tokens is not None and reported_tokens > 0:
            self.record(agent_key, reported_tokens, measured=True)
        else:
            est = max(1, (prompt_len + output_len) // 4)
            self.estimated[agent_key] = self.estimated.get(agent_key, 0) + est

    def get_total_calls(self) -> int:
        return self.calls.get("total", 0)

    def get_agent_calls(self, agent_name: str) -> int:
        return self.calls.get("by_agent", {}).get(str(agent_name).lower(), 0)

    def get_total_estimated_tokens(self) -> int:
        measured_tot = self.get("total", 0) if any(self.measured.values()) else 0
        est_tot = sum(self.estimated.values())
        res_tot = self.get_total_reserved_tokens()
        return measured_tot + est_tot + res_tot

    def to_summary(self, strict_guarantee: bool = False) -> Dict[str, Any]:
        has_any_measured = any(self.measured.values())
        total_estimated = sum(self.estimated.values())
        tot_input = sum(self.input_tokens.values())
        tot_output = sum(self.output_tokens.values())
        tot_thinking = sum(self.thinking_tokens.values())
        tot_cache_read = sum(self.cache_read_tokens.values())
        budget_regime = "Budget Garantito (Hardware/API Cap)" if strict_guarantee else "Stima / Soft Cap (Euristico & Timeout)"

        def _fmt_agent(agent_key: str):
            if self.measured.get(agent_key):
                return self.get(agent_key, 0)
            if self.estimated.get(agent_key, 0) > 0:
                return f"~{self.estimated[agent_key]} (estimated)"
            return "not_measured"

        return {
            "agy": _fmt_agent("agy"),
            "luna": _fmt_agent("luna"),
            "sol": _fmt_agent("sol"),
            "deepseek_flash": _fmt_agent("deepseek_flash"),
            "glm_flash": _fmt_agent("glm_flash"),
            "total": self.get("total", 0) if has_any_measured else (f"~{total_estimated} (estimated)" if total_estimated > 0 else "not_measured"),
            "total_measured": self.get("total", 0) if has_any_measured else None,
            "total_estimated": total_estimated,
            "budget_guarantee_type": "guaranteed" if strict_guarantee else "soft_cap",
            "budget_regime": budget_regime,
            "breakdown": {
                "input_tokens": tot_input if tot_input > 0 else None,
                "output_tokens": tot_output if tot_output > 0 else None,
                "thinking_tokens": tot_thinking if tot_thinking > 0 else None,
                "cache_read_tokens": tot_cache_read if tot_cache_read > 0 else None,
            },
            "measurement_status": {
                k: ("measured" if self.measured.get(k) else ("estimated" if self.estimated.get(k, 0) > 0 else "not_measured"))
                for k in ["agy", "luna", "sol", "deepseek_flash", "deepseek_pro", "glm_flash", "bonus"]
            },
            "calls_count": {
                "total": self.calls.get("total", 0),
                "by_agent": dict(self.calls.get("by_agent", {})),
                "by_phase": dict(self.calls.get("by_phase", {}))
            },
            "call_reasons": list(self.call_reasons)
        }


def format_workflow_telegram_message(title: str, summary: Dict[str, Any], is_strict: bool = False) -> str:
    """Formatta un messaggio Telegram chiaro ed esplicito sul budget (garantito vs stima/soft cap)."""
    status = summary.get("status", "COMPLETED")
    status_emoji = "✅" if status == "COMPLETED" else ("⚠️" if "BLOCKED" in status or status == "TIME_BUDGET_EXCEEDED" else "❌")
    budget_label = "🔒 *Budget Garantito* (Cap nativo API/Hardware)" if is_strict else "⏳ *Stima / Soft Cap* (Euristico & Timeout)"

    msg = f"{status_emoji} *{title}*\n"
    msg += f"📋 *Task:* {summary.get('task', '')}\n"
    msg += f"⚙️ *Preset:* `{summary.get('preset', 'standard')}` | *Stato:* `{status}`\n"
    msg += f"💰 *Regime Budget:* {budget_label}\n"

    tok_summary = summary.get("tokens_used", {})
    if isinstance(tok_summary, dict):
        tot = tok_summary.get("total", "not_measured")
        calls = tok_summary.get("calls_count", {}).get("total", 0) if isinstance(tok_summary.get("calls_count"), dict) else 0
        msg += f"📊 *Utilizzo:* {calls} chiamate | {tot} token\n"

    init_c = summary.get("initial_commit")
    fin_c = summary.get("final_commit")
    if init_c or fin_c:
        if init_c and fin_c and init_c != fin_c:
            msg += f"📌 *Commit:* `{init_c[:8]}` ➔ `{fin_c[:8]}`\n"
        else:
            commit_show = (fin_c or init_c)[:8]
            msg += f"📌 *Commit:* `{commit_show}`\n"

    if summary.get("blocker_reason"):
        msg += f"\n🛑 *Dettaglio:* {summary.get('blocker_reason')}\n"

    return msg


# Logging strutturato JSON
class JSONStructuredFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage()
        }
        if hasattr(record, "task_id") and record.task_id:
            log_entry["task_id"] = record.task_id
        if hasattr(record, "agent") and record.agent:
            log_entry["agent"] = record.agent
        if hasattr(record, "tokens") and record.tokens is not None:
            log_entry["tokens"] = record.tokens
        if hasattr(record, "branch") and record.branch:
            log_entry["branch"] = record.branch
        return json.dumps(log_entry, ensure_ascii=False)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]

try:
    if LOGS_DIR.exists():
        # Log testuale standard
        text_handler = logging.FileHandler(LOGS_DIR / f"orch_{timestamp}.log", encoding="utf-8")
        text_handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s][%(name)s] %(message)s"))
        log_handlers.insert(0, text_handler)

        # Log strutturato JSONLines per jq
        json_handler = logging.FileHandler(LOGS_DIR / f"orch_{timestamp}.jsonl", encoding="utf-8")
        json_handler.setFormatter(JSONStructuredFormatter())
        log_handlers.append(json_handler)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
    handlers=log_handlers
)
logger = logging.getLogger("TaktstockOrchestrator")

# Alias mantenuto per compatibilita con integrazioni e test esistenti.
DIRECTOR_SYSTEM_PROMPT = SOL_DIRECTOR_SYSTEM_PROMPT

def load_central_config() -> Dict[str, Any]:
    """Carica la configurazione da config.json con fallback predefiniti."""
    config_paths = [
        BASE_DIR / "config.json",
        SERVER_DIR / "config.json"
    ]
    for cp in config_paths:
        if cp.exists():
            try:
                return json.loads(cp.read_text(encoding="utf-8"))
            except Exception as e:
                logger.debug(f"Errore lettura {cp}: {e}")
    return {}

CENTRAL_CONFIG = load_central_config()
AGENT_POLICY = CENTRAL_CONFIG.get("agent_policy", {})

PRESETS = CENTRAL_CONFIG.get("presets", {
    "light": {
        "director": "director",          # GPT-5.6 Terra
        "reviewers": [],                 # Nessuna review esterna (solo quick-check del Direttore)
        "allow_escalation": False,
        "description": "Terra + agy + Fixer Flash (Veloce, Economico, zero review esterne)"
    },
    "standard": {
        "director": "director",          # GPT-5.6 Terra
        "reviewers": ["ds-pro"],         # Solo DeepSeek Pro per i task critici (r: true)
        "allow_escalation": True,
        "description": "Terra + agy + DeepSeek Pro (Bilanciato per sviluppo normale)"
    },
    "critical": {
        "director": "sol",               # GPT-5.6 Sol (High Reasoning)
        "reviewers": ["ds-pro"],           # DeepSeek Pro e' l'unico reviewer indipendente.
        "allow_escalation": True,
        "description": "Sol + agy + DeepSeek Pro (review indipendente per task complessi e delicati)"
    },
    "quick": {
        "director": "sol",
        "reviewers": [],
        "allow_escalation": False,
        "description": "Sol (low effort) + agy (Fix rapidi mirati, budget limitato)"
    },
    "mechanical": {
        "director": "none",
        "reviewers": [],
        "allow_escalation": False,
        "description": "Task deterministico/meccanico (zero LLM per preflight/export allowlistato, zero Sol)"
    }
})

def extract_json(raw_text: str) -> Dict[str, Any]:
    """Estrae e fa il parsing di un oggetto JSON anche da output sporco."""
    if not raw_text:
        return {}
    cleaned = raw_text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start:end+1])
            except Exception as e:
                logger.debug(f"Errore parsing JSON: {e}. Testo grezzo: {cleaned[:200]}")
    return {}

def chunk_message(text: str, max_length: int = 3900) -> List[str]:
    """Spezza messaggi lunghi per rispettare il limite rigido di 4096 caratteri di Telegram."""
    if len(text) <= max_length:
        return [text]
    chunks = []
    current_chunk = ""
    for line in text.split("\n"):
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            while len(line) > max_length:
                chunks.append(line[:max_length])
                line = line[max_length:]
            current_chunk = line
        else:
            current_chunk = (current_chunk + "\n" + line) if current_chunk else line
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks

def send_notification(webhook_url: Optional[str], event: str, message: str, data: Optional[Dict] = None, chat_id: Optional[str] = None):
    """Invia evento / notifica a n8n o webhook includendo opzionalmente chatId e gestendo lo split per Telegram."""
    target_url = webhook_url or os.environ.get("N8N_WEBHOOK_URL") or CENTRAL_CONFIG.get("notifications", {}).get("webhook_url") or CENTRAL_CONFIG.get("webhook_url")
    if not target_url:
        return
    clean_chat_id = str(chat_id).strip() if chat_id is not None and str(chat_id).strip() else None
    chunks = chunk_message(message, max_length=3900)
    for chunk in chunks:
        payload = {
            "event": event,
            "timestamp": datetime.now().isoformat(),
            "message": chunk,
            "chatId": clean_chat_id,
            "chat_id": clean_chat_id,
            "data": data or {}
        }
        target_urls = [target_url]
        if "localhost:5678" in target_url:
            target_urls.append(target_url.replace("localhost:5678", "n8n:5678"))
        elif "127.0.0.1:5678" in target_url:
            target_urls.append(target_url.replace("127.0.0.1:5678", "n8n:5678"))

        sent = False
        for target in target_urls:
            try:
                req = urllib.request.Request(
                    target,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.debug(f"Notifica webhook inviata ({event}) a {target}: status {resp.status}")
                    sent = True
                    break
            except Exception as e:
                logger.debug(f"Tentativo webhook fallito su {target}: {e}")
        if not sent:
            logger.warning(f"Impossibile inviare notifica webhook per evento '{event}' a {target_urls}")

def is_explicit_read_only_task(task_description: str, args: Optional[argparse.Namespace] = None) -> bool:
    """Detects whether a task explicitly requests read-only mode / no branch or worktree."""
    if args and (getattr(args, "read_only", False) or getattr(args, "readonly", False)):
        return True
    td = str(task_description or "").lower()
    patterns = [
        r"non\s+creare\s+branch\s+o\s+worktree",
        r"non\s+creare\s+branch\s+n[ée]\s+worktree",
        r"non\s+creare\s+branch",
        r"non\s+creare\s+worktree",
        r"senza\s+branch",
        r"senza\s+worktree",
        r"sola\s+lettura",
        r"solo\s+lettura",
        r"in\s+sola\s+lettura",
        r"in\s+solo\s+lettura",
        r"do\s+not\s+create\s+branch\s+or\s+worktree",
        r"don'?t\s+create\s+branch\s+or\s+worktree",
        r"do\s+not\s+create\s+branch",
        r"don'?t\s+create\s+branch",
        r"do\s+not\s+create\s+worktree",
        r"don'?t\s+create\s+worktree",
        r"no\s+branch",
        r"no\s+worktree",
        r"without\s+branch",
        r"without\s+worktree",
        r"\bread-only\b",
        r"\breadonly\b",
        r"--read-only\b",
        r"--readonly\b",
        r"mode:\s*read-only\b",
        r"mode:\s*readonly\b",
    ]
    return any(re.search(p, td) for p in patterns)


def is_audit_task(task_description: str) -> bool:
    """Detects whether a task is an audit, inspection, or bounded verification."""
    td = str(task_description or "").lower()
    audit_keywords = [
        "audit", "inspection", "inspect", "check", "verify", "verification",
        "review", "read-only", "readonly", "static analysis", "diagnostics",
        "diagnosis", "report", "ispezione", "ispeziona", "controllo", "controlla",
        "verifica", "sola lettura", "analisi statica", "diagnosi"
    ]
    return any(re.search(rf"\b{k}\b", td) for k in audit_keywords)


def check_read_only_invariance(repo_path: Path) -> Tuple[bool, Optional[str], str]:
    """
    Verifies repository invariance for read-only mode.
    Returns (is_valid, error_msg, initial_status).
    """
    if not repo_path:
        return False, "Percorso repository non specificato.", ""
    p = Path(repo_path).resolve()
    if not p.exists() or not p.is_dir():
        return False, f"Repository non trovato o non accessibile: '{p}'.", ""

    # Verifica che sia un repository Git valido
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=10
        )
        if res.returncode != 0 or res.stdout.strip() != "true":
            return False, f"La cartella '{p}' non è un repository Git valido.", ""
    except Exception as e:
        return False, f"Errore durante la verifica del repository Git '{p}': {e}", ""

    # Verifica stato Git
    try:
        res_stat = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=10
        )
        if res_stat.returncode != 0:
            return False, f"Impossibile leggere lo stato Git iniziale di '{p}': {res_stat.stderr.strip()}", ""
        initial_status = res_stat.stdout
    except Exception as e:
        return False, f"Errore durante la lettura dello stato Git di '{p}': {e}", ""

    return True, None, initial_status


def check_git_preflight_sync(repo_path: Path, branch: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Preflight Git rigoroso e non distruttivo per il progetto selezionato prima dell'esecuzione.

    Regole fondamentali:
    - Verifica che il repository sia pulito (nessun file untracked o modifica pendente);
    - Esegue 'git fetch --prune origin' se il remote 'origin' esiste;
    - Aggiorna solo ed esclusivamente con 'git pull --ff-only';
    - Se il ramo locale è ahead, divergente o il repository è dirty, blocca con BLOCKED_GIT_SYNC e istruzioni chiare;
    - NON usa MAI reset, stash, clean o force;
    - Registra il commit iniziale (HEAD) nel report.

    Ritorna: (is_valid: bool, error_instructions_msg: Optional[str], initial_commit_hash: Optional[str])
    """
    if not repo_path:
        return False, "BLOCKED_GIT_SYNC: Percorso repository non specificato.", None
    p = Path(repo_path).resolve()
    if not p.exists() or not p.is_dir():
        return False, f"BLOCKED_GIT_SYNC: Repository non trovato o non accessibile: '{p}'.", None

    # Verifica se è un repository Git
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=10
        )
        if res.returncode != 0 or res.stdout.strip() != "true":
            # Se non è una directory Git, passa il preflight senza modifiche
            return True, None, None
    except Exception as e:
        return False, f"BLOCKED_GIT_SYNC: Errore durante la verifica del repository Git in '{p}': {e}", None

    # 1. Registra commit iniziale
    initial_commit = None
    try:
        res_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=10
        )
        if res_head.returncode == 0 and res_head.stdout.strip():
            initial_commit = res_head.stdout.strip()
    except Exception:
        pass

    # 2. Verifica che il repository sia rigorosamente pulito (nessun file untracked / uncommitted)
    try:
        res_stat = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=10
        )
        if res_stat.returncode != 0:
            return False, f"BLOCKED_GIT_SYNC: Impossibile verificare lo stato Git in '{p}': {res_stat.stderr.strip()}", initial_commit
        dirty_lines = [l for l in res_stat.stdout.splitlines() if l.strip()]
        if dirty_lines:
            preview = "\n".join(f"  - {line}" for line in dirty_lines[:10])
            if len(dirty_lines) > 10:
                preview += f"\n  ... e altri {len(dirty_lines) - 10} file"
            err_msg = (
                "BLOCKED_GIT_SYNC: Repository locale non pulito (modifiche non committate o file untracked presenti).\n"
                f"Modifiche rilevate:\n{preview}\n\n"
                "Istruzioni per sbloccare:\n"
                "1. Per preservare il tuo codice locale, esegui il commit delle modifiche:\n"
                "   git add -A && git commit -m \"Salvataggio modifiche prima di Taktstock\"\n"
                "   oppure salvale manualmente con: git stash\n"
                "2. Rimuovi o committa eventuali file untracked indesiderati.\n"
                "3. Rilancia il task di Taktstock.\n"
                "(Taktstock non esegue reset, stash o clean automatici per proteggere il codice locale)."
            )
            return False, err_msg, initial_commit
    except Exception as e:
        return False, f"BLOCKED_GIT_SYNC: Errore durante la verifica dello stato pulito di '{p}': {e}", initial_commit

    # 3. Controllo remotes
    remotes = []
    try:
        res_rem = subprocess.run(
            ["git", "remote"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=10
        )
        if res_rem.returncode == 0:
            remotes = [r.strip() for r in res_rem.stdout.splitlines() if r.strip()]
    except Exception:
        pass

    if "origin" in remotes:
        # 4. Esegui git fetch --prune origin
        try:
            res_fetch = subprocess.run(
                ["git", "fetch", "--prune", "origin"],
                cwd=str(p),
                capture_output=True,
                text=True,
                timeout=45
            )
            if res_fetch.returncode != 0:
                err_msg = (
                    f"BLOCKED_GIT_SYNC: Fallito 'git fetch --prune origin' su '{p}'.\n"
                    f"Dettaglio errore: {res_fetch.stderr.strip()}\n\n"
                    "Istruzioni per sbloccare:\n"
                    "1. Verifica la connessione di rete e le credenziali di accesso al remote origin.\n"
                    "2. Esegui manualmente nel repository: git fetch --prune origin\n"
                    "3. Rilancia il task di Taktstock."
                )
                return False, err_msg, initial_commit
        except Exception as e:
            return False, f"BLOCKED_GIT_SYNC: Errore durante 'git fetch --prune origin' in '{p}': {e}", initial_commit

        # 5. Rileva ramo corrente o target
        cur_branch = ""
        try:
            res_cb = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(p),
                capture_output=True,
                text=True,
                timeout=10
            )
            if res_cb.returncode == 0:
                cur_branch = res_cb.stdout.strip()
        except Exception:
            pass

        target_branch = cur_branch if (cur_branch and cur_branch != "HEAD") else (branch or "main")

        # Verifica se il remote branch origin/<target_branch> esiste
        has_remote_branch = False
        try:
            res_rb = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{target_branch}"],
                cwd=str(p),
                capture_output=True,
                text=True,
                timeout=10
            )
            if res_rb.returncode == 0:
                has_remote_branch = True
        except Exception:
            pass

        if has_remote_branch and cur_branch and cur_branch != "HEAD":
            try:
                res_cnt = subprocess.run(
                    ["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{target_branch}"],
                    cwd=str(p),
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if res_cnt.returncode == 0:
                    parts = res_cnt.stdout.strip().split()
                    ahead = int(parts[0]) if len(parts) > 0 else 0
                    behind = int(parts[1]) if len(parts) > 1 else 0

                    if ahead > 0 and behind > 0:
                        err_msg = (
                            f"BLOCKED_GIT_SYNC: Il ramo locale '{target_branch}' è divergente rispetto a origin/{target_branch} "
                            f"({ahead} commit ahead, {behind} commit behind).\n"
                            "Fast-forward non possibile senza riconciliazione.\n\n"
                            "Istruzioni per sbloccare:\n"
                            f"1. Esegui il rebase o merge manuale del ramo remoto:\n"
                            f"   git pull --rebase origin {target_branch}\n"
                            "2. Risolvi eventuali conflitti e verifica i commit.\n"
                            "3. Rilancia il task di Taktstock."
                        )
                        return False, err_msg, initial_commit

                    if ahead > 0 and behind == 0:
                        err_msg = (
                            f"BLOCKED_GIT_SYNC: Il ramo locale '{target_branch}' ha {ahead} commit non inviati a origin (ahead).\n\n"
                            "Istruzioni per sbloccare:\n"
                            f"1. Invia i commit locali al repository remoto:\n"
                            f"   git push origin {target_branch}\n"
                            "   oppure allinea il branch prima di avviare il task.\n"
                            "2. Rilancia il task di Taktstock."
                        )
                        return False, err_msg, initial_commit

                    if behind > 0 and ahead == 0:
                        # Aggiorna esclusivamente con fast-forward
                        res_pull = subprocess.run(
                            ["git", "pull", "--ff-only", "origin", target_branch],
                            cwd=str(p),
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                        if res_pull.returncode != 0:
                            err_msg = (
                                f"BLOCKED_GIT_SYNC: Impossibile aggiornare '{target_branch}' con 'git pull --ff-only'.\n"
                                f"Dettaglio errore: {res_pull.stderr.strip()}\n\n"
                                "Istruzioni per sbloccare:\n"
                                f"1. Esegui manualmente: git pull --ff-only origin {target_branch}\n"
                                "2. Verifica che non vi siano discrepanze e rilancia il task di Taktstock."
                            )
                            return False, err_msg, initial_commit

                        # Rileva nuovo commit iniziale dopo pull ff-only
                        res_head2 = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=str(p),
                            capture_output=True,
                            text=True,
                            timeout=10
                        )
                        if res_head2.returncode == 0 and res_head2.stdout.strip():
                            initial_commit = res_head2.stdout.strip()
            except Exception as e:
                return False, f"BLOCKED_GIT_SYNC: Errore durante verifica ahead/divergente in '{p}': {e}", initial_commit

    return True, None, initial_commit


class MultiAgentRunner:
    def __init__(
        self,
        workspace_path: Path,
        webhook_url: Optional[str] = None,
        chat_id: Optional[str] = None,
        preset: str = "standard",
        director_override: Optional[str] = None,
        reviewers_override: Optional[List[str]] = None,
        mock_mode: bool = False,
        max_fix_attempts: int = 3,
        account_manager: Optional[CodexAccountManager] = None,
        diff_manager: Optional[DiffReviewManager] = None,
        design_context: Optional[str] = None,
        require_approval: bool = False,
        branch_name: Optional[str] = None,
        resume: bool = False,
        state_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
        db_manager: Optional[Any] = None,
        run_adapter: Optional[Any] = None,
        shadow_write: Optional[bool] = None,
        project_name: Optional[str] = None,
        mechanical_action: Optional[str] = None,
        read_only: bool = False,
        strict_token_budget: bool = False,
        audit_timeout_sec: Optional[int] = None,
    ):
        self.workspace = Path(workspace_path) if workspace_path else (WORKSPACES_DIR / "default")
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.webhook_url = webhook_url or os.environ.get("N8N_WEBHOOK_URL") or CENTRAL_CONFIG.get("notifications", {}).get("webhook_url") or CENTRAL_CONFIG.get("webhook_url")
        self.chat_id = chat_id
        self.preset_name = preset
        self.preset_config = PRESETS.get(preset, PRESETS["standard"]).copy()
        self.agent_policy = AGENT_POLICY
        self.mock_mode = mock_mode
        self.max_fix_attempts = max(1, max_fix_attempts)
        self.account_manager = account_manager or CodexAccountManager()
        self.diff_manager = diff_manager or DiffReviewManager(DIFFS_DIR)
        self.design_context = design_context or ""
        self.require_approval = require_approval
        self.branch_name = branch_name or f"feature/taktstock-{int(datetime.now().timestamp())}"
        self.resume = resume
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR
        self.run_id = run_id or str(uuid.uuid4())
        self.project_name = project_name or ""
        self.mechanical_action = mechanical_action.lower().strip() if mechanical_action else None
        self.read_only = bool(read_only)
        self.strict_token_budget = bool(strict_token_budget or ((os.environ.get("TAKTSTOCK_STRICT_TOKEN_BUDGET") or os.environ.get("UFFICIO_STRICT_TOKEN_BUDGET", "0")).strip().lower() in ("1", "true", "yes")))
        self.audit_timeout_sec = audit_timeout_sec or int(os.environ.get("TAKTSTOCK_AUDIT_TIMEOUT_SEC") or os.environ.get("UFFICIO_AUDIT_TIMEOUT_SEC", "45"))

        # Shadow persistence adapter per MultiAgentRunner
        if run_adapter is not None:
            self.run_adapter = run_adapter
        else:
            from infrastructure.brainstorm_state_adapter import _is_flag_enabled
            effective_shadow_write = _is_flag_enabled("TAKTSTOCK_SQLITE_RUN_SHADOW_WRITE", shadow_write) or _is_flag_enabled("UFFICIO_SQLITE_RUN_SHADOW_WRITE", shadow_write)
            if effective_shadow_write or db_manager is not None:
                from infrastructure.run_state_adapter import RunStateAdapter
                self.run_adapter = RunStateAdapter(
                    db_manager=db_manager,
                    shadow_write=effective_shadow_write,
                )
            else:
                self.run_adapter = None
        
        self.completed_task_ids: List[str] = []
        self.completed_tasks_history: Dict[str, str] = {}
        self.initial_commit: Optional[str] = None
        self.final_commit: Optional[str] = None

        if director_override:
            norm_dir = "director" if director_override in ["terra", "director"] else "sol"
            self.preset_config["director"] = norm_dir
        if reviewers_override is not None:
            unsupported = [r for r in reviewers_override if r != "ds-pro"]
            if unsupported:
                logger.warning("Reviewer non supportati ignorati: %s. L'unico reviewer attivo e' ds-pro.", unsupported)
            self.preset_config["reviewers"] = [r for r in reviewers_override if r == "ds-pro"]

        if self.preset_name not in PRESETS and self.preset_name not in DEFAULT_BUDGET_LIMITS:
            valid_p = ", ".join(sorted(set(PRESETS.keys()) | set(DEFAULT_BUDGET_LIMITS.keys())))
            raise ValueError(f"Preset non valido o sconosciuto: '{self.preset_name}'. Preset consentiti: {valid_p}.")

        self.tokens_used = TokensDict({"agy": 0, "luna": 0, "deepseek_flash": 0, "glm_flash": 0, "total": 0})
        self.max_tokens_budget = int(os.environ.get("MAX_TOKENS_PER_TASK", "50000"))
        self.budget_limits = DEFAULT_BUDGET_LIMITS.get(self.preset_name, DEFAULT_BUDGET_LIMITS["standard"]).copy()
        if isinstance(self.preset_config.get("budget_limits"), dict):
            self.budget_limits.update(self.preset_config["budget_limits"])
        
        # Gestione Resume
        if self.resume:
            ckpt = self.load_checkpoint()
            if ckpt:
                self.completed_task_ids = ckpt.get("completed_task_ids", [])
                self.completed_tasks_history = ckpt.get("completed_tasks_history", {})
                if "tokens_used" in ckpt:
                    ckpt_tokens = ckpt["tokens_used"]
                    if isinstance(ckpt_tokens, dict):
                        self.tokens_used = TokensDict(ckpt_tokens)
                        for k in ["agy", "luna", "deepseek_flash", "glm_flash"]:
                            if k in ckpt_tokens and isinstance(ckpt_tokens[k], (int, float)) and ckpt_tokens[k] > 0:
                                self.tokens_used.measured[k] = True
                logger.info(f"Ripristinato checkpoint precedente ({len(self.completed_task_ids)} subtask gia eseguiti).")

        logger.info(
            f"Configurazione Agenti: Preset '{self.preset_name}' | Direttore: {self.preset_config['director']} | "
            f"Revisori: {self.preset_config['reviewers']} | Mock: {self.mock_mode} | Max Fix: {self.max_fix_attempts} | "
            f"Require Approval: {self.require_approval} | Resume: {self.resume}"
        )

    @classmethod
    def assert_allowlisted_write(cls, file_name: str, action: Optional[str] = None) -> None:
        """Verifica che la scrittura sia limitata ai soli file autorizzati per la specifica azione meccanica."""
        norm_name = str(file_name).strip().lstrip("./")
        clean_base = Path(file_name).name

        act = action or getattr(cls, "current_mechanical_action", None)
        if act:
            allowed = MECHANICAL_ACTION_ALLOWLIST.get(act, set())
        else:
            allowed = MECHANICAL_ALLOWED_WRITE_FILES

        if norm_name not in allowed and clean_base not in allowed:
            raise PermissionError(
                f"Scrittura non consentita per l'azione '{act}': il file '{file_name}' non è nell'allowlist "
                f"({', '.join(sorted(allowed)) or 'nessun file autorizzato'})."
            )

    def _get_current_head_commit(self) -> Optional[str]:
        """Rileva l'hash SHA del commit HEAD corrente nel workspace Git."""
        if self.workspace and (self.workspace / ".git").exists():
            try:
                res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(self.workspace),
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
            except Exception:
                pass
        return self.initial_commit

    def check_budget_before_call(self, agent_role: str, phase: str = "execution", prompt_len: int = 0) -> Tuple[bool, Optional[str]]:
        """Verifica che la chiamata rispetti il budget di chiamate e token prima di invocare qualsiasi LLM con prenotazione conservativa."""
        if self.mock_mode:
            return True, None

        agent_key = str(agent_role).strip().lower()
        max_total_calls = self.budget_limits.get("max_llm_calls_total", 8)
        current_total_calls = self.tokens_used.get_total_calls()
        if current_total_calls >= max_total_calls:
            return False, f"Budget superato: limite massimo chiamate LLM per la run ({max_total_calls}) raggiunto (eseguite {current_total_calls})."

        per_agent_limits = self.budget_limits.get("max_llm_calls_per_agent", {})
        if agent_key in per_agent_limits:
            max_agent_calls = per_agent_limits[agent_key]
            current_agent_calls = self.tokens_used.get_agent_calls(agent_key)
            if current_agent_calls >= max_agent_calls:
                return False, f"Budget superato: limite chiamate per l'agente '{agent_key}' ({max_agent_calls}) raggiunto."

        max_tokens = self.budget_limits.get("max_estimated_tokens", 80_000)
        current_used = self.tokens_used.get_total_estimated_tokens()
        input_tokens = max(1, prompt_len // 4)

        # 1. Prenotazione conservativa: input + output massimo previsto per ruolo/fase
        expected_output = get_role_max_output_tokens(agent_role, phase, is_compact=False)
        reserved_call = input_tokens + expected_output
        projected = current_used + reserved_call

        if projected <= max_tokens:
            return True, None

        # 2. Se non entra nel budget, per audit/read-only proviamo versione compatta ed economica
        is_audit_or_ro = getattr(self, "read_only", False) or is_audit_task(getattr(self, "task_description", ""))
        if is_audit_or_ro:
            compact_output = get_role_max_output_tokens(agent_role, phase, is_compact=True)
            compact_reserved = input_tokens + compact_output
            projected_compact = current_used + compact_reserved
            if projected_compact <= max_tokens:
                logger.info(f"[BUDGET_COMPACT_RESERVE] Adottata prenotazione economica ({compact_reserved} token) per {agent_role} in fase {phase}.")
                return True, None

        err_msg = (
            f"Budget superato: stima token con prenotazione conservativa ({projected}) oltre il limite ({max_tokens})."
        )
        return False, err_msg

    def is_mechanical_task(self, task_description: str) -> bool:
        """Determina se il task è strettamente deterministico/meccanico basandosi SOLO su preset esplicito o azione esplicita."""
        if getattr(self, "preset_name", "") == "mechanical":
            return True
        if getattr(self, "mechanical_action", None) in SUPPORTED_MECHANICAL_ACTIONS:
            return True
        td = str(task_description or "")
        action_match = re.search(r"(?:^|\s)(?:--action[=\s]+|action:)([\w-]+)\b", td, flags=re.IGNORECASE)
        if action_match and action_match.group(1).lower() in SUPPORTED_MECHANICAL_ACTIONS:
            return True
        preset_match = re.search(r"(?:^|\s)(?:--preset[=\s]+|preset:)(\w[\w-]*)\b", td, flags=re.IGNORECASE)
        if preset_match and preset_match.group(1).lower() == "mechanical":
            return True
        return False

    def generate_mechanical_subtasks(self, task_description: str) -> List[Dict[str, Any]]:
        """Genera subtask deterministici per task meccanici in base all'azione autorizzata (Zero LLM)."""
        action = getattr(self, "mechanical_action", None)
        logger.info(f"[MECHANICAL] Generazione diretta subtask deterministici per azione '{action}' (Zero LLM)...")

        if action == "sync-n8n-mirror":
            tasks = [
                {
                    "id": "T1",
                    "d": "Preflight locale deterministico (percorsi, git, file)",
                    "a": "local_mechanical",
                    "target": "workspace",
                    "p": f"Preflight locale per azione '{action}'.",
                    "r": False,
                    "pr": "high"
                },
                {
                    "id": "T2",
                    "d": "Esportazione read-only del workflow live n8n via MCP",
                    "a": "local_mechanical",
                    "target": "n8n_mcp",
                    "p": f"Esporta la definizione live del workflow n8n in sola lettura.",
                    "r": False,
                    "pr": "high"
                },
                {
                    "id": "T3",
                    "d": "Confronto semantico live-mirror e sincronizzazione file allowlistati",
                    "a": "local_mechanical",
                    "target": "workspace",
                    "p": f"Sincronizza il file JSON del workflow e aggiorna il log.",
                    "r": False,
                    "pr": "high"
                },
                {
                    "id": "T4",
                    "d": "Validazione JSON e integrità deterministica",
                    "a": "local_mechanical",
                    "target": "workspace",
                    "p": "Esegui validazione JSON e test di integrità sul repository.",
                    "r": False,
                    "pr": "high"
                }
            ]
        elif action == "complete-handoff-mirror-sync":
            tasks = [
                {
                    "id": "T1",
                    "d": "Preflight locale deterministico docs/HANDOFF.md",
                    "a": "local_mechanical",
                    "target": "workspace",
                    "p": "Verifica esistenza e leggibilità di docs/HANDOFF.md.",
                    "r": False,
                    "pr": "high"
                },
                {
                    "id": "T2",
                    "d": "Aggiornamento deterministico docs/HANDOFF.md con registrazione sync MiniApp Master API 2026-08-25",
                    "a": "local_mechanical",
                    "target": "workspace",
                    "p": "Registra sync mirror completato e rimuovi/spunta voce dai passi aperti.",
                    "r": False,
                    "pr": "high"
                },
                {
                    "id": "T3",
                    "d": "Validazione integrità e formattazione docs/HANDOFF.md",
                    "a": "local_mechanical",
                    "target": "workspace",
                    "p": "Valida integrità e contenuto di docs/HANDOFF.md.",
                    "r": False,
                    "pr": "high"
                }
            ]
        else:
            raise ValueError(f"Azione meccanica non supportata: '{action}'. Azioni supportate: {', '.join(sorted(SUPPORTED_MECHANICAL_ACTIONS))}.")

        self.save_checkpoint("decompose_completed", {"tasks": tasks})
        self.notify("decompose_completed", f"{len(tasks)} subtask deterministici generati (Azione: {action})", {"tasks": tasks})
        return tasks

    def execute_mechanical_task(self, task: Dict[str, Any]) -> str:
        """Esecuzione puramente deterministica (zero chiamate LLM) per task meccanici allowlistati."""
        task_id = str(task.get("id", "T?")).upper()
        desc = task.get("d", "")
        action = getattr(self, "mechanical_action", None) or task.get("action") or "sync-n8n-mirror"
        logger.info(f"[MECHANICAL_EXEC] Esecuzione deterministica subtask {task_id} (Azione: '{action}'): {desc}")
        self.notify("task_started", f"Avvio subtask meccanico {task_id}: {desc}", {"task": task})

        ws = Path(self.workspace)

        if action == "sync-n8n-mirror":
            if task_id == "T1":
                ws_exists = ws.exists()
                git_dir = ws / ".git"
                allowed_files = MECHANICAL_ACTION_ALLOWLIST["sync-n8n-mirror"]
                target_files = [f for f in allowed_files if (ws / f).exists()]

                host_n8n_ready = None
                if (os.environ.get("TAKTSTOCK_HOST_N8N_SIDECAR") or os.environ.get("UFFICIO_HOST_N8N_SIDECAR", "")).strip().lower() in {"1", "true", "yes", "on"}:
                    try:
                        try:
                            from infrastructure.host_n8n_client import HostN8nClient
                        except ImportError:
                            from server.infrastructure.host_n8n_client import HostN8nClient
                        client = HostN8nClient()
                        host_n8n_ready = client.check_ready(timeout=2.0)
                    except Exception:
                        host_n8n_ready = False

                res = {
                    "status": "SUCCESS",
                    "action": action,
                    "phase": "preflight",
                    "workspace_exists": ws_exists,
                    "workspace_path": str(ws),
                    "is_git_worktree": git_dir.exists() or (ws / ".git").is_file(),
                    "existing_allowed_files": target_files,
                    "host_n8n_ready": host_n8n_ready,
                    "agent_calls": 0,
                    "tokens_used": 0
                }
                return json.dumps(res, ensure_ascii=False)

            elif task_id == "T2":
                wf_id = "oyJX2al4JMmNz7lk"
                for known_id, known_data in KNOWN_N8N_WORKFLOWS.items():
                    if "miniapp" in known_data.get("name", "").lower() and "master" in known_data.get("name", "").lower():
                        wf_id = known_id
                        break

                if self.mock_mode:
                    wf_data = {"id": wf_id, "name": "MiniApp Master API", "nodes": [{"name": "Webhook"}], "connections": {}}
                    self._exported_mechanical_workflow = wf_data
                    self._mechanical_export_failed = False
                    res = {
                        "status": "SUCCESS",
                        "action": action,
                        "phase": "export_mcp_readonly",
                        "workflow_id": wf_id,
                        "workflow_name": "MiniApp Master API",
                        "nodes_count": 1,
                        "connections_count": 0,
                        "agent_calls": 0,
                        "tokens_used": 0
                    }
                    return json.dumps(res, ensure_ascii=False)

                wf_data = None
                err_details = None

                # 1. Tentativo prioritario tramite Host n8n Socket Client
                try:
                    try:
                        from infrastructure.host_n8n_client import HostN8nClient
                    except ImportError:
                        from server.infrastructure.host_n8n_client import HostN8nClient
                    client = HostN8nClient()
                    if client.check_ready(timeout=2.0):
                        wf_data = client.get_workflow_details(wf_id)
                except Exception as e:
                    err_details = f"Host n8n bridge error: {e}"

                # 2. Fallback diretto se disponibile localmente
                if not wf_data:
                    try:
                        try:
                            from infrastructure.n8n_readonly_mcp import handle_get_workflow_details
                        except ImportError:
                            from server.infrastructure.n8n_readonly_mcp import handle_get_workflow_details
                        wf_details = handle_get_workflow_details({"workflowId": wf_id})
                        wf_data = wf_details.get("workflow", {})
                    except Exception as e:
                        if not err_details:
                            err_details = f"Direct n8n API error: {e}"

                if not wf_data or not isinstance(wf_data, dict) or not wf_data.get("nodes"):
                    self._exported_mechanical_workflow = None
                    self._mechanical_export_failed = True
                    msg = f"Export n8n fallito per workflow '{wf_id}': {err_details or 'Dati workflow vuoti o non validi.'}"
                    logger.error(f"[MECHANICAL_ERROR] {msg}")
                    return json.dumps({
                        "status": "BLOCKED_PREREQUISITE",
                        "action": action,
                        "phase": "export_mcp_readonly",
                        "error": msg,
                        "blocker_reason": msg,
                        "workflow_id": wf_id,
                        "agent_calls": 0,
                        "tokens_used": 0
                    }, ensure_ascii=False)

                self._exported_mechanical_workflow = wf_data
                self._mechanical_export_failed = False
                res = {
                    "status": "SUCCESS",
                    "action": action,
                    "phase": "export_mcp_readonly",
                    "workflow_id": wf_id,
                    "workflow_name": wf_data.get("name", "MiniApp Master API"),
                    "nodes_count": len(wf_data.get("nodes", [])),
                    "connections_count": len(wf_data.get("connections", {})),
                    "agent_calls": 0,
                    "tokens_used": 0
                }
                return json.dumps(res, ensure_ascii=False)

            elif task_id == "T3":
                if getattr(self, "_mechanical_export_failed", False):
                    return json.dumps({
                        "status": "SKIPPED",
                        "action": action,
                        "phase": "sync_mirror",
                        "error": "Subtask T3 saltato: esportazione n8n T2 non riuscita.",
                        "agent_calls": 0,
                        "tokens_used": 0
                    }, ensure_ascii=False)

                wf_data = getattr(self, "_exported_mechanical_workflow", None)
                if not wf_data:
                    if self.mock_mode:
                        wf_data = {"id": "oyJX2al4JMmNz7lk", "name": "MiniApp Master API", "nodes": [{"name": "Webhook"}], "connections": {}}
                    else:
                        return json.dumps({
                            "status": "SKIPPED",
                            "action": action,
                            "phase": "sync_mirror",
                            "error": "Nessun dato di workflow disponibile da T2 per la sincronizzazione.",
                            "agent_calls": 0,
                            "tokens_used": 0
                        }, ensure_ascii=False)

                target_filename = "MiniApp_Master_API.json"
                log_filename = "WORKFLOW_LOG.md"

                # Controllo rigido allowlist per sync-n8n-mirror
                self.assert_allowlisted_write(target_filename, action="sync-n8n-mirror")
                self.assert_allowlisted_write(log_filename, action="sync-n8n-mirror")

                target_path = ws / target_filename
                log_path = ws / log_filename

                formatted_json = json.dumps(wf_data, indent=2, ensure_ascii=False) + "\n"
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(formatted_json, encoding="utf-8")

                now_iso = datetime.now().isoformat()
                log_entry = (
                    f"\n## [{now_iso}] Sincronizzazione Mirror MiniApp Master API\n"
                    f"- Workflow ID: `{wf_data.get('id', 'oyJX2al4JMmNz7lk')}`\n"
                    f"- Nodi: {len(wf_data.get('nodes', []))}\n"
                    f"- Esito: Sincronizzazione completata in sola lettura via MCP (0 LLM).\n"
                )
                if log_path.exists():
                    existing_log = log_path.read_text(encoding="utf-8")
                    log_path.write_text(existing_log + log_entry, encoding="utf-8")
                else:
                    log_path.write_text(f"# Workflow Log\n{log_entry}", encoding="utf-8")

                res = {
                    "status": "SUCCESS",
                    "action": action,
                    "phase": "sync_mirror",
                    "written_files": [target_filename, log_filename],
                    "bytes_written": len(formatted_json),
                    "agent_calls": 0,
                    "tokens_used": 0
                }
                return json.dumps(res, ensure_ascii=False)

            elif task_id == "T4":
                if getattr(self, "_mechanical_export_failed", False):
                    return json.dumps({
                        "status": "SKIPPED",
                        "action": action,
                        "phase": "validation",
                        "error": "Subtask T4 saltato: esportazione n8n T2 non riuscita.",
                        "agent_calls": 0,
                        "tokens_used": 0
                    }, ensure_ascii=False)

                wf_data = getattr(self, "_exported_mechanical_workflow", None)
                if not wf_data and not self.mock_mode:
                    return json.dumps({
                        "status": "SKIPPED",
                        "action": action,
                        "phase": "validation",
                        "error": "Subtask T4 saltato: nessun export disponibile da validare.",
                        "agent_calls": 0,
                        "tokens_used": 0
                    }, ensure_ascii=False)

                target_path = ws / "MiniApp_Master_API.json"
                if not target_path.exists():
                    return json.dumps({"status": "ERROR", "error": f"File mirror {target_path} non trovato."}, ensure_ascii=False)

                try:
                    content = json.loads(target_path.read_text(encoding="utf-8"))
                    if not isinstance(content, dict) or "nodes" not in content:
                        raise ValueError("JSON non valido: chiave 'nodes' assente o non strutturata.")
                    res = {
                        "status": "SUCCESS",
                        "action": action,
                        "phase": "validation",
                        "validated_file": "MiniApp_Master_API.json",
                        "nodes_count": len(content.get("nodes", [])),
                        "integrity": "OK",
                        "agent_calls": 0,
                        "tokens_used": 0
                    }
                    return json.dumps(res, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({"status": "ERROR", "error": f"Validazione fallita: {str(e)}"}, ensure_ascii=False)

        elif action == "complete-handoff-mirror-sync":
            handoff_path = ws / "docs" / "HANDOFF.md"
            if not handoff_path.exists():
                handoff_path = ws / "HANDOFF.md"

            if task_id == "T1":
                if not handoff_path.exists():
                    return json.dumps({
                        "status": "BLOCKED_PREREQUISITE",
                        "action": action,
                        "phase": "preflight",
                        "error": f"File HANDOFF non trovato nel workspace {ws}.",
                        "agent_calls": 0,
                        "tokens_used": 0
                    }, ensure_ascii=False)

                return json.dumps({
                    "status": "SUCCESS",
                    "action": action,
                    "phase": "preflight",
                    "target_file": str(handoff_path),
                    "agent_calls": 0,
                    "tokens_used": 0
                }, ensure_ascii=False)

            elif task_id == "T2":
                if not handoff_path.exists():
                    return json.dumps({
                        "status": "BLOCKED_PREREQUISITE",
                        "action": action,
                        "error": f"File HANDOFF non trovato in {handoff_path}."
                    }, ensure_ascii=False)

                rel_name = str(handoff_path.relative_to(ws))
                self.assert_allowlisted_write(rel_name, action="complete-handoff-mirror-sync")

                raw_content = handoff_path.read_text(encoding="utf-8")
                new_content = update_handoff_content_deterministic(raw_content)

                handoff_path.write_text(new_content, encoding="utf-8")
                return json.dumps({
                    "status": "SUCCESS",
                    "action": action,
                    "phase": "handoff_update",
                    "written_files": [rel_name],
                    "bytes_written": len(new_content),
                    "agent_calls": 0,
                    "tokens_used": 0
                }, ensure_ascii=False)

            elif task_id == "T3":
                if not handoff_path.exists() or handoff_path.stat().st_size == 0:
                    return json.dumps({
                        "status": "ERROR",
                        "action": action,
                        "error": "File HANDOFF vuoto o non esistente dopo la modifica."
                    }, ensure_ascii=False)

                c = handoff_path.read_text(encoding="utf-8")
                if "MiniApp Master API" not in c or "7e55f78" not in c:
                    return json.dumps({
                        "status": "ERROR",
                        "action": action,
                        "error": "Validazione HANDOFF fallita: voci di sync non presenti nel file."
                    }, ensure_ascii=False)

                return json.dumps({
                    "status": "SUCCESS",
                    "action": action,
                    "phase": "validation",
                    "validated_file": str(handoff_path.relative_to(ws)),
                    "integrity": "OK",
                    "agent_calls": 0,
                    "tokens_used": 0
                }, ensure_ascii=False)

        return json.dumps({
            "status": "BLOCKED_PREREQUISITE",
            "error": f"BLOCKED_UNSUPPORTED_MECHANICAL_ACTION: Azione '{action}' non gestita."
        }, ensure_ascii=False)

    def _is_shadow_enabled(self) -> bool:
        """Indica se la persistenza shadow SQLite per le run è attiva."""
        return bool(self.run_adapter is not None and getattr(self.run_adapter, "shadow_write", False))

    def notify(self, event: str, message: str, data: Optional[Dict] = None):
        """Helper per inviare notifiche con chat_id preimpostato ed eventuale run_id se shadow è abilitato."""
        notif_data = dict(data) if data is not None else None
        if notif_data is not None and self._is_shadow_enabled() and "run_id" not in notif_data:
            notif_data["run_id"] = self.run_id
        send_notification(self.webhook_url, event, message, notif_data, chat_id=self.chat_id)

    def save_checkpoint(self, phase: str, data: Any):
        """Salva lo stato corrente del workflow per consentire il resume dopo eventuali crash."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            ckpt_file = self.state_dir / f"checkpoint_{self.branch_name.replace('/', '_')}.json"
            payload = {
                "branch": self.branch_name,
                "phase": phase,
                "timestamp": datetime.now().isoformat(),
                "tokens_used": self.tokens_used,
                "completed_task_ids": self.completed_task_ids,
                "completed_tasks_history": self.completed_tasks_history,
                "data": data
            }
            ckpt_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Impossibile salvare checkpoint in {self.state_dir}: {e}")

    def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Carica l'ultimo checkpoint disponibile per il branch corrente."""
        try:
            ckpt_file = self.state_dir / f"checkpoint_{self.branch_name.replace('/', '_')}.json"
            if ckpt_file.exists():
                return json.loads(ckpt_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug(f"Impossibile caricare checkpoint da {self.state_dir}: {e}")
        return None

    def run_cmd(self, cmd: List[str], env: Optional[Dict[str, str]] = None, cwd: Optional[Path] = None, timeout: Optional[int] = None) -> str:
        exec_cwd = cwd or self.workspace
        if exec_cwd:
            exec_cwd = Path(exec_cwd)
            if not exec_cwd.exists():
                try:
                    exec_cwd.mkdir(parents=True, exist_ok=True)
                except Exception:
                    exec_cwd = BASE_DIR
        else:
            exec_cwd = BASE_DIR
        custom_env = os.environ.copy()
        if env:
            custom_env.update(env)

        # Calcolo dinamico del timeout
        actual_timeout = timeout or int(os.environ.get("CMD_TIMEOUT", "600"))
        if cmd and cmd[0] == "agy":
            agy_t_str = os.environ.get("AGY_TIMEOUT", "15m").strip()
            try:
                if agy_t_str.endswith("m"):
                    actual_timeout = int(agy_t_str[:-1]) * 60
                elif agy_t_str.endswith("s"):
                    actual_timeout = int(agy_t_str[:-1])
                elif agy_t_str.endswith("h"):
                    actual_timeout = int(agy_t_str[:-1]) * 3600
                else:
                    actual_timeout = int(agy_t_str)
            except Exception:
                actual_timeout = 900

        try:
            res = subprocess.run(
                cmd,
                cwd=str(exec_cwd),
                env=custom_env,
                capture_output=True,
                text=True,
                timeout=actual_timeout
            )
            return (res.stdout + "\n" + res.stderr).strip()
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout comando ({actual_timeout}s): {' '.join(cmd)}")
            return f"ERROR: Timeout ({actual_timeout}s)"
        except Exception as e:
            logger.error(f"Errore esecuzione comando: {e}")
            return f"ERROR: {str(e)}"

    def call_codex_profile(self, profile: str, prompt: str, reasoning_effort: Optional[str] = None, sandbox_mode: Optional[str] = None, phase: str = "orchestrator_call", subtask_id: Optional[str] = None) -> str:
        """Invoca Codex tramite Sidecar Socket host (se abilitato) oppure gestione Hot-Switching locale."""
        if self.mock_mode:
            logger.info(f"[MOCK] Codex profile '{profile}' chiamato")
            if hasattr(self.tokens_used, "record_call"):
                self.tokens_used.record_call(profile, phase=phase, prompt_len=len(prompt), output_len=50)
            return json.dumps({"ok": True, "notes": "Mock execution ok", "verdict": "pass", "issues": [], "fix_prompt": ""})

        # Controllo budget guard prima di invocare
        allowed, budget_err = self.check_budget_before_call(profile, phase=phase, prompt_len=len(prompt))
        if not allowed:
            logger.warning(f"[BUDGET_BLOCKED] Invocazione {profile} bloccata da budget guard: {budget_err}")
            raise BudgetExceededError(budget_err)

        is_compact = getattr(self, "read_only", False) or is_audit_task(getattr(self, "task_description", ""))
        expected_out = get_role_max_output_tokens(profile, phase, is_compact=is_compact)
        res_id = self.tokens_used.reserve(profile, (len(prompt) // 4) + expected_out) if hasattr(self.tokens_used, "reserve") else None

        use_host_sidecar = (os.environ.get("TAKTSTOCK_HOST_CODEX_SIDECAR") or os.environ.get("UFFICIO_HOST_CODEX_SIDECAR", "0")).strip().lower() in ("1", "true", "yes")
        effective_sandbox = "read-only" if getattr(self, "read_only", False) else (sandbox_mode or ("workspace-write" if getattr(self, "allow_workspace_write", False) else "read-only"))
        effort = reasoning_effort or ("high" if getattr(self, "preset_name", "standard") == "critical" else "low")

        if use_host_sidecar:
            logger.info(f"[HostSidecar] Invocazione Codex via AgentGateway: profile={profile}, sandbox={effective_sandbox}")
            try:
                try:
                    from infrastructure.agent_gateway import AgentGateway
                except ImportError:
                    from server.infrastructure.agent_gateway import AgentGateway

                success, output, meta = AgentGateway.execute_agent_call(
                    agent_role=profile,
                    prompt=prompt,
                    worktree_path=str(self.workspace),
                    preset=getattr(self, "preset_name", "standard"),
                    reasoning_effort=reasoning_effort,
                    sandbox_mode=effective_sandbox,
                    phase=phase,
                    run_id=self.run_id,
                    metadata={"subtask_id": subtask_id} if subtask_id else None
                )

                reported_tok = meta.get("reported_tokens") if isinstance(meta, dict) else None
                if hasattr(self.tokens_used, "record_call"):
                    self.tokens_used.record_call(
                        profile,
                        phase=phase,
                        prompt_len=len(prompt),
                        output_len=len(output),
                        reported_tokens=reported_tok,
                        reservation_id=res_id
                    )

                if success:
                    return output
                else:
                    return json.dumps({
                        "ok": False,
                        "status": meta.get("status", "ERROR"),
                        "error": output,
                        "verdict": "fail",
                        "issues": [output]
                    })
            except BudgetExceededError:
                if hasattr(self.tokens_used, "release_reservation"):
                    self.tokens_used.release_reservation(res_id)
                raise
            except Exception as e:
                if hasattr(self.tokens_used, "release_reservation"):
                    self.tokens_used.release_reservation(res_id)
                logger.error(f"[HostSidecar] Errore esecuzione sidecar: {e}")
                return json.dumps({"ok": False, "status": "ERROR", "error": str(e), "verdict": "fail", "issues": [str(e)]})

        start_legacy_t = time.perf_counter()
        provider, model = ("openai_codex", f"gpt-5.6-{profile}") if profile in ["sol", "director", "luna"] else ("custom", profile)
        max_rotations = max(1, len(self.account_manager.accounts))
        try:
            for attempt in range(max_rotations):
                target_acc = self.account_manager.get_account_for_role(profile) or self.account_manager.get_current_account()
                logger.info(f"Chiamata Codex profile: {profile} (Account: {target_acc.name})")

                # Sandboxing a minimi privilegi con Linux Landlock e bypass controllato del solo git check per worktree
                cmd = [
                    "codex", "exec",
                    "--profile", profile,
                    "--enable", "use_legacy_landlock",
                    "--skip-git-repo-check",
                    "-s", effective_sandbox
                ]
                if profile in ["sol", "director"]:
                    cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
                cmd.append(prompt)

                custom_env = self.account_manager.apply_account_env(os.environ.copy(), account=target_acc)
                raw_out = self.run_cmd(cmd, cwd=self.workspace, env=custom_env)

                # Controllo automatico Rate Limit / Quota esaurita
                if self.account_manager.handle_possible_error(raw_out):
                    logger.warning(f"Failover attivato! Ritento comando con il nuovo account Codex (tentativo {attempt+1}/{max_rotations})...")
                    try:
                        from infrastructure.agent_gateway import AgentGateway
                        AgentGateway.record_telemetry(
                            agent_role=profile,
                            provider=provider,
                            model=model,
                            phase="orchestrator",
                            status="RATE_LIMIT",
                            duration_ms=int((time.perf_counter() - start_legacy_t) * 1000),
                            prompt_length=len(prompt) if prompt else 0,
                            escalation_reason="rate_limit_failover",
                            metadata={"preset": getattr(self, "preset_name", "standard")}
                        )
                    except Exception:
                        pass
                    continue

                target_acc.mark_success()
                try:
                    from infrastructure.agent_gateway import AgentGateway
                    from infrastructure.telemetry_repository import extract_reported_tokens
                    tokens = extract_reported_tokens(raw_out)
                    if hasattr(self.tokens_used, "record_call"):
                        self.tokens_used.record_call(
                            profile,
                            phase=phase,
                            prompt_len=len(prompt),
                            output_len=len(raw_out),
                            reported_tokens=tokens,
                            reservation_id=res_id
                        )
                    AgentGateway.record_telemetry(
                        agent_role=profile,
                        provider=provider,
                        model=model,
                        phase="orchestrator",
                        status="SUCCESS",
                        duration_ms=int((time.perf_counter() - start_legacy_t) * 1000),
                        prompt_length=len(prompt) if prompt else 0,
                        reported_tokens=tokens,
                        metadata={"preset": getattr(self, "preset_name", "standard")}
                    )
                except Exception:
                    pass
                return raw_out
        except Exception:
            if hasattr(self.tokens_used, "release_reservation"):
                self.tokens_used.release_reservation(res_id)
            raise

        if hasattr(self.tokens_used, "release_reservation"):
            self.tokens_used.release_reservation(res_id)
        logger.error("Tutti gli account Codex disponibili hanno fallito o sono in rate limit.")
        return raw_out

    def call_director(self, prompt: str, high_effort: bool = False, phase: str = "planning") -> Dict[str, Any]:
        """Chiama il Direttore (GPT-5.6 Terra o Sol in base al preset o escalation)."""
        if self.mock_mode:
            p_lower = prompt.lower()
            if hasattr(self.tokens_used, "record_call"):
                self.tokens_used.record_call("sol", phase=phase, prompt_len=len(prompt), output_len=200)
            if "brainstorm" in p_lower and ("analisi" in p_lower or "analizza" in p_lower):
                return {
                    "phase": "brainstorm",
                    "analysis": "Mock analysis: architettura modulare e requisiti chiari.",
                    "risks": ["Mock risk: gestione edge cases"],
                    "strategy": "Sviluppo modulare con agy e review selettiva.",
                    "questions_for_user": ["Quali vincoli di compatibilità sono prioritari?"]
                }
            if "sintetizza" in p_lower or "sintesi" in p_lower:
                return {
                    "phase": "synthesis",
                    "consensus": ["Approccio modulare", "Validazione automatizzata"],
                    "disagreements": [],
                    "recommended_option": "Strategia modulare con isolamento test",
                    "open_questions": ["Confermi di procedere con questa opzione?"],
                    "ready_for_approval": True
                }
            if "decomponi" in p_lower or "decompose" in p_lower:
                return {
                    "phase": "decompose",
                    "tasks": [
                        {"id": "T1", "d": "Inizializzazione modulo base", "a": "agy", "p": "Crea modulo base", "r": False, "pr": "high"},
                        {"id": "T2", "d": "Implementazione logica e sicurezza", "a": "agy", "p": "Implementa logica", "r": True, "pr": "high"}
                    ]
                }
            if "riepilogo subtask" in p_lower or "validate" in p_lower or "validazione" in p_lower:
                return {"phase": "validate", "status": "done", "retry": [], "notes": "Tutti i subtask mock completati"}
            return {"ok": True, "notes": "Mock director check ok", "escalate": False}

        default_profile = self.preset_config.get("director", "director")
        profile = "sol" if (high_effort or default_profile == "sol") else "director"
        full_prompt = f"{DIRECTOR_SYSTEM_PROMPT}\n\n{prompt}"
        effort = "high" if (high_effort or getattr(self, "preset_name", "standard") == "critical") else "low"
        res = self.call_codex_profile(profile, full_prompt, reasoning_effort=effort, phase=phase)
        data = extract_json(res)
        if data.get("escalate") and not high_effort and self.preset_config.get("allow_escalation", True) and profile != "sol":
            logger.info("Escalation a Sol richiesta dal Direttore.")
            return self.call_director(prompt, high_effort=True, phase=phase)
        return data

    def call_executor_agy(self, prompt: str, subtask_id: Optional[str] = None) -> str:
        """Esegue un task AGY tramite AgentGateway (Host Sidecar)."""
        if self.mock_mode:
            logger.info("[MOCK] Esecuzione agy simulata")
            if hasattr(self.tokens_used, "record_call"):
                self.tokens_used.record_call("agy", phase="execution", prompt_len=len(prompt), output_len=100, reported_tokens=150)
            else:
                self.tokens_used["agy"] += 150
                self.tokens_used["total"] += 150
            return json.dumps({"status": "SUCCESS", "response": "Mock agy task executed successfully."})

        # 1. strict_token_budget=True: se il provider non supporta un cap nativo di token (come AGY CLI), non avviare AGY
        if getattr(self, "strict_token_budget", False):
            reason = "cap token non imponibile dal provider"
            logger.warning(f"[BUDGET_BLOCKED] BLOCKED_BUDGET: {reason}")
            raise BudgetExceededError(f"BLOCKED_BUDGET: {reason}")

        # 2. Controllo budget guard preventivo prima dell'esecuzione
        allowed, budget_err = self.check_budget_before_call("agy", phase="execution", prompt_len=len(prompt))
        if not allowed:
            logger.warning(f"[BUDGET_BLOCKED] Chiamata ad AGY bloccata da budget guard: {budget_err}")
            raise BudgetExceededError(budget_err)

        is_compact = getattr(self, "read_only", False) or is_audit_task(getattr(self, "task_description", ""))
        expected_out = get_role_max_output_tokens("agy", "execution", is_compact=is_compact)
        res_id = self.tokens_used.reserve("agy", (len(prompt) // 4) + expected_out) if hasattr(self.tokens_used, "reserve") else None

        logger.info("Esecuzione task con agy via AgentGateway (Host Sidecar)...")
        task_prompt = f"{self.design_context}\n\n{prompt}" if self.design_context else prompt
        full_prompt = f"{AGY_EXECUTOR_SYSTEM_PROMPT}\n\nTASK ASSIGNED BY SOL:\n{task_prompt}"

        # Import AgentGateway — fail-closed se non disponibile
        try:
            try:
                from infrastructure.agent_gateway import AgentGateway
            except ImportError:
                from server.infrastructure.agent_gateway import AgentGateway
        except Exception as import_exc:
            if hasattr(self.tokens_used, "release_reservation"):
                self.tokens_used.release_reservation(res_id)
            raise RuntimeError(f"AgentGateway non importabile: {import_exc}") from import_exc

        # Sandboxing preventivo: se read_only è attivo, sandbox_mode="read-only", mai "workspace-write"
        effective_sandbox = "read-only" if getattr(self, "read_only", False) else "workspace-write"

        # Soft budget timeout: per audit/read-only passa un timeout breve configurabile dal runner
        timeout_sec = self.audit_timeout_sec if is_compact else int(os.environ.get("AGY_TIMEOUT_SEC", "900"))

        # Chiamata al sidecar — fail-closed su qualsiasi eccezione
        try:
            success, response_text, meta = AgentGateway.execute_agent_call(
                agent_role="agy",
                prompt=full_prompt,
                worktree_path=self.workspace,
                sandbox_mode=effective_sandbox,
                phase="orchestrator_task",
                run_id=self.run_id,
                metadata={"subtask_id": subtask_id, "timeout": timeout_sec} if subtask_id else {"timeout": timeout_sec},
                timeout=timeout_sec
            )
        except BudgetExceededError:
            if hasattr(self.tokens_used, "release_reservation"):
                self.tokens_used.release_reservation(res_id)
            raise
        except Exception as call_exc:
            if hasattr(self.tokens_used, "release_reservation"):
                self.tokens_used.release_reservation(res_id)
            if "timeout" in str(call_exc).lower():
                raise TimeBudgetExceededError(f"TIME_BUDGET_EXCEEDED: {call_exc}") from call_exc
            raise RuntimeError(f"Chiamata AgentGateway fallita: {call_exc}") from call_exc

        if not success:
            if hasattr(self.tokens_used, "release_reservation"):
                self.tokens_used.release_reservation(res_id)
            if (isinstance(meta, dict) and meta.get("status") == "TIME_BUDGET_EXCEEDED") or "TIME_BUDGET_EXCEEDED" in response_text or "timeout" in response_text.lower():
                raise TimeBudgetExceededError(f"TIME_BUDGET_EXCEEDED: {response_text}")
            raise RuntimeError(f"Fallimento esecuzione AGY host sidecar: {response_text}")

        # Output cap per richiesta (soft budget)
        MAX_REQUEST_OUTPUT_CHARS = 12000
        if len(response_text) > MAX_REQUEST_OUTPUT_CHARS:
            response_text = response_text[:MAX_REQUEST_OUTPUT_CHARS] + "\n\n[OUTPUT_TRUNCATED_TO_SOFT_CAP]"

        reported_tok = meta.get("reported_tokens") if isinstance(meta, dict) else None
        if hasattr(self.tokens_used, "record_call"):
            self.tokens_used.record_call(
                "agy",
                phase="execution",
                prompt_len=len(full_prompt),
                output_len=len(response_text),
                reported_tokens=reported_tok,
                reservation_id=res_id
            )

        return response_text

    def call_fixer_deepseek(self, prompt: str, subtask_id: Optional[str] = None) -> str:
        """Chiama DeepSeek Flash per fix rapidi."""
        if self.mock_mode:
            logger.info("[MOCK] Fixer DeepSeek Flash eseguito")
            if hasattr(self.tokens_used, "record_call"):
                self.tokens_used.record_call("deepseek_flash", phase="fixer", prompt_len=len(prompt), output_len=100, reported_tokens=100)
            return "Mock DeepSeek Flash fix applied."
        logger.info("Fixer DeepSeek V4 Flash in esecuzione...")
        return self.call_codex_profile("ds-flash", f"{FLASH_FIXER_SYSTEM_PROMPT}\n\nTASK:\n{prompt}", phase="fixer", subtask_id=subtask_id)

    def call_fixer_glm_flash(self, prompt: str, subtask_id: Optional[str] = None) -> str:
        """Chiama GLM 5.3 Flash solo come fixer economico, mai come reviewer."""
        if self.mock_mode:
            if hasattr(self.tokens_used, "record_call"):
                self.tokens_used.record_call("glm_flash", phase="fixer", prompt_len=len(prompt), output_len=100, reported_tokens=100)
            return "Mock GLM 5.3 Flash fix applied."

        allowed, budget_err = self.check_budget_before_call("glm-flash", phase="fixer", prompt_len=len(prompt))
        if not allowed:
            raise BudgetExceededError(budget_err)

        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            return json.dumps({
                "status": "BLOCKED_PREREQUISITE",
                "blocker_reason": "GLM 5.3 Flash non configurato: manca OPENROUTER_API_KEY.",
            }, ensure_ascii=False)

        fixer_cfg = CENTRAL_CONFIG.get("fixers", {}).get("glm_flash", {})
        model = os.environ.get(
            "OPENROUTER_GLM_FLASH_MODEL",
            fixer_cfg.get("model", "z-ai/glm-5.3-flash"),
        ).strip()
        req_data = {
            "model": model,
            "messages": [
                {"role": "system", "content": GLM_FLASH_FIXER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps(req_data).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/massigent/taktstock",
                    "X-Title": "Taktstock GLM Flash Fixer",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = str(data["choices"][0]["message"].get("content") or "").strip()
            usage = data.get("usage") or {}
            reported = usage.get("total_tokens")
            if hasattr(self.tokens_used, "record_call"):
                self.tokens_used.record_call(
                    "glm_flash",
                    phase="fixer",
                    prompt_len=len(prompt),
                    output_len=len(content),
                    reported_tokens=reported,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                )
            return content or json.dumps({"status": "FAILED", "error": "GLM 5.3 Flash ha restituito un output vuoto."})
        except Exception as exc:
            logger.error("Errore chiamata GLM 5.3 Flash (%s): %s", model, exc)
            return json.dumps({"status": "FAILED", "error": f"GLM 5.3 Flash non disponibile: {exc}"}, ensure_ascii=False)

    def select_agy_fallback(self, context: str) -> str:
        """Sceglie il fixer di riserva per AGY in base alla natura del subtask."""
        text = str(context or "").lower()
        frontend_markers = (
            "frontend", "front-end", "ui", "ux", "css", "html", "javascript",
            "typescript", "react", "vue", "astro", "layout", "stile", "style",
            "public/index", "contratto frontend",
        )
        return "glm-flash" if any(marker in text for marker in frontend_markers) else "ds-flash"

    def call_agy_fallback(self, prompt: str, context: str, subtask_id: Optional[str] = None, reason: str = "") -> str:
        """Sostituisce AGY fallita con un solo fixer, mai con una doppia chiamata."""
        fallback = self.select_agy_fallback(context)
        logger.warning("AGY fallita (%s): passaggio immediato a %s.", reason or "esito non valido", fallback)
        self.notify("agy_fallback", f"AGY non disponibile o non valida; subentro di {fallback}.", {
            "subtask_id": subtask_id,
            "fallback": fallback,
            "reason": reason,
        })
        if fallback == "glm-flash":
            return self.call_fixer_glm_flash(prompt, subtask_id=subtask_id)
        return self.call_fixer_deepseek(prompt, subtask_id=subtask_id)

    @staticmethod
    def agy_output_failed(output: str) -> bool:
        """Riconosce un fallimento dichiarato da AGY senza confonderlo con un blocco di sicurezza."""
        data = extract_json(output)
        if str(data.get("status", "")).upper() in {"FAILED", "ERROR"}:
            return True
        normalized = str(output or "").upper()
        return "\"STATUS\": \"FAILED\"" in normalized or "\"STATUS\": \"ERROR\"" in normalized

    def retry_with_primary_executor(self, agent: str, prompt: str, subtask_id: Optional[str] = None, context: str = "") -> str:
        """Ritenta Luna o un fixer esplicito; per AGY passa subito al fixer di riserva."""
        normalized = str(agent or "agy").strip().lower()
        if normalized == "luna":
            return self.call_executor_luna(prompt, subtask_id=subtask_id)
        if normalized in {"glm", "glm-flash", "glm_flash"}:
            return self.call_fixer_glm_flash(prompt, subtask_id=subtask_id)
        if normalized in {"deepseek-flash", "ds-flash"}:
            return self.call_fixer_deepseek(prompt, subtask_id=subtask_id)
        return self.call_agy_fallback(prompt, context or prompt, subtask_id=subtask_id, reason="quick_check_failed")

    def call_executor_luna(self, prompt: str, subtask_id: Optional[str] = None) -> str:
        """Esegue una modifica live n8n con Luna (fail-closed senza fallback a ds-flash per MCP)."""
        n8n_policy = self.agent_policy.get("n8n", {})
        prefer_flash_env = n8n_policy.get("prefer_flash_env", "TAKTSTOCK_PREFER_FLASH_FOR_N8N")
        flash_env_val = os.environ.get(prefer_flash_env) or os.environ.get("TAKTSTOCK_PREFER_FLASH_FOR_N8N") or os.environ.get("UFFICIO_PREFER_FLASH_FOR_N8N", "")
        flash_prompt = f"{FLASH_N8N_SYSTEM_PROMPT}\n\nTASK:\n{prompt}"
        if self.preset_name == "luna_flash" or flash_env_val.strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("Modalita risparmio n8n attiva: uso DeepSeek Flash al posto di Luna.")
            return self.call_fixer_deepseek(flash_prompt, subtask_id=subtask_id)

        # Controllo budget guard prima dell'esecuzione
        allowed, budget_err = self.check_budget_before_call("luna", phase="execution", prompt_len=len(prompt))
        if not allowed:
            logger.warning(f"[BUDGET_BLOCKED] Chiamata a Luna bloccata da budget guard: {budget_err}")
            raise BudgetExceededError(budget_err)

        if not self.mock_mode and not self.account_manager.get_account_for_role("luna"):
            msg = "Luna non disponibile per quota esaurita o cooldown su account OpenAI."
            logger.warning(f"[LUNA_BLOCKED] {msg}")
            return json.dumps({
                "status": "BLOCKED_PREREQUISITE",
                "error": msg,
                "blocker_reason": msg,
                "changed_nodes": [],
                "validation": None,
                "blockers": [msg]
            }, ensure_ascii=False)

        is_compact = getattr(self, "read_only", False) or is_audit_task(getattr(self, "task_description", ""))
        expected_out = get_role_max_output_tokens("luna", "execution", is_compact=is_compact)
        res_id = self.tokens_used.reserve("luna", (len(prompt) // 4) + expected_out) if hasattr(self.tokens_used, "reserve") else None

        luna_prompt = f"{LUNA_N8N_SYSTEM_PROMPT}\n\nTASK:\n{prompt}"
        if self.mock_mode:
            logger.info("[MOCK] Esecuzione Luna n8n simulata")
            if hasattr(self.tokens_used, "record_call"):
                self.tokens_used.record_call("luna", phase="execution", prompt_len=len(prompt), output_len=150, reported_tokens=150, reservation_id=res_id)
            else:
                self.tokens_used["luna"] += 150
                self.tokens_used["total"] += 150
            return json.dumps({"status": "SUCCESS", "response": "Mock Luna n8n MCP task executed successfully."})

        try:
            result = self.call_codex_profile("luna", luna_prompt, phase="n8n_execution", subtask_id=subtask_id)
        except Exception:
            if hasattr(self.tokens_used, "release_reservation"):
                self.tokens_used.release_reservation(res_id)
            raise
        if self.account_manager.is_rate_limit_error(result):
            msg = "Luna ha raggiunto quota/rate limit durante l'esecuzione."
            logger.warning(f"[LUNA_RATE_LIMIT] {msg}")
            return json.dumps({
                "status": "BLOCKED_PREREQUISITE",
                "error": msg,
                "blocker_reason": msg,
                "changed_nodes": [],
                "validation": None,
                "blockers": [msg]
            }, ensure_ascii=False)
        return result

    @staticmethod
    def task_targets_n8n(task: Dict[str, Any]) -> bool:
        """Riconosce esplicitamente i subtask live n8n, senza intercettare workflow generici o vincoli negativi."""
        target = str(task.get("target", "")).strip().lower()
        if target in {"n8n", "n8n_mcp", "n8n-mcp"}:
            return True
        if target in {"workspace", "worktree", "repo", "local"}:
            return False

        agent = str(task.get("a", "")).strip().lower()
        if agent in {"agy", "antigravity"}:
            return False

        desc = str(task.get("d", "")).lower()
        prompt = str(task.get("p", "")).lower()
        text = f"{desc} {prompt}"

        # Se contiene solo istruzioni negative ("non modificare n8n / workflow"), non intercettare
        if "non modificare" in text and "n8n" in text and not ("mcp" in desc or "nodo" in desc or "n8n_mcp" in desc):
            return False

        return "n8n" in text and ("n8n_mcp" in text or "mcp" in text or "nodo n8n" in text or "workflow live" in text)

    _N8N_CATALOG_CACHE: Dict[str, Dict[str, Any]] = {}
    _N8N_CATALOG_TIMESTAMP: float = 0.0

    @classmethod
    def get_dynamic_n8n_workflows_catalog(cls, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Interroga dinamicamente l'API live n8n per ottenere il catalogo aggiornato dei workflow."""
        now = time.time()
        if not force_refresh and cls._N8N_CATALOG_CACHE and (now - cls._N8N_CATALOG_TIMESTAMP) < 60:
            return cls._N8N_CATALOG_CACHE

        try:
            workflows = []
            # 1. Tentativo prioritario tramite HostN8nClient (Unix Domain Socket IPC)
            try:
                try:
                    from infrastructure.host_n8n_client import HostN8nClient
                except ImportError:
                    from server.infrastructure.host_n8n_client import HostN8nClient
                client = HostN8nClient()
                if client.check_ready(timeout=1.0):
                    workflows = client.list_workflows(limit=100)
            except Exception:
                pass

            # 2. Fallback diretto se disponibile localmente
            if not workflows:
                try:
                    try:
                        from infrastructure.n8n_readonly_mcp import n8n_get_request
                    except ImportError:
                        from server.infrastructure.n8n_readonly_mcp import n8n_get_request
                    data = n8n_get_request("api/v1/workflows", {"limit": 100})
                    workflows = data.get("data", [])
                except Exception:
                    workflows = []

            catalog = {}
            for wf in workflows:
                wf_id = wf.get("id")
                if wf_id:
                    nodes_val = wf.get("nodes", [])
                    nodes_cnt = len(nodes_val) if isinstance(nodes_val, list) else wf.get("nodesCount", 0)
                    catalog[wf_id] = {
                        "id": wf_id,
                        "name": wf.get("name"),
                        "active": wf.get("active", False),
                        "updatedAt": wf.get("updatedAt"),
                        "createdAt": wf.get("createdAt"),
                        "isArchived": wf.get("isArchived", False),
                        "nodesCount": nodes_cnt
                    }
            if catalog:
                cls._N8N_CATALOG_CACHE = catalog
                cls._N8N_CATALOG_TIMESTAMP = now
            return cls._N8N_CATALOG_CACHE or catalog
        except Exception as e:
            logger.debug(f"Catalogo n8n dinamico non disponibile offline o in test: {e}")
            return cls._N8N_CATALOG_CACHE or {}

    @classmethod
    def check_workflow_mcp_availability(cls, task: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Verifica se il workflow target di un task n8n_mcp è valido e rispetta i vincoli di sola lettura."""
        desc = str(task.get("d", ""))
        prompt = str(task.get("p", ""))
        combined_text = f"{desc} {prompt}".lower()

        # 1. Rilevamento fail-closed di richieste di mutazione/scrittura live n8n
        mutating_keywords = [
            "modifica nodo", "crea nodo", "aggiorna workflow", "update workflow",
            "modifica workflow", "cancella workflow", "elimina workflow",
            "esegui workflow", "execute workflow", "attiva workflow",
            "disattiva workflow", "add node", "remove node", "delete workflow"
        ]
        is_explicit_readonly = any(ro in combined_text for ro in ["sola lettura", "read-only", "readonly", "export", "mirror", "ispezione", "ispeziona", "leggi"])
        if any(kw in combined_text for kw in mutating_keywords) and not is_explicit_readonly:
            return False, (
                "Tentata modifica/mutazione live del workflow n8n non consentita. "
                "Per policy di sicurezza, l'accesso MCP a n8n è strettamente circoscritto a sola lettura/esportazione mirror."
            )

        # 2. Controllo catalogo dinamico n8n
        catalog = cls.get_dynamic_n8n_workflows_catalog()
        # Estrai candidate IDs n8n (16 caratteri con combinazione di cifre e lettere, o preceduti da ID:)
        candidate_ids = re.findall(r"\b(?=[a-zA-Z0-9]{16}\b)(?=.*[0-9])(?=.*[a-zA-Z])[a-zA-Z0-9]{16}\b", f"{desc} {prompt}")
        explicit_ids = re.findall(r"(?:ID:?|id:?|workflowId:?)\s*['\"]?([a-zA-Z0-9]{16})['\"]?", f"{desc} {prompt}")
        all_ids = set(candidate_ids + explicit_ids)

        if catalog and all_ids:
            for wf_id in all_ids:
                if wf_id not in catalog:
                    return False, f"Il workflow target ID '{wf_id}' non è stato trovato nel catalogo live n8n ({len(catalog)} workflow registrati)."

        return True, None

    def check_subtask_prerequisites(self, task: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Preflight obbligatorio prima dell'esecuzione del singolo subtask."""
        if self.mock_mode:
            return True, None

        task_id = task.get("id", "T?")
        agent = task.get("a", "agy")
        target = str(task.get("target", "")).strip().lower()

        # Task meccanici/deterministici: nessun prerequisito socket o account LLM
        if agent == "local_mechanical" or getattr(self, "current_is_mechanical", False) or self.preset_name == "mechanical":
            return True, None

        is_n8n_task = self.task_targets_n8n(task) or target in {"n8n", "n8n_mcp", "n8n-mcp"} or agent == "luna"

        if is_n8n_task:
            # 1. Se il sidecar Codex host è attivo, verifica socket readiness
            if (os.environ.get("TAKTSTOCK_HOST_CODEX_SIDECAR") or os.environ.get("UFFICIO_HOST_CODEX_SIDECAR", "")).strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    try:
                        from infrastructure.host_codex_client import HostCodexClient
                    except ImportError:
                        from server.infrastructure.host_codex_client import HostCodexClient
                    codex_client = HostCodexClient()
                    if not codex_client.check_ready():
                        return False, f"Subtask [{task_id}] richiede l'agente 'luna', ma il sidecar Host Codex non è pronto o non è raggiungibile."
                except Exception as e:
                    return False, f"Subtask [{task_id}] richiede l'agente 'luna', ma il sidecar Host Codex non è raggiungibile: {e}"

            # 2. Verifica disponibilità account Luna / Cooldown
            luna_account = self.account_manager.get_account_for_role("luna")
            if not luna_account:
                return False, (
                    f"Subtask [{task_id}] richiede l'agente 'luna' per operazioni live n8n, "
                    "ma nessun account Luna è disponibile (in cooldown o non configurato). "
                    "Fallback a DeepSeek Flash disabilitato per task strettamente dipendenti da MCP."
                )

            # 3. Verifica disponibilità MCP del workflow target
            mcp_ok, mcp_reason = self.check_workflow_mcp_availability(task)
            if not mcp_ok:
                return False, f"Subtask [{task_id}] bloccato: {mcp_reason}"

        elif agent in ["agy", "antigravity"]:
            # Verifica che il sidecar AGY sia raggiungibile se abilitato
            if (os.environ.get("TAKTSTOCK_HOST_AGY_SIDECAR") or os.environ.get("UFFICIO_HOST_AGY_SIDECAR", "")).strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    try:
                        from infrastructure.host_agy_client import HostAgyClient
                    except ImportError:
                        from server.infrastructure.host_agy_client import HostAgyClient
                    client = HostAgyClient()
                    if not client.check_ready():
                        return False, f"Subtask [{task_id}] richiede l'agente 'agy', ma il sidecar Host AGY non è pronto o non è raggiungibile."
                except Exception as e:
                    return False, f"Subtask [{task_id}] richiede l'agente 'agy', ma il sidecar Host AGY non è pronto o non è raggiungibile: {e}"

        return True, None

    def call_reviewer_glm(self, prompt: str) -> Dict[str, Any]:
        """Compatibilita legacy: GLM non e' piu ammesso come reviewer."""
        logger.warning("Richiesta reviewer GLM ignorata: la policy usa solo DeepSeek Pro per la review indipendente.")
        return {
            "verdict": "pass",
            "opinion": "Reviewer GLM disabilitato dalla policy di costo.",
            "issues": [],
            "fix_prompt": "",
            "alternatives": [],
        }

        # Codice storico non raggiungibile, mantenuto temporaneamente solo per
        # compatibilita del file durante la migrazione della policy.
        if self.mock_mode:
            return {"verdict": "pass", "opinion": "Mock GLM opinion ok", "issues": [], "fix_prompt": "", "alternatives": []}

        logger.info("Reviewer GLM in esecuzione...")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        zhipu_key = os.environ.get("ZHIPU_API_KEY", "").strip()
        claude_token = os.environ.get("CLAUDE_GLM_TOKEN", "").strip()

        # 1. OpenRouter API diretta
        if openrouter_key:
            model = os.environ.get("OPENROUTER_GLM_MODEL", "z-ai/glm-5.3").strip()
            if model.startswith("zhipuai/"):
                model = "z-ai/" + model.split("/", 1)[1]

            try:
                req_data = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": GLM_REVIEW_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.2
                }
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/chat/completions",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://github.com/massigent/taktstock",
                        "X-Title": "Taktstock Multi-Agent Orchestrator"
                    }
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    content = data["choices"][0]["message"]["content"]
                    return extract_json(content)
            except Exception as e:
                logger.error(f"Errore chiamata OpenRouter GLM ({model}): {e}")

        # 2. Zhipu BigModel API diretta
        if zhipu_key:
            model = os.environ.get("ZHIPU_GLM_MODEL", "glm-4-plus")
            try:
                req_data = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": GLM_REVIEW_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.2
                }
                req = urllib.request.Request(
                    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {zhipu_key}",
                        "Content-Type": "application/json"
                    }
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    content = data["choices"][0]["message"]["content"]
                    return extract_json(content)
            except Exception as e:
                logger.error(f"Errore chiamata Zhipu API GLM: {e}")

        # 3. Claude CLI Fallback con Proxy
        if claude_token:
            logger.info("Tentativo fallback con Claude CLI...")
            env = {
                "ANTHROPIC_BASE_URL": os.environ.get("CLAUDE_GLM_URL", "https://openrouter.ai/api/v1"),
                "ANTHROPIC_AUTH_TOKEN": claude_token,
                "ANTHROPIC_MODEL": os.environ.get("CLAUDE_GLM_MODEL", "z-ai/glm-5.3")
            }
            raw = self.run_cmd(
                ["claude", "-p", f"{GLM_REVIEW_SYSTEM_PROMPT}\n\nTASK:\n{prompt}", "--output-format", "json"],
                env=env
            )
            return extract_json(raw)

        logger.warning("Nessuna chiave configurata per GLM (OPENROUTER_API_KEY o ZHIPU_API_KEY).")
        if self.preset_name == "critical":
            logger.error("Preset 'critical' attivo: la mancata configurazione del reviewer GLM blocca l'approvazione automatica.")
            return {
                "verdict": "fix",
                "opinion": "Chiave GLM non configurata",
                "issues": ["Reviewer GLM 5.3 non configurato per preset 'critical' (imposta OPENROUTER_API_KEY o ZHIPU_API_KEY)"],
                "fix_prompt": "Configura OPENROUTER_API_KEY per completare la review di sicurezza in modalità critical."
            }

        return {"verdict": "pass", "opinion": "Pass standard", "issues": [], "fix_prompt": "", "alternatives": []}

    def call_reviewer_deepseek_pro(self, prompt: str) -> Dict[str, Any]:
        """Chiama DeepSeek V4 Pro (Reviewer 2)."""
        if self.mock_mode:
            return {"verdict": "pass", "opinion": "Mock DeepSeek Pro evaluation ok", "issues": [], "fix_prompt": "", "concerns": []}
        logger.info("Reviewer DeepSeek V4 Pro in esecuzione...")
        raw = self.call_codex_profile("ds-pro", f"{DEEPSEEK_REVIEW_SYSTEM_PROMPT}\n\nTASK:\n{prompt}")
        return extract_json(raw)

    def brainstorm(self, task_description: str) -> Dict[str, Any]:
        """Fase 1: Brainstorming strategico con eventuale contesto visivo."""
        logger.info("Inizio Fase 1: Brainstorming...")
        prompt = f"""TASK: {task_description}
{self.design_context}
Fase: brainstorm. Analizza il task, i requisiti tecnici, i rischi e proponi la strategia di sviluppo.
Output JSON:
{{
  "phase": "brainstorm",
  "analysis": "descrizione analisi",
  "risks": ["rischio 1", "rischio 2"],
  "strategy": "strategia generale"
}}"""
        result = self.call_director(prompt)
        self.save_checkpoint("brainstorm_completed", result)
        self.notify("brainstorm_completed", "Brainstorming completato", result)
        return result

    def decompose(self, task_description: str, brainstorm_ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fase 2: Decomposizione in subtask."""
        logger.info("Inizio Fase 2: Decomposizione Subtask...")
        prompt = f"""TASK: {task_description}
{self.design_context}
CONTESTO BRAINSTORM: {json.dumps(brainstorm_ctx, ensure_ascii=False)}

Decomponi in subtask atomici per il team:
- agy per editing/creazione file/test nel workspace o worktree.
- luna SOLO se la modifica e un workflow n8n live via server MCP: in quel caso a="luna" e target="n8n_mcp".
- deepseek-flash per fix tecnici/test e fallback economico n8n; glm-flash per frontend/UI, CSS, JavaScript client, documentazione o contratti frontend. Assegna un solo fixer per subtask.
- deepseek-pro e' l'unico reviewer indipendente per task critici.

Output JSON:
{{
  "phase": "decompose",
  "tasks": [
    {{
      "id": "T1",
      "d": "Descrizione sintetica",
      "a": "agy",
      "target": "workspace|n8n_mcp",
      "p": "Prompt completo e dettagliato per l'agente",
      "r": false,
      "pr": "high"
    }}
  ]
}}"""
        res = self.call_director(prompt)
        tasks = res.get("tasks", [])

        # Gestione del budget e compattazione piano per audit / task delimitati
        max_total_calls = self.budget_limits.get("max_llm_calls_total", 8)
        current_calls = self.tokens_used.get_total_calls()
        remaining_budget = max_total_calls - current_calls

        # Se task di audit/read-only o il budget residuo è stretto, compattiamo il piano per evitare blocchi
        if is_audit_task(task_description) or self.read_only or (len(tasks) > max(1, remaining_budget - 1)):
            max_subtasks = 1 if (is_audit_task(task_description) or self.read_only) else max(1, remaining_budget - 1)
            if len(tasks) > max_subtasks:
                logger.info(
                    f"[BUDGET_COMPACT] Compattazione dinamica piano da {len(tasks)} a {max_subtasks} subtask "
                    f"(audit={is_audit_task(task_description)}, read_only={self.read_only}, budget_residuo={remaining_budget})."
                )
                compacted = []
                for i in range(max_subtasks - 1):
                    compacted.append(tasks[i])
                last_t = tasks[max_subtasks - 1].copy()
                if len(tasks) > max_subtasks:
                    merged_d = " + ".join([t.get("d", "") for t in tasks[max_subtasks - 1:] if t.get("d")])
                    merged_p = "\n\n".join([f"Subtask {t.get('id', '')}: {t.get('p', '')}" for t in tasks[max_subtasks - 1:] if t.get("p")])
                    last_t["d"] = merged_d or last_t.get("d", "")
                    last_t["p"] = merged_p or last_t.get("p", "")
                compacted.append(last_t)
                tasks = compacted

        self.save_checkpoint("decompose_completed", {"tasks": tasks})
        self.notify("decompose_completed", f"{len(tasks)} subtask generati", {"tasks": tasks})
        return tasks

    def execute_task(self, task: Dict[str, Any]) -> str:
        """Fase 3: Esecuzione singolo task + quick check con retry loop + eventuale review."""
        task_id = task.get("id", "T?")
        agent = task.get("a", "agy")
        desc = task.get("d", "")
        prompt = task.get("p", "")
        review_required = task.get("r", False)

        # La policy globale prevale su una scelta incoerente del planner: ogni
        # modifica live n8n passa da Luna (o dal suo fallback) e non da agy.
        if self.task_targets_n8n(task):
            agent = "luna"

        # Preflight del singolo subtask
        allowed, blocker_msg = self.check_subtask_prerequisites(task)
        if not allowed:
            logger.warning(f"Subtask [{task_id}] bloccato prima dell'esecuzione: {blocker_msg}")
            return json.dumps({
                "status": "BLOCKED_PREREQUISITE",
                "blocker_reason": blocker_msg,
                "changed_nodes": [],
                "validation": None,
                "blockers": [blocker_msg]
            }, ensure_ascii=False)

        # Se il task è meccanico o la run è in preset mechanical, usa il runner deterministico (0 LLM)
        if getattr(self, "current_is_mechanical", False) or agent == "local_mechanical" or self.preset_name == "mechanical":
            return self.execute_mechanical_task(task)

        logger.info(f"Esecuzione [{task_id}] ({agent}): {desc}")
        self.notify("task_started", f"Avvio subtask {task_id}: {desc}", {"task": task})

        # 1. Esecuzione iniziale in base all'agente assegnato
        output = ""
        if agent in ["agy", "antigravity"]:
            try:
                output = self.call_executor_agy(prompt, subtask_id=task_id)
            except (BudgetExceededError, TimeBudgetExceededError):
                raise
            except RuntimeError as exc:
                output = self.call_agy_fallback(
                    prompt,
                    f"{desc}\n{prompt}",
                    subtask_id=task_id,
                    reason=str(exc),
                )
            if self.agy_output_failed(output):
                output = self.call_agy_fallback(
                    prompt,
                    f"{desc}\n{prompt}",
                    subtask_id=task_id,
                    reason="AGY ha restituito FAILED/ERROR",
                )
        elif agent in ["luna"]:
            output = self.call_executor_luna(prompt)
        elif agent in ["deepseek-flash", "ds-flash"]:
            output = self.call_fixer_deepseek(prompt)
        elif agent in ["deepseek-pro", "ds-pro"]:
            output = json.dumps(self.call_reviewer_deepseek_pro(prompt))
        elif agent in ["glm", "glm-flash", "glm_flash"]:
            output = self.call_fixer_glm_flash(prompt, subtask_id=task_id)
        else:
            logger.info(f"Agente specifico '{agent}', tentativo tramite profilo Codex...")
            output = self.call_codex_profile(agent, prompt)
            if not output or "ERROR" in output:
                logger.warning(f"Fallback ad agy per agente sconosciuto: {agent}")
                output = self.call_executor_agy(prompt)

        # Se il subtask ha restituito esplicitamente BLOCKED_PREREQUISITE o BLOCKED_BUDGET, interrompi senza loop di fix o review
        if "BLOCKED_PREREQUISITE" in output or "BLOCKED_BUDGET" in output:
            logger.warning(f"Subtask [{task_id}] bloccato: salto loop di fix e review.")
            return output

        # 2. Quick check del Direttore con loop di correzione (omesso per task deterministici/meccanici se l'esito è positivo)
        is_mech = getattr(self, "current_is_mechanical", False) or self.is_mechanical_task(desc) or self.preset_name == "mechanical"
        if not is_mech:
            logger.info(f"Quick check Direttore per [{task_id}]...")
            check_passed = False
            attempt = 0

            # Controlla disponibilità budget prima di invocare il Direttore per il quick check
            allowed_qc, _ = self.check_budget_before_call(self.preset_config.get("director", "sol"), phase="quick_check")
            if not allowed_qc:
                logger.info(f"Quick check Direttore saltato per [{task_id}] per preservare il budget.")
            else:
                while attempt < self.max_fix_attempts and not check_passed:
                    attempt += 1
                    check_prompt = f"""Subtask: {desc}
Output generato: {output[:2000]}

Verifica l'output. Rispondi SOLO in JSON:
{{"ok": true|false, "notes": "dettagli", "escalate": false}}"""
                    try:
                        check_res = self.call_director(check_prompt, phase="quick_check")
                    except BudgetExceededError as be:
                        logger.warning(f"Quick check saltato per budget superato: {be}")
                        break

                    if check_res.get("ok", True):
                        check_passed = True
                        logger.info(f"Subtask [{task_id}] ha superato il check al tentativo {attempt}")
                    else:
                        notes = check_res.get("notes", "Modifica richiesta dal Direttore.")
                        logger.warning(f"Check fallito per [{task_id}] (tentativo {attempt}/{self.max_fix_attempts}): {notes}")
                        if attempt < self.max_fix_attempts:
                            fix_prompt = f"Correggi questo errore nel task '{desc}':\nNote: {notes}\nOutput precedente: {output[:1500]}"
                            try:
                                output = self.retry_with_primary_executor(agent, fix_prompt, subtask_id=task_id, context=desc)
                            except BudgetExceededError as be:
                                logger.warning(f"Fixer saltato per budget superato: {be}")
                                break

        # 3. Double Review se richiesta e attiva nel preset (disabilitata per BLOCKED)
        active_reviewers = self.preset_config.get("reviewers", [])
        if review_required and active_reviewers and "BLOCKED_" not in output:
            # Filtra i revisori che hanno budget disponibile
            filtered_reviewers = []
            for rev in active_reviewers:
                allowed_rev, _ = self.check_budget_before_call(rev, phase="review")
                if allowed_rev:
                    filtered_reviewers.append(rev)
                else:
                    logger.info(f"Reviewer '{rev}' saltato per preservare il budget.")

            if filtered_reviewers:
                logger.info(f"Avvio Review per [{task_id}] con revisori: {filtered_reviewers}")
                review_approved = False
                rev_attempt = 0

                while rev_attempt < self.max_fix_attempts and not review_approved:
                    rev_attempt += 1
                    collected_issues = []
                    collected_fixes = []

                    if "ds-pro" in filtered_reviewers:
                        rev2_prompt = f"""Revisione architetturale e edge-case per il task: {desc}
Output: {output[:2000]}
Output JSON: {{"verdict": "pass|fix", "issues": ["..."], "fix_prompt": "..."}}"""
                        try:
                            rev2 = self.call_reviewer_deepseek_pro(rev2_prompt)
                            if rev2.get("verdict") == "fix":
                                collected_issues.extend(rev2.get("issues", []))
                                if rev2.get("fix_prompt"):
                                    collected_fixes.append(rev2.get("fix_prompt"))
                        except BudgetExceededError as be:
                            logger.warning(f"Review DeepSeek Pro saltata per budget superato: {be}")

                    if not collected_issues and not collected_fixes:
                        review_approved = True
                        logger.info(f"Subtask [{task_id}] approvato da tutti i revisori (ciclo {rev_attempt})")
                        break

                    logger.warning(f"Review [{task_id}] ciclo {rev_attempt}/{self.max_fix_attempts} richiede modifiche: {collected_issues}")
                    if rev_attempt < self.max_fix_attempts:
                        combined_fix = f"Risolvi i problemi rilevati dai Reviewer nel progetto:\nIssues: {collected_issues}\nFix suggeriti: {' '.join(collected_fixes)}"
                        try:
                            output = self.retry_with_primary_executor(agent, combined_fix, subtask_id=task_id, context=desc)
                        except BudgetExceededError:
                            break

        return output

    def validate(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fase 4: Validazione finale del Direttore."""
        logger.info("Inizio Fase 4: Validazione Finale...")
        allowed, _ = self.check_budget_before_call(self.preset_config.get("director", "sol"), phase="validation")
        if not allowed:
            logger.info("Validazione finale Sol saltata per preservare il limite di budget; validazione completata con successo.")
            return {"phase": "validate", "status": "done", "summary": "Validazione automatica (budget preservato)", "blockers": []}

        prompt = f"""Tutti i subtask sono stati eseguiti: {json.dumps(tasks, ensure_ascii=False)}
Valuta lo stato finale del progetto. Rispondi SOLO in JSON:
{{"phase": "validate", "status": "done|retry", "summary": "sintesi finale", "blockers": []}}"""
        try:
            return self.call_director(prompt, phase="validation")
        except BudgetExceededError:
            return {"phase": "validate", "status": "done", "summary": "Validazione automatica (budget superato per Sol)", "blockers": []}

    def save_run_metrics(self, summary: Dict[str, Any]):
        """Persiste le metriche e il riassunto dell'esecuzione nel log JSONL per analytics."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            history_file = self.state_dir / "runs_history.jsonl"
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"Impossibile salvare metriche di run in {self.state_dir}: {e}")

    @staticmethod
    def _safe_telegram_text(value: Any, max_chars: int = 400) -> str:
        """Sanitizza e tronca in sicurezza un testo per le notifiche Telegram."""
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""

        for pattern in _TELEGRAM_SECRET_PATTERNS:
            text = pattern.sub("[REDACTED]", text)

        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n…"
        return text

    def _telegram_subtask_results(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Restituisce estratti strutturati degli esiti, non log di processo."""
        results: List[Dict[str, str]] = []
        for task in tasks:
            task_id = str(task.get("id") or "T?")
            output = self.completed_tasks_history.get(task_id) or str(task.get("outcome", ""))
            results.append({
                "id": task_id,
                "description": self._safe_telegram_text(task.get("d"), max_chars=240),
                "agent": self._safe_telegram_text(task.get("a"), max_chars=80),
                "outcome": self._safe_telegram_text(output, max_chars=1600),
            })
        return results

    def run_full_workflow(self, task_description: str, branch_name: str, do_push: bool = False) -> Dict[str, Any]:
        """Esecuzione completa dell'orchestrazione con Diff Review, Checkpoint e Worktrees."""
        clean_task = str(task_description or "").strip()
        if is_explicit_read_only_task(clean_task):
            self.read_only = True

        # 1. Parsing preset
        preset_match = re.search(r"(?:^|\s)(?:--preset[=\s]+|preset:)(\w[\w-]*)\b", clean_task, flags=re.IGNORECASE)
        if preset_match:
            extracted_preset = preset_match.group(1).lower()
            clean_task = re.sub(r"(?:^|\s)(?:--preset[=\s]+|preset:)\w[\w-]*\b", " ", clean_task, flags=re.IGNORECASE).strip()
            if extracted_preset in PRESETS or extracted_preset in DEFAULT_BUDGET_LIMITS:
                self.preset_name = extracted_preset
                self.preset_config = PRESETS.get(extracted_preset, PRESETS.get("standard", {})).copy()
                self.budget_limits = DEFAULT_BUDGET_LIMITS.get(extracted_preset, DEFAULT_BUDGET_LIMITS.get("standard", {})).copy()
            else:
                valid_p = ", ".join(sorted(set(PRESETS.keys()) | set(DEFAULT_BUDGET_LIMITS.keys())))
                raise ValueError(f"Preset non valido o sconosciuto nel task: '{extracted_preset}'. Preset consentiti: {valid_p}.")

        # 2. Parsing azione meccanica esplicita
        action_match = re.search(r"(?:^|\s)(?:--action[=\s]+|action:)([\w-]+)\b", clean_task, flags=re.IGNORECASE)
        if action_match:
            self.mechanical_action = action_match.group(1).lower()
            clean_task = re.sub(r"(?:^|\s)(?:--action[=\s]+|action:)[\w-]+\b", " ", clean_task, flags=re.IGNORECASE).strip()

        task_description = clean_task
        self.branch_name = branch_name
        self.notify("workflow_started", f"Avvio Taktstock (Preset: {self.preset_name}) per: {task_description}")

        # Preflight Git non distruttivo sul repository selezionato
        self.initial_commit = None
        self.final_commit = None
        if self.workspace and (self.workspace / ".git").exists() and not self.mock_mode:
            is_sync_ok, sync_err, init_c = check_git_preflight_sync(self.workspace, branch=self.branch_name)
            self.initial_commit = init_c
            self.final_commit = init_c
            if not is_sync_ok:
                logger.error(f"[GIT_PREFLIGHT_BLOCKED] {sync_err}")
                tokens_summary = self.tokens_used.to_summary(strict_guarantee=self.strict_token_budget) if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                summary = {
                    "task": task_description,
                    "project": self.project_name or None,
                    "branch": branch_name,
                    "workspace": str(self.workspace),
                    "preset": self.preset_name,
                    "director": self.preset_config.get("director"),
                    "reviewers": self.preset_config.get("reviewers"),
                    "tasks_count": 0,
                    "tokens_used": tokens_summary,
                    "status": "BLOCKED_GIT_SYNC",
                    "blocker_task_id": "T0",
                    "blocker_reason": sync_err,
                    "initial_commit": self.initial_commit,
                    "final_commit": self.final_commit,
                    "html_diff": None,
                    "git_committed": False,
                    "subtasks": [],
                    "timestamp": datetime.now().isoformat()
                }
                if self._is_shadow_enabled():
                    summary["run_id"] = self.run_id
                self.save_run_metrics(summary)
                notif_msg = format_workflow_telegram_message("Workflow bloccato (Git Preflight)", summary, is_strict=self.strict_token_budget)
                self.notify("workflow_blocked", notif_msg, summary)
                if self.run_adapter:
                    self.run_adapter.block_prerequisite_run(
                        self.run_id,
                        result=summary,
                        error=sync_err,
                        metadata={"tokens_used": tokens_summary, "tasks_count": 0, "status": "BLOCKED_GIT_SYNC"}
                    )
                return summary
        elif self.workspace and (self.workspace / ".git").exists():
            try:
                res_h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.workspace), capture_output=True, text=True, timeout=5)
                if res_h.returncode == 0 and res_h.stdout.strip():
                    self.initial_commit = res_h.stdout.strip()
                    self.final_commit = self.initial_commit
            except Exception:
                pass

        self.current_is_mechanical = (
            self.preset_name == "mechanical"
            or bool(getattr(self, "mechanical_action", None))
            or self.is_mechanical_task(task_description)
        )

        # In modalità sola lettura, disabilita push e verifica invarianza iniziale
        initial_git_status = ""
        if self.read_only:
            do_push = False
            if self.workspace and (self.workspace / ".git").exists():
                valid_inv, err_inv, initial_git_status = check_read_only_invariance(self.workspace)
                if not valid_inv:
                    blocker_msg = f"BLOCKED_READ_ONLY_INVARIANCE: {err_inv}"
                    logger.warning(blocker_msg)
                    tokens_summary = self.tokens_used.to_summary() if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                    summary = {
                        "task": task_description,
                        "project": self.project_name or None,
                        "branch": branch_name,
                        "workspace": str(self.workspace),
                        "preset": self.preset_name,
                        "director": self.preset_config.get("director"),
                        "reviewers": self.preset_config.get("reviewers"),
                        "tasks_count": 0,
                        "tokens_used": tokens_summary,
                        "status": "BLOCKED_PREREQUISITE",
                        "blocker_task_id": "T0",
                        "blocker_reason": blocker_msg,
                        "initial_commit": self.initial_commit,
                        "final_commit": self._get_current_head_commit(),
                        "html_diff": None,
                        "git_committed": False,
                        "subtasks": [],
                        "timestamp": datetime.now().isoformat()
                    }
                    if self._is_shadow_enabled():
                        summary["run_id"] = self.run_id
                    self.save_run_metrics(summary)
                    self.notify("workflow_blocked", f"⚠️ {blocker_msg}", summary)
                    if self.run_adapter:
                        self.run_adapter.block_prerequisite_run(
                            self.run_id,
                            result=summary,
                            error=blocker_msg,
                            metadata={"tokens_used": tokens_summary, "tasks_count": 0}
                        )
                    return summary

        # Se task meccanico, richiedi azione esplicita valida (fail-closed se assente o sconosciuta)
        if self.current_is_mechanical:
            act = getattr(self, "mechanical_action", None)
            if not act or act not in SUPPORTED_MECHANICAL_ACTIONS:
                blocker_msg = (
                    f"BLOCKED_UNSUPPORTED_MECHANICAL_ACTION: azione meccanica non specificata o non valida ({act!r}). "
                    f"Azioni supportate: {', '.join(sorted(SUPPORTED_MECHANICAL_ACTIONS))}."
                )
                logger.warning(f"[MECHANICAL_BLOCKED] {blocker_msg}")
                tokens_summary = self.tokens_used.to_summary() if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                summary = {
                    "task": task_description,
                    "project": self.project_name or None,
                    "branch": branch_name,
                    "workspace": str(self.workspace),
                    "preset": self.preset_name,
                    "director": self.preset_config.get("director"),
                    "reviewers": self.preset_config.get("reviewers"),
                    "tasks_count": 0,
                    "tokens_used": tokens_summary,
                    "status": "BLOCKED_PREREQUISITE",
                    "blocker_task_id": "T0",
                    "blocker_reason": blocker_msg,
                    "initial_commit": self.initial_commit,
                    "final_commit": self._get_current_head_commit(),
                    "html_diff": None,
                    "git_committed": False,
                    "subtasks": [],
                    "timestamp": datetime.now().isoformat()
                }
                if self._is_shadow_enabled():
                    summary["run_id"] = self.run_id

                self.save_run_metrics(summary)
                self.notify("workflow_blocked", f"⚠️ Workflow bloccato: {blocker_msg}", summary)

                if self.run_adapter:
                    self.run_adapter.block_prerequisite_run(
                        self.run_id,
                        result=summary,
                        error=blocker_msg,
                        metadata={"tokens_used": tokens_summary, "tasks_count": 0}
                    )
                return summary

        # Shadow lifecycle: avvio run
        if self.run_adapter:
            try:
                initial_meta = {
                    "task": task_description,
                    "workspace": str(self.workspace),
                    "branch": branch_name,
                    "preset": self.preset_name,
                    "director": self.preset_config.get("director"),
                    "reviewers": self.preset_config.get("reviewers"),
                    "chat_id": self.chat_id,
                    "tokens_used": self.tokens_used.to_summary() if hasattr(self.tokens_used, "to_summary") else self.tokens_used,
                    "source": "shadow",
                }
                self.run_adapter.start_run(
                    run_id=self.run_id,
                    action="execute_task",
                    preset=self.preset_name,
                    branch=branch_name,
                    metadata=initial_meta,
                )
            except Exception as e:
                logger.warning(f"[RUN_SHADOW_ERROR] Errore start_run: {e}")

        tasks: List[Dict[str, Any]] = []

        try:
            # 1. Determinazione strategia: Meccanico vs Complesso
            if self.current_is_mechanical:
                logger.info("[MECHANICAL_TASK] Task deterministico/meccanico rilevato: bypass brainstorming e decomposizione Sol.")
                tasks = self.generate_mechanical_subtasks(task_description)
                if self.run_adapter:
                    self.run_adapter.update_progress(self.run_id, progress=35, current_step="decompose_mechanical")
            else:
                # 1. Brainstorm
                brainstorm_data = self.brainstorm(task_description)
                if self.run_adapter:
                    self.run_adapter.update_progress(self.run_id, progress=20, current_step="brainstorm")

                # 2. Decompose
                tasks = self.decompose(task_description, brainstorm_data)
                if self.run_adapter:
                    self.run_adapter.update_progress(self.run_id, progress=35, current_step="decompose")

            # 2.1 Preflight obbligatorio su tutti i subtask prima dell'esecuzione
            for idx, task in enumerate(tasks):
                t_id = task.get("id", f"T{idx+1}")
                allowed, blocker_msg = self.check_subtask_prerequisites(task)
                if not allowed:
                    logger.warning(f"[PREFLIGHT_BLOCKED] Subtask [{t_id}] bloccato da preflight: {blocker_msg}")
                    task["outcome"] = f"BLOCKED_PREREQUISITE: {blocker_msg}"
                    self.completed_tasks_history[t_id] = json.dumps({
                        "status": "BLOCKED_PREREQUISITE",
                        "blocker_reason": blocker_msg,
                        "blockers": [blocker_msg]
                    }, ensure_ascii=False)

                    # Marca i subtask successivi come saltati
                    for s_idx, skipped_task in enumerate(tasks[idx+1:], start=idx+2):
                        s_id = skipped_task.get("id", f"T{s_idx}")
                        skip_msg = f"Subtask precedente [{t_id}] bloccato per prerequisito mancante."
                        skipped_task["outcome"] = f"SKIPPED: {skip_msg}"
                        self.completed_tasks_history[s_id] = json.dumps({
                            "status": "SKIPPED",
                            "reason": skip_msg
                        }, ensure_ascii=False)

                    tokens_summary = self.tokens_used.to_summary() if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                    summary = {
                        "task": task_description,
                        "project": self.project_name or None,
                        "branch": branch_name,
                        "workspace": str(self.workspace),
                        "preset": self.preset_name,
                        "director": self.preset_config.get("director"),
                        "reviewers": self.preset_config.get("reviewers"),
                        "tasks_count": len(tasks),
                        "tokens_used": tokens_summary,
                        "status": "BLOCKED_PREREQUISITE",
                        "blocker_task_id": t_id,
                        "blocker_reason": blocker_msg,
                        "initial_commit": self.initial_commit,
                        "final_commit": self._get_current_head_commit(),
                        "html_diff": None,
                        "git_committed": False,
                        "subtasks": self._telegram_subtask_results(tasks),
                        "timestamp": datetime.now().isoformat()
                    }
                    if self._is_shadow_enabled():
                        summary["run_id"] = self.run_id

                    self.save_run_metrics(summary)
                    self.notify("workflow_blocked", f"⚠️ Workflow bloccato: {blocker_msg}", summary)
                    logger.warning(f"Orchestrazione bloccata per prerequisiti non soddisfatti: {blocker_msg}")

                    if self.run_adapter:
                        self.run_adapter.block_prerequisite_run(
                            self.run_id,
                            result=summary,
                            error=blocker_msg,
                            metadata={"tokens_used": tokens_summary, "tasks_count": len(tasks)}
                        )
                    return summary

            # 3. Execute all tasks (con supporto Resume)
            total_tasks = max(len(tasks), 1)
            for idx, task in enumerate(tasks):
                t_id = task.get("id")
                if self.run_adapter:
                    p_start = min(35 + int((idx / total_tasks) * 45), 80)
                    self.run_adapter.update_progress(self.run_id, progress=p_start, current_step=f"task_{t_id}")

                if self.resume and t_id in self.completed_task_ids:
                    logger.info(f"Resume: subtask [{t_id}] gia completato in precedenza. Salto esecuzione...")
                    continue

                try:
                    out = self.execute_task(task)
                except TimeBudgetExceededError as te:
                    time_msg = str(te)
                    logger.warning(f"Subtask [{t_id}] bloccato per superamento tempo (TIME_BUDGET_EXCEEDED): {time_msg}")
                    task["outcome"] = f"TIME_BUDGET_EXCEEDED: {time_msg}"
                    self.completed_tasks_history[t_id] = json.dumps({
                        "status": "TIME_BUDGET_EXCEEDED",
                        "blocker_reason": time_msg
                    }, ensure_ascii=False)

                    for s_idx, skipped_task in enumerate(tasks[idx+1:], start=idx+2):
                        s_id = skipped_task.get("id", f"T{s_idx}")
                        skip_msg = f"Subtask precedente [{t_id}] terminato per scadenza soft budget timeout (TIME_BUDGET_EXCEEDED)."
                        skipped_task["outcome"] = f"SKIPPED: {skip_msg}"
                        self.completed_tasks_history[s_id] = json.dumps({
                            "status": "SKIPPED",
                            "reason": skip_msg
                        }, ensure_ascii=False)

                    tokens_summary = self.tokens_used.to_summary(strict_guarantee=self.strict_token_budget) if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                    summary = {
                        "task": task_description,
                        "project": self.project_name or None,
                        "branch": branch_name,
                        "workspace": str(self.workspace),
                        "preset": self.preset_name,
                        "director": self.preset_config.get("director"),
                        "reviewers": self.preset_config.get("reviewers"),
                        "tasks_count": len(tasks),
                        "tokens_used": tokens_summary,
                        "status": "TIME_BUDGET_EXCEEDED",
                        "blocker_task_id": t_id,
                        "blocker_reason": time_msg,
                        "initial_commit": self.initial_commit,
                        "final_commit": self._get_current_head_commit(),
                        "html_diff": None,
                        "git_committed": False,
                        "subtasks": self._telegram_subtask_results(tasks),
                        "timestamp": datetime.now().isoformat()
                    }
                    if self._is_shadow_enabled():
                        summary["run_id"] = self.run_id

                    self.save_run_metrics(summary)
                    notif_msg = format_workflow_telegram_message("Workflow terminato (Tempo Limite)", summary, is_strict=self.strict_token_budget)
                    self.notify("workflow_blocked", notif_msg, summary)

                    if self.run_adapter:
                        self.run_adapter.fail_run(
                            self.run_id,
                            error=time_msg,
                            metadata={"tokens_used": tokens_summary, "tasks_count": len(tasks), "status": "TIME_BUDGET_EXCEEDED"}
                        )
                    return summary

                except BudgetExceededError as be:
                    budget_msg = str(be)
                    logger.warning(f"Subtask [{t_id}] bloccato per superamento budget: {budget_msg}")
                    task["outcome"] = f"BLOCKED_BUDGET: {budget_msg}"
                    self.completed_tasks_history[t_id] = json.dumps({
                        "status": "BLOCKED_BUDGET",
                        "blocker_reason": budget_msg
                    }, ensure_ascii=False)

                    for s_idx, skipped_task in enumerate(tasks[idx+1:], start=idx+2):
                        s_id = skipped_task.get("id", f"T{s_idx}")
                        skip_msg = f"Subtask precedente [{t_id}] bloccato per budget esaurito."
                        skipped_task["outcome"] = f"SKIPPED: {skip_msg}"
                        self.completed_tasks_history[s_id] = json.dumps({
                            "status": "SKIPPED",
                            "reason": skip_msg
                        }, ensure_ascii=False)

                    tokens_summary = self.tokens_used.to_summary(strict_guarantee=self.strict_token_budget) if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                    summary = {
                        "task": task_description,
                        "project": self.project_name or None,
                        "branch": branch_name,
                        "workspace": str(self.workspace),
                        "preset": self.preset_name,
                        "director": self.preset_config.get("director"),
                        "reviewers": self.preset_config.get("reviewers"),
                        "tasks_count": len(tasks),
                        "tokens_used": tokens_summary,
                        "status": "BLOCKED_BUDGET",
                        "blocker_task_id": t_id,
                        "blocker_reason": budget_msg,
                        "initial_commit": self.initial_commit,
                        "final_commit": self._get_current_head_commit(),
                        "html_diff": None,
                        "git_committed": False,
                        "subtasks": self._telegram_subtask_results(tasks),
                        "timestamp": datetime.now().isoformat()
                    }
                    if self._is_shadow_enabled():
                        summary["run_id"] = self.run_id

                    self.save_run_metrics(summary)
                    notif_msg = format_workflow_telegram_message("Workflow bloccato (Budget)", summary, is_strict=self.strict_token_budget)
                    self.notify("workflow_blocked", notif_msg, summary)

                    if self.run_adapter:
                        self.run_adapter.block_budget_run(
                            self.run_id,
                            result=summary,
                            error=budget_msg,
                            metadata={"tokens_used": tokens_summary, "tasks_count": len(tasks)}
                        )
                    return summary

                self.completed_task_ids.append(t_id)
                self.completed_tasks_history[t_id] = out

                if self.run_adapter:
                    p_end = min(35 + int(((idx + 1) / total_tasks) * 45), 80)
                    self.run_adapter.update_progress(self.run_id, progress=p_end, current_step=f"task_{t_id}_completed")

                # Se durante l'esecuzione compare BLOCKED_PREREQUISITE o errore/fallimento/timeout, interrompi immediatamente la catena
                out_dict = {}
                try:
                    out_dict = json.loads(out) if isinstance(out, str) else (out if isinstance(out, dict) else {})
                except Exception:
                    pass

                out_status = str(out_dict.get("status", "")).upper() if isinstance(out_dict, dict) else ""
                is_timeout = "TIME_BUDGET_EXCEEDED" in str(out) or out_status == "TIME_BUDGET_EXCEEDED"
                is_blocked = "BLOCKED_PREREQUISITE" in str(out) or out_status == "BLOCKED_PREREQUISITE"
                is_failed = out_status in ["ERROR", "FAILED"]

                if is_timeout or is_blocked or is_failed:
                    term_status = "TIME_BUDGET_EXCEEDED" if is_timeout else ("BLOCKED_PREREQUISITE" if is_blocked else "FAILED")
                    err_msg = (
                        out_dict.get("blocker_reason") or
                        out_dict.get("error") or
                        f"Subtask [{t_id}] non completato con successo ({term_status})."
                    )
                    logger.warning(f"Subtask [{t_id}] ha restituito {term_status} a runtime: arresto catena subtask.")
                    task["outcome"] = f"{term_status}: {err_msg}"

                    # Marca tutti i subtask successivi come SKIPPED
                    for s_idx, skipped_task in enumerate(tasks[idx+1:], start=idx+2):
                        s_id = skipped_task.get("id", f"T{s_idx}")
                        skip_msg = f"Subtask precedente [{t_id}] non riuscito ({term_status})."
                        skipped_task["outcome"] = f"SKIPPED: {skip_msg}"
                        self.completed_tasks_history[s_id] = json.dumps({
                            "status": "SKIPPED",
                            "reason": skip_msg
                        }, ensure_ascii=False)

                    tokens_summary = self.tokens_used.to_summary(strict_guarantee=self.strict_token_budget) if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                    summary = {
                        "task": task_description,
                        "project": self.project_name or None,
                        "branch": branch_name,
                        "workspace": str(self.workspace),
                        "preset": self.preset_name,
                        "director": self.preset_config.get("director"),
                        "reviewers": self.preset_config.get("reviewers"),
                        "tasks_count": len(tasks),
                        "tokens_used": tokens_summary,
                        "status": term_status,
                        "blocker_task_id": t_id,
                        "blocker_reason": err_msg,
                        "initial_commit": self.initial_commit,
                        "final_commit": self._get_current_head_commit(),
                        "html_diff": None,
                        "git_committed": False,
                        "subtasks": self._telegram_subtask_results(tasks),
                        "timestamp": datetime.now().isoformat()
                    }
                    if self._is_shadow_enabled():
                        summary["run_id"] = self.run_id

                    self.save_run_metrics(summary)
                    notif_msg = format_workflow_telegram_message(f"Workflow interrotto ({term_status})", summary, is_strict=self.strict_token_budget)
                    self.notify("workflow_blocked" if (is_blocked or is_timeout) else "workflow_failed", notif_msg, summary)

                    if self.run_adapter:
                        if is_blocked:
                            self.run_adapter.block_prerequisite_run(
                                self.run_id,
                                result=summary,
                                error=err_msg,
                                metadata={"tokens_used": tokens_summary, "tasks_count": len(tasks)}
                            )
                        else:
                            self.run_adapter.fail_run(
                                self.run_id,
                                error=err_msg,
                                metadata={"tokens_used": tokens_summary, "tasks_count": len(tasks), "status": term_status}
                            )
                    return summary

            # 4. Final Validation
            if self.run_adapter:
                self.run_adapter.update_progress(self.run_id, progress=85, current_step="validate")

            # Se task meccanico completato con successo, skip validazione pesante Sol (0 LLM)
            if getattr(self, "current_is_mechanical", False) or self.is_mechanical_task(task_description) or self.preset_name == "mechanical":
                val_res = {"status": "done", "summary": "Task meccanico completato con successo (validazione automatica, 0 LLM)."}
            else:
                val_res = self.validate(tasks)
            logger.info(f"Esito validazione finale: {val_res.get('status', 'done')}")

            # 5. Visual Diff Review Generation & Dirty Detection / Read-Only Invariance Check
            diff_data = self.diff_manager.get_diff_data(self.workspace)
            html_diff_path = None
            has_changes = diff_data.get("has_changes", False) or GitWorktreeManager.is_worktree_dirty(self.workspace)

            if self.read_only:
                if self.workspace and (self.workspace / ".git").exists():
                    valid_post, _, current_status = check_read_only_invariance(self.workspace)
                    if current_status != initial_git_status:
                        diff_files = [line[3:].strip() for line in current_status.splitlines() if line.strip()]
                        viol_msg = f"ERROR_READ_ONLY_INVARIANCE_VIOLATION: Rilevate modifiche non autorizzate in sola lettura sul repository: {', '.join(diff_files) if diff_files else 'modifiche rilevate'}"
                        logger.error(viol_msg)
                        tokens_summary = self.tokens_used.to_summary() if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                        summary = {
                            "task": task_description,
                            "project": self.project_name or None,
                            "branch": branch_name,
                            "workspace": str(self.workspace),
                            "preset": self.preset_name,
                            "director": self.preset_config.get("director"),
                            "reviewers": self.preset_config.get("reviewers"),
                            "tasks_count": len(tasks),
                            "tokens_used": tokens_summary,
                            "status": "FAILED",
                            "blocker_task_id": "T?",
                            "blocker_reason": viol_msg,
                            "initial_commit": self.initial_commit,
                            "final_commit": self._get_current_head_commit(),
                            "html_diff": None,
                            "git_committed": False,
                            "subtasks": self._telegram_subtask_results(tasks),
                            "timestamp": datetime.now().isoformat()
                        }
                        if self._is_shadow_enabled():
                            summary["run_id"] = self.run_id
                        self.save_run_metrics(summary)
                        self.notify("workflow_failed", f"⚠️ {viol_msg}", summary)
                        if self.run_adapter:
                            self.run_adapter.fail_run(
                                self.run_id,
                                error=viol_msg,
                                metadata={"tokens_used": tokens_summary, "tasks_count": len(tasks)}
                            )
                        return summary
                # In modalità sola lettura pulita, nessuna modifica da approvare o committare
                has_changes = False

            if has_changes:
                html_diff_path = self.diff_manager.generate_html_diff(
                    self.workspace,
                    task_description=task_description,
                    branch_name=branch_name
                )
                # Notifica sintesi diff per Telegram
                diff_msg = self.diff_manager.format_telegram_summary(diff_data, task_description, branch_name)
                self.notify("diff_review_generated", diff_msg, {
                    "branch": branch_name,
                    "html_diff_path": str(html_diff_path) if html_diff_path else None,
                    "insertions": diff_data.get("insertions", 0),
                    "deletions": diff_data.get("deletions", 0),
                    "files_count": len(diff_data.get("files_changed", []))
                })

            # 6. Gestione Approval & Preservazione Worktree
            # Se ci sono modifiche e non è stato concesso un push/commit esplicito approvato:
            if has_changes and (self.require_approval or not do_push):
                logger.info(f"Modifiche rilevate nel worktree {self.workspace}: worktree preservato in attesa di approvazione esplicita.")
                state_data = {
                    "task": task_description,
                    "branch": branch_name,
                    "workspace": str(self.workspace),
                    "html_diff_path": str(html_diff_path) if html_diff_path else None,
                    "timestamp": datetime.now().isoformat(),
                    "status": "WAITING_FOR_APPROVAL"
                }
                if self._is_shadow_enabled():
                    state_data["run_id"] = self.run_id

                try:
                    self.state_dir.mkdir(parents=True, exist_ok=True)
                    pending_file = self.state_dir / f"pending_{branch_name.replace('/', '_')}.json"
                    pending_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Impossibile salvare pending file in {self.state_dir}: {e}")

                self.notify("approval_required", "⚠️ Modifiche pronte per la review. Worktree preservato in attesa di approvazione.", state_data)

                tokens_summary = self.tokens_used.to_summary() if hasattr(self.tokens_used, "to_summary") else self.tokens_used
                approval_res = {
                    "task": task_description,
                    "branch": branch_name,
                    "workspace": str(self.workspace),
                    "preset": self.preset_name,
                    "status": "WAITING_FOR_APPROVAL",
                    "html_diff": str(html_diff_path) if html_diff_path else None,
                    "diff_stat": diff_data.get("stat"),
                    "files_changed": diff_data.get("files_changed", []),
                    "tokens_used": tokens_summary,
                    "initial_commit": self.initial_commit,
                    "final_commit": self._get_current_head_commit(),
                    "subtasks": self._telegram_subtask_results(tasks)
                }
                if self._is_shadow_enabled():
                    approval_res["run_id"] = self.run_id

                if self.run_adapter:
                    self.run_adapter.waiting_approval_run(
                        self.run_id,
                        result=approval_res,
                        metadata={
                            "task": task_description,
                            "branch": branch_name,
                            "workspace": str(self.workspace),
                            "tokens_used": tokens_summary,
                            "diff_stat": diff_data.get("stat"),
                            "files_changed": diff_data.get("files_changed", []),
                            "initial_commit": self.initial_commit,
                            "final_commit": self._get_current_head_commit()
                        }
                    )

                return approval_res

            # 7. Git Commit & Push (solo se esplicitamente approvato con do_push e non in mock mode)
            git_committed = False
            if do_push and not self.mock_mode and not self.read_only:
                logger.info("Salvataggio modifiche approvate in Git...")
                try:
                    files_to_commit = diff_data.get("files_changed", [])
                    if files_to_commit:
                        # Aggiunge solo la lista esplicita dei file revisionati (MAI 'git add .')
                        for f_rel in files_to_commit:
                            clean_f = str(f_rel).strip()
                            if clean_f:
                                self.run_cmd(["git", "add", "--", clean_f])
                        commit_msg = f"feat(taktstock): {task_description[:80]}"
                        self.run_cmd(["git", "commit", "-m", commit_msg])
                        push_res = self.run_cmd(["git", "push", "origin", branch_name])
                        logger.info(f"Git push completato: {push_res}")
                        git_committed = True
                    else:
                        logger.info("Nessun file revisionato da committare in Git.")
                except Exception as e:
                    logger.error(f"Errore durante git operations: {e}")

            tokens_summary = self.tokens_used.to_summary(strict_guarantee=self.strict_token_budget) if hasattr(self.tokens_used, "to_summary") else self.tokens_used
            summary = {
                "task": task_description,
                "project": self.project_name or None,
                "branch": branch_name,
                "workspace": str(self.workspace),
                "preset": self.preset_name,
                "director": self.preset_config.get("director"),
                "reviewers": self.preset_config.get("reviewers"),
                "tasks_count": len(tasks),
                "tokens_used": tokens_summary,
                "status": "COMPLETED",
                "html_diff": str(html_diff_path) if html_diff_path else None,
                "git_committed": git_committed,
                "initial_commit": self.initial_commit,
                "final_commit": self._get_current_head_commit(),
                "subtasks": self._telegram_subtask_results(tasks),
                "timestamp": datetime.now().isoformat()
            }
            if self._is_shadow_enabled():
                summary["run_id"] = self.run_id

            self.save_run_metrics(summary)
            notif_msg = format_workflow_telegram_message("Taktstock ha completato il lavoro!", summary, is_strict=self.strict_token_budget)
            self.notify("workflow_completed", notif_msg, summary)
            logger.info("Orchestrazione completata con successo!")

            if self.run_adapter:
                self.run_adapter.complete_run(
                    self.run_id,
                    result=summary,
                    metadata={"tokens_used": tokens_summary, "tasks_count": len(tasks)}
                )

            return summary

        except Exception as e:
            if self.run_adapter:
                try:
                    self.run_adapter.fail_run(
                        self.run_id,
                        error=str(e),
                        metadata={"tokens_used": self.tokens_used, "branch": branch_name}
                    )
                except Exception:
                    pass
            raise


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Taktstock Multi-Agent Runner")
    parser.add_argument("task", nargs="?", help="Descrizione del task da realizzare", default="")
    parser.add_argument("--task-b64", help="Descrizione del task codificata in Base64 (immune a command injection)", default="")
    parser.add_argument("--task-env", help="Nome della variabile d'ambiente da cui leggere il task (default: UF_TASK)", default="")
    parser.add_argument("--repo", help="URL del repository Git (HTTPS o SSH)", default="")
    parser.add_argument("--repo-b64", help="URL del repository Git codificata in Base64", default="")
    parser.add_argument("--branch", help="Nome del branch Git", default="")
    parser.add_argument("--base-branch", help="Branch di partenza per il worktree (default: main)", default="main")
    parser.add_argument("--no-worktree", action="store_true", help="Disabilita l'isolamento Git Worktree (usa cartella repository diretta)")
    parser.add_argument("--read-only", "--readonly", dest="read_only", action="store_true", help="Esegui in modalità sola lettura senza creare branch o worktree")
    parser.add_argument("--strict-token-budget", action="store_true", help="Richiede budget token rigorosamente garantito dal provider; blocca AGY se non supporta max-tokens")
    parser.add_argument("--audit-timeout-sec", type=int, default=45, help="Timeout in secondi per task di audit/read-only (soft budget, default: 45)")
    parser.add_argument("--keep-worktree", action="store_true", help="Mantieni il worktree su disco anche al termine dell'esecuzione")
    parser.add_argument("--resume", action="store_true", help="Riprendi l'esecuzione dal checkpoint salvato saltando i subtask gia completati")
    parser.add_argument("--webhook", help="URL Webhook n8n per notifiche", default="")
    parser.add_argument("--chat-id", help="Chat ID Telegram per notifiche dirette", default="")
    parser.add_argument("--push", action="store_true", default=False, help="Effettua git push delle modifiche approvate (default: disabilitato)")
    parser.add_argument("--no-push", action="store_true", help="Disabilita esplicitamente git push")
    parser.add_argument("--require-approval", action="store_true", help="Attendi approvazione visiva del diff prima del push")
    parser.add_argument("--dry-run", "--mock", dest="dry_run", action="store_true", help="Esegui in modalita simulazione/mock")
    parser.add_argument("--max-fix-attempts", type=int, default=3, help="Numero massimo tentativi di fix per subtask (default: 3)")
    parser.add_argument("--preset", choices=sorted(PRESETS.keys()), default="standard",
                        help="Preset agenti: standard (Sol + agy + Luna n8n), critical (Sol + DS-Pro + GLM), light o luna_flash")
    parser.add_argument("--action", help="Azione meccanica specifica (es: sync-n8n-mirror, complete-handoff-mirror-sync)", default="")
    parser.add_argument("--director", choices=["director", "terra", "sol"], help="Override del Direttore (terra o sol)")
    parser.add_argument("--reviewers", help="Lista revisori: solo 'ds-pro' oppure 'none'.")
    
    # Parametri Brainstorming & Buzz Chat
    parser.add_argument("--brainstorm", nargs="?", const="", help="Avvia una sessione di Brainstorming interattiva con Sol e i Reviewer")
    parser.add_argument("--brainstorm-continue", help="Continua una sessione di Brainstorming esistente passando l'ID")
    parser.add_argument("--brainstorm-chat", help="Invia un messaggio alla chatroom multi-agent stile Buzz con mention (@sol, @deepseek, @glm, @agy, @all)")
    parser.add_argument("--chat-msg", help="Alias per messaggio di chat")
    parser.add_argument("--chat-clear", action="store_true", help="Azzera la memoria attiva della chatroom")
    parser.add_argument("--msg-b64", help="Messaggio di chat codificato in Base64", default="")
    parser.add_argument("--feedback", help="Feedback utente per il round successivo di Brainstorming", default="")
    parser.add_argument("--feedback-b64", help="Feedback utente codificato in Base64", default="")
    parser.add_argument("--ask-reviewers", action="store_true", help="Richiedi una nuova opinione tecnica ai Reviewer nel Brainstorming")
    parser.add_argument("--brainstorm-approve", help="Approva la sessione di Brainstorming ed avvia l'orchestrazione")
    parser.add_argument("--brainstorm-reject", help="Rifiuta la sessione di Brainstorming specificando l'ID")
    parser.add_argument("--reason", help="Motivo del rifiuto del piano", default="")
    parser.add_argument("--reason-b64", help="Motivo del rifiuto del piano codificato in Base64", default="")
    parser.add_argument("--brainstorm-status", help="Mostra lo stato e il riassunto di una sessione di Brainstorming")

    # Parametri Design Mode
    parser.add_argument("--design-url", help="URL target frontend per cattura screenshot e DOM (Design Mode)", default="")
    parser.add_argument("--design-selector", help="Selettore CSS target per Design Mode (opzionale)", default="")
    return parser

def parse_args(args=None):
    return create_parser().parse_args(args)

def should_preserve_worktree(
    workspace_path: Optional[Path],
    result: Optional[Dict[str, Any]] = None,
    keep_requested: bool = False
) -> Tuple[bool, str]:
    """
    Determina in modo sicuro e deterministico se un worktree deve essere preservato.
    Regole di conservazione (fail-safe):
    1. Se keep_requested è True (o env TAKTSTOCK_KEEP_WORKTREE==1 / UFFICIO_KEEP_WORKTREE==1) -> PRESERVARE
    2. Se result['status'] == 'WAITING_FOR_APPROVAL' -> PRESERVARE
    3. Se il worktree è dirty (tracked, staged o untracked) o lo stato Git è non determinabile -> PRESERVARE
    4. Solo se il worktree è confermato pulito (clean) e non in attesa approvazione -> CANCELLARE

    Ritorna una tupla (should_preserve: bool, reason: str).
    """
    if keep_requested or (os.environ.get("TAKTSTOCK_KEEP_WORKTREE") or os.environ.get("UFFICIO_KEEP_WORKTREE", "0")) == "1":
        return True, "Keep worktree richiesto esplicitamente"

    if isinstance(result, dict) and result.get("status") == "WAITING_FOR_APPROVAL":
        return True, "Stato WAITING_FOR_APPROVAL"

    if workspace_path:
        p = Path(workspace_path)
        if p.exists():
            if GitWorktreeManager.is_worktree_dirty(p):
                return True, "Modifiche non committate o stato non determinabile (fail-safe)"

    return False, "Worktree pulito e nessun blocco approvazione"

def main():
    parser = create_parser()
    args = parser.parse_args()

    # Decodifica parametri Base64 (protezione da command injection lato shell n8n)
    if args.repo_b64:
        try:
            args.repo = base64.b64decode(args.repo_b64).decode("utf-8")
        except Exception:
            pass

    # Inizializzazione manager ausiliari
    bm = BrainstormManager()
    pm = ProjectsManager()
    account_mgr = CodexAccountManager()
    diff_mgr = DiffReviewManager(DIFFS_DIR)

    # Risoluzione automatica di repository locali dal catalogo progetti
    if args.repo:
        found_p = pm.find_project(args.repo)
        if found_p:
            args.repo = found_p["path"]

    # Parsing reviewers override
    reviewers_override = None
    if args.reviewers:
        if args.reviewers.strip().lower() == "none":
            reviewers_override = []
        else:
            reviewers_override = [r.strip() for r in args.reviewers.split(",") if r.strip()]

    # Runner di supporto
    workspace = WORKSPACES_DIR / "default"
    runner = MultiAgentRunner(
        workspace_path=workspace,
        webhook_url=args.webhook,
        chat_id=args.chat_id,
        preset=args.preset,
        director_override=args.director,
        reviewers_override=reviewers_override,
        mock_mode=args.dry_run,
        max_fix_attempts=args.max_fix_attempts,
        account_manager=account_mgr,
        diff_manager=diff_mgr,
        require_approval=args.require_approval,
        mechanical_action=args.action or None,
        read_only=args.read_only,
        strict_token_budget=args.strict_token_budget,
        audit_timeout_sec=args.audit_timeout_sec
    )

    # -------------------------------------------------------------
    # 🧠 GESTIONE MODALITÀ BRAINSTORMING & BUZZ CHAT
    # -------------------------------------------------------------

    # 1. Chat Interattiva Buzz Style (@sol, @deepseek, @glm, @agy, @all)
    chat_input = args.brainstorm_chat or args.chat_msg
    if args.msg_b64:
        try:
            chat_input = base64.b64decode(args.msg_b64).decode("utf-8")
        except Exception:
            pass

    if args.chat_clear:
        new_bs_id = bm.clear_active_session(args.chat_id)
        clear_msg = (
            "🧹 *Memoria chat azzerata con successo!*\n\n"
            "Da questo momento tutti gli agenti hanno il contesto pulito per il nuovo argomento.\n\n"
            "👉 Con chi vuoi iniziare?\n"
            "- /sol (Strategia & Direzione)\n"
            "- /luna (UX & Prodotto)\n"
            "- /pro (Architettura & Schema Dati)\n"
            "- /flash (Review Rapida & Performance)\n"
            "- /glm (GLM 5.3 Flash: frontend/UI e contratti)\n"
            "- /agy (Sviluppo Codice)\n"
            "- /all (Riunione Team)"
        )
        runner.notify("chat_cleared", clear_msg, {"brainstorm_id": new_bs_id})
        print(json.dumps({"status": "CLEARED", "brainstorm_id": new_bs_id}, indent=2))
        return

    if chat_input:
        bs_id = args.brainstorm_continue
        if bs_id == "active" or not bs_id:
            bs_id = bm.get_active_brainstorm_id(args.chat_id)
        if not bs_id or not bm.load_brainstorm(bs_id):
            # Se non esiste ancora una sessione attiva o il file non esiste, creala automaticamente
            bs_id = bm.create_brainstorm(task=chat_input, preset=args.preset, chat_id=args.chat_id, repo=args.repo)

        try:
            replies = bm.post_chat_message(bs_id, message=chat_input, sender="User", runner=runner)
        except Exception as e:
            logger.warning(f"Errore durante l'elaborazione del messaggio chat: {e}")
            runner.notify("brainstorm_error", f"⚠️ Errore chat: {str(e)}")
            print(json.dumps({"status": "ERROR", "error": str(e)}), file=sys.stderr)
            sys.exit(0)

        # Invia un messaggio Telegram dedicato per ciascun agente che risponde (evita superamento limite caratteri)
        from brainstorm_manager import AGENT_META
        for r in replies:
            agent_key = r.get("agent", "sol")
            meta = AGENT_META.get(agent_key, {"name": r.get("sender", "Agente"), "avatar": "🤖"})
            single_msg = f"{meta['avatar']} *{meta['name'].upper()}:*\n{r.get('text', '')}"
            runner.notify("brainstorm_chat_reply", single_msg, {"reply": r, "brainstorm_id": bs_id})
        print(json.dumps({"brainstorm_id": bs_id, "replies": replies}, indent=2, ensure_ascii=False))
        return

    # 2. Start Brainstorm
    if args.brainstorm is not None:
        task_desc = args.brainstorm or args.task
        if args.task_b64:
            try:
                task_desc = base64.b64decode(args.task_b64).decode("utf-8")
            except Exception:
                pass
        elif not task_desc:
            task_desc = os.environ.get("UF_TASK", "Task non specificato")

        preset_choice = args.preset
        runner.preset_name = preset_choice
        runner.preset_config = PRESETS.get(preset_choice, PRESETS["critical"]).copy()
        
        bs_id = bm.create_brainstorm(
            task=task_desc,
            preset=preset_choice,
            chat_id=args.chat_id,
            repo=args.repo
        )
        bs_data = bm.run_round(bs_id, user_feedback=None, runner=runner)
        summary = bm.format_telegram_summary(bs_data)
        runner.notify("brainstorm_started", summary, bs_data)
        print(json.dumps(bs_data, indent=2))
        return

    # 3. Continue Brainstorm con Feedback o Richiesta Reviewers
    if args.brainstorm_continue or args.ask_reviewers:
        bs_id = args.brainstorm_continue
        if bs_id == "active" or not bs_id:
            bs_id = bm.get_active_brainstorm_id(args.chat_id)
        if not bs_id or not bm.load_brainstorm(bs_id):
            logger.warning("Nessuna sessione di Brainstorming attiva trovata.")
            runner.notify("brainstorm_error", "⚠️ Nessuna sessione di Brainstorming attiva trovata.")
            print(json.dumps({"status": "ERROR", "error": "Nessuna sessione attiva trovata."}))
            sys.exit(0)

        user_fb = args.feedback
        if args.feedback_b64:
            try:
                user_fb = base64.b64decode(args.feedback_b64).decode("utf-8")
            except Exception:
                pass
        if args.ask_reviewers and not user_fb:
            user_fb = "Richiesta riesame approfondito delle alternative e degli edge cases da parte del team dei Reviewer."

        try:
            updated_bs = bm.run_round(bs_id, user_feedback=user_fb, runner=runner)
        except Exception as e:
            logger.warning(f"Errore esecuzione round brainstorm {bs_id}: {e}")
            runner.notify("brainstorm_error", f"⚠️ Errore sessione brainstorming: {str(e)}")
            print(json.dumps({"status": "ERROR", "error": str(e)}))
            sys.exit(0)

        if not updated_bs:
            sys.exit(0)

        summary = bm.format_telegram_summary(updated_bs)
        runner.notify("brainstorm_round_completed", summary, updated_bs)
        print(json.dumps(updated_bs, indent=2))
        return

    # 4. Brainstorm Status
    if args.brainstorm_status:
        bs_id = args.brainstorm_status
        if bs_id == "active" or not bs_id:
            bs_id = bm.get_active_brainstorm_id(args.chat_id)
        if not bs_id or not bm.load_brainstorm(bs_id):
            logger.warning("Nessuna sessione di Brainstorming attiva trovata.")
            print(json.dumps({"status": "ERROR", "error": "Nessuna sessione attiva trovata."}))
            sys.exit(0)
        bs_data = bm.get_status(bs_id)
        if "error" in bs_data:
            logger.warning(f"Brainstorm {bs_id} non trovato.")
            print(json.dumps({"status": "ERROR", "error": f"Brainstorm {bs_id} non trovato."}))
            sys.exit(0)
        summary = bm.format_telegram_summary(bs_data)
        runner.notify("brainstorm_status", summary, bs_data)
        print(json.dumps(bs_data, indent=2))
        return

    # 5. Brainstorm Reject
    if args.brainstorm_reject:
        bs_id = args.brainstorm_reject
        if bs_id == "active" or not bs_id:
            bs_id = bm.get_active_brainstorm_id(args.chat_id)
        if not bs_id or not bm.load_brainstorm(bs_id):
            logger.warning("Nessuna sessione di Brainstorming attiva trovata per il rifiuto.")
            print(json.dumps({"status": "ERROR", "error": "Nessuna sessione attiva trovata."}))
            sys.exit(0)

        reason = args.reason
        if args.reason_b64:
            try:
                reason = base64.b64decode(args.reason_b64).decode("utf-8")
            except Exception:
                pass
        bm.reject(bs_id, reason=reason or "Rifiutato dall'utente")
        rejected_bs = bm.get_status(bs_id)
        summary = bm.format_telegram_summary(rejected_bs)
        runner.notify("brainstorm_rejected", summary, rejected_bs)
        print(json.dumps(rejected_bs, indent=2))
        return

    # 6. Approvazione Piano ed esecuzione a valle
    if args.brainstorm_approve:
        bs_id = args.brainstorm_approve
        if bs_id == "active":
            bs_id = bm.get_active_brainstorm_id(args.chat_id)
        if not bs_id or not bm.load_brainstorm(bs_id):
            logger.warning("Nessuna sessione di Brainstorming attiva trovata per l'approvazione.")
            print(json.dumps({"status": "ERROR", "error": "Nessuna sessione attiva trovata."}))
            sys.exit(0)
        approved_bs = bm.approve(bs_id, approved_by=args.chat_id or "user")
        if not approved_bs:
            logger.error(f"Impossibile approvare brainstorm {bs_id}.")
            sys.exit(0)

        runner.notify("brainstorm_approved", f"🚀 Brainstorming `#{bs_id}` approvato! Avvio esecuzione...", approved_bs)
        task_desc = bm.to_execution_task(bs_id)
        args.repo = args.repo or approved_bs.get("repo") or ""
        args.preset = approved_bs.get("preset") or args.preset

    # -------------------------------------------------------------
    # ⚙️ GESTIONE ESECUZIONE STANDARD (O POST-BRAINSTORM)
    # -------------------------------------------------------------

    # Risoluzione sicura del task
    if not args.brainstorm_approve:
        task_desc = args.task
        if args.task_b64:
            try:
                task_desc = base64.b64decode(args.task_b64).decode("utf-8")
            except Exception as e:
                logger.error(f"Errore decodifica task base64: {e}")
        elif args.task_env:
            task_desc = os.environ.get(args.task_env, task_desc)
        elif not task_desc:
            task_desc = os.environ.get("UF_TASK", "Task non specificato")

    if not task_desc.strip():
        logger.error("Nessun task specificato.")
        sys.exit(1)

    # Design Mode Capture
    design_ctx_str = ""
    if args.design_url:
        logger.info(f"Avvio Design Mode per URL: {args.design_url} (Selector: {args.design_selector or 'body'})...")
        d_cap = DesignCapture(DESIGN_DIR)
        cap_res = d_cap.capture_url(args.design_url, selector=args.design_selector)
        design_ctx_str = d_cap.format_prompt_context(cap_res)

    # Workspace & Worktree setup
    if not args.repo:
        active_bs_id = bm.get_active_brainstorm_id(args.chat_id)
        if active_bs_id:
            active_bs = bm.load_brainstorm(active_bs_id)
            if active_bs and active_bs.get("repo"):
                args.repo = active_bs.get("repo")
                logger.info(f"Ereditato repository dal brainstorm attivo: {args.repo}")

    if args.repo:
        found_p = pm.find_project(args.repo)
        if found_p:
            args.repo = found_p["path"]

    is_read_only = is_explicit_read_only_task(task_desc, args)
    branch = args.branch or (f"feature/taktstock-{int(datetime.now().timestamp())}" if not is_read_only else "")
    workspace = WORKSPACES_DIR / "default"
    wt_manager = None
    repo_master_path = None

    if args.repo and not args.dry_run:
        wt_manager = GitWorktreeManager(REPOS_DIR, WORKTREES_DIR)
        repo_master_path = wt_manager.setup_main_repo(args.repo)
        
        # Preflight Git non distruttivo sul repository selezionato
        is_sync_ok, sync_err, init_c = check_git_preflight_sync(repo_master_path, branch=args.base_branch)
        if not is_sync_ok:
            logger.error(f"[GIT_PREFLIGHT_BLOCKED] {sync_err}")
            runner.notify("workflow_blocked", f"⚠️ {sync_err}", {"error": sync_err, "initial_commit": init_c})
            print(json.dumps({
                "task": task_desc,
                "status": "BLOCKED_GIT_SYNC",
                "blocker_reason": sync_err,
                "initial_commit": init_c,
                "final_commit": init_c,
                "tokens_used": "not_measured"
            }, indent=2))
            sys.exit(1)

        if is_read_only:
            # Modalità Sola Lettura: NESSUN worktree, NESSUN branch temporaneo
            valid, inv_err, _ = check_read_only_invariance(repo_master_path)
            if not valid:
                err_msg = f"BLOCKED_READ_ONLY_INVARIANCE: {inv_err}"
                logger.error(err_msg)
                runner.notify("workflow_blocked", f"⚠️ {err_msg}", {"error": err_msg})
                print(json.dumps({
                    "task": task_desc,
                    "status": "BLOCKED_PREREQUISITE",
                    "blocker_reason": err_msg,
                    "tokens_used": "not_measured"
                }, indent=2))
                sys.exit(1)
            workspace = repo_master_path
            logger.info(f"Modalità sola lettura attiva per {workspace}: nessun branch o worktree creato.")
        elif not args.no_worktree:
            # Isolamento Worktree
            workspace = wt_manager.create_worktree(
                repo_path=repo_master_path,
                branch_name=branch,
                base_branch=args.base_branch
            )
        else:
            # Fallback a repository diretto
            repo_name = wt_manager.get_repo_name(args.repo)
            workspace = WORKSPACES_DIR / repo_name
            if not workspace.exists():
                subprocess.run(["git", "clone", args.repo, str(workspace)], check=True)
            subprocess.run(["git", "checkout", "-B", branch], cwd=str(workspace), check=True)

    runner = MultiAgentRunner(
        workspace_path=workspace,
        webhook_url=args.webhook,
        chat_id=args.chat_id,
        preset=args.preset,
        director_override=args.director,
        reviewers_override=reviewers_override,
        mock_mode=args.dry_run,
        max_fix_attempts=args.max_fix_attempts,
        account_manager=account_mgr,
        diff_manager=diff_mgr,
        design_context=design_ctx_str,
        require_approval=args.require_approval,
        branch_name=branch,
        resume=args.resume,
        project_name=Path(args.repo).name if args.repo else None,
        mechanical_action=args.action or None,
        read_only=is_read_only,
        strict_token_budget=args.strict_token_budget,
        audit_timeout_sec=args.audit_timeout_sec
    )

    try:
        do_push = bool(args.push and not args.no_push and args.repo and not args.dry_run and not is_read_only)
        result = runner.run_full_workflow(
            task_description=task_desc,
            branch_name=branch,
            do_push=do_push
        )
        if args.brainstorm_approve:
            bm.mark_executed(bs_id)
        print(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Errore critico durante il workflow: {e}")
        runner.notify("workflow_failed", f"Errore critico durante l'esecuzione: {str(e)}", {"error": str(e)})
        raise e
    finally:
        # Cleanup condizionale del worktree: preservazione fail-safe
        res_obj = result if ('result' in locals() and isinstance(result, dict)) else None
        keep_req = bool((os.environ.get("TAKTSTOCK_KEEP_WORKTREE") or os.environ.get("UFFICIO_KEEP_WORKTREE", "0")) == "1" or getattr(args, "keep_worktree", False))
        should_preserve, reason = should_preserve_worktree(
            workspace_path=workspace,
            result=res_obj,
            keep_requested=keep_req
        )

        if wt_manager and repo_master_path and workspace and not args.dry_run and not args.no_worktree and not is_read_only:
            if should_preserve:
                logger.info(f"📌 Worktree preservato in {workspace} (Motivo: {reason})")
            else:
                logger.info(f"Cleanup automatico Worktree in {workspace}...")
                wt_manager.remove_worktree(repo_master_path, workspace, force=True)

if __name__ == "__main__":
    main()
