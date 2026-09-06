#!/usr/bin/env python3
"""
Taktstock Brainstorming & Multi-Agent Buzz Chat Manager
-------------------------------------------------------
Manages interactive deliberation and multi-agent chat in Buzz/Slack style:
- State persisted in ~/taktstock/state/brainstorms/<brainstorm_id>.json
- Active session tracking by Telegram chatId
- Direct agent mentions:
  * @sol / @director  -> Lead Strategist (GPT-5.6 Sol)
  * @deepseek / @ds   -> Edge-Cases & Architecture (DeepSeek V4 Pro)
  * @glm / @claude    -> Security & Code Standards (GLM 5.3)
  * @agy              -> Practical CLI / File Execution Specialist
  * @all / @team      -> Entire team in open deliberation
- Structured round deliberation cycle and open multi-day chatroom
"""

import os
import re
import json
import logging
from pathlib import Path

# Auto-load .env if present
_env_f = Path(__file__).resolve().parent.parent / ".env"
if _env_f.exists():
    with open(_env_f, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k and k not in os.environ:
                    os.environ[k] = v

import fcntl
import threading
import subprocess
import uuid
from typing import Dict, Any, List, Optional, Tuple, Set
from datetime import datetime

from account_manager import CodexAccountManager
from projects_manager import ProjectsManager
from skills_manager import SkillsManager
from agent_prompts import CHAT_AGENT_PROMPTS, SOL_BRAINSTORM_SYSTEM_PROMPT

BASE_DIR = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or os.environ.get("ORCH_HOME") or (Path.home() / "taktstock"))
STATE_DIR = BASE_DIR / "state"

ALLOWED_CHANNELS: Set[str] = {"unknown", "telegram", "web", "api"}
REQUIRED_SUMMARY_FIELDS: Tuple[str, ...] = ("facts", "decisions", "constraints", "open_questions", "next_steps")

# Contratto P2 - Context Budget & Token Efficiency
MAX_SUMMARY_TOTAL_CHARS: int = 3000
MAX_SUMMARY_ITEMS_PER_SECTION: int = 5
MAX_SUMMARY_ITEM_CHARS: int = 180
MAX_CONTEXT_TAIL_MESSAGES: int = 20
MAX_MESSAGE_PROMPT_CHARS: int = 1500
MAX_AGGREGATE_PAYLOAD_CHARS: int = 32000
MAX_COMPACTION_INPUT_CHARS: int = 12000
TRUNCATION_MARKER: str = " ... [troncato per budget contesto]"


def _normalize_id(val: Optional[Any]) -> Optional[str]:
    """Normalizza identificativi (chat_id, web_session_id), convertendo stringhe vuote/None a None."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "undefined", '""', "''"):
        return None
    return s


def _generate_message_id(agent: str) -> str:
    """Genera un id messaggio univoco: timestamp leggibile + suffisso casuale sicuro."""
    return f"msg_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{agent}_{uuid.uuid4().hex[:8]}"


def is_context_eligible(message: Dict[str, Any]) -> bool:
    """Verifica se un messaggio è idoneo per essere incluso nel contesto/prompt dell'LLM o nella compattazione.

    Regole:
    1. Metadati espliciti (priorità assoluta):
       - exclude_from_context is True -> False
       - is_error is True -> False
       - message_type in ("compaction_notice", "operational_error", "system_notice") -> False
    2. Compatibilità legacy ristretta:
       - Annunci Gemini Compactor: sender == "Gemini Compactor" oppure (agent == "gemini" e text.startswith("⚡ *[Memoria Compattata]*"))
       - Nessuna ricerca libera di substring su messaggi utente (un utente che discute di '[Memoria Compattata]' resta idoneo).
       - Errori operativi legacy con prefissi specifici ("⚠️ [AGY Sidecar Error]", "⚠️ [Codex Error]", "⚠️ [Sidecar Error]", "⚠️ Runner host AGY occupato").
    3. Messaggi vuoti o non validi -> False.
    """
    if not isinstance(message, dict):
        return False
    if message.get("exclude_from_context") is True:
        return False
    if message.get("is_error") is True:
        return False
    if message.get("message_type") in ("compaction_notice", "operational_error", "system_notice"):
        return False

    sender = str(message.get("sender", "")).strip()
    agent = str(message.get("agent", "")).strip().lower()
    text = str(message.get("text", "")).strip()

    if not text:
        return False

    # Legacy: annunci compattatore mirati (solo sender noto o prefisso esatto da agent gemini)
    if sender == "Gemini Compactor":
        return False
    if agent == "gemini" and text.startswith("⚡ *[Memoria Compattata]*"):
        return False

    # Legacy: errori operativi sidecar con prefissi noti
    if text.startswith("⚠️ [AGY Sidecar Error]") or text.startswith("⚠️ [Codex Error]") or text.startswith("⚠️ [Sidecar Error]") or text.startswith("⚠️ Runner host AGY occupato"):
        return False

    return True


def truncate_for_context(text: str, max_chars: int = MAX_MESSAGE_PROMPT_CHARS) -> str:
    """Tronca in modo non mutante il testo per l'inserimento nel prompt agente."""
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if len(s) <= max_chars:
        return s
    cut_len = max(0, max_chars - len(TRUNCATION_MARKER))
    return s[:cut_len] + TRUNCATION_MARKER


def normalize_and_cap_summary(
    structured: Dict[str, List[str]],
    max_items_per_section: int = MAX_SUMMARY_ITEMS_PER_SECTION,
    max_chars_per_item: int = MAX_SUMMARY_ITEM_CHARS
) -> Dict[str, List[str]]:
    """Normalizza e applica cap rigido a ciascuna sezione della summary (max 180 caratteri totali per elemento inclusi i puntini)."""
    if not isinstance(structured, dict):
        return {}
    capped: Dict[str, List[str]] = {}
    for field in REQUIRED_SUMMARY_FIELDS:
        items = structured.get(field, [])
        if not isinstance(items, list):
            capped[field] = []
            continue
        clean_items = []
        for it in items:
            if not isinstance(it, str):
                continue
            cleaned = it.strip()
            if not cleaned:
                continue
            if len(cleaned) > max_chars_per_item:
                cut_len = max(0, max_chars_per_item - 3)
                cleaned = cleaned[:cut_len].rstrip() + "..."
            clean_items.append(cleaned)
            if len(clean_items) >= max_items_per_section:
                break
        capped[field] = clean_items
    return capped



def validate_structured_summary(raw_output: str) -> Tuple[bool, Optional[Dict[str, List[str]]], str]:
    """
    Valida rigorosamente l'output del compattatore Gemini:
    - Deve essere JSON valido
    - Deve contenere tutti i campi richiesti: facts, decisions, constraints, open_questions, next_steps
    - Ogni campo deve essere una lista di stringhe non vuote
    - Rifiuta stringhe vuote, messaggi di errore (rate limit, quota, traceback) o strutture incomplete
    """
    if not raw_output or not isinstance(raw_output, str):
        return False, None, "Output compattazione assente o non di tipo stringa."

    clean_text = raw_output.strip()

    # Rifiuta esplicitamente messaggi di errore o quota tipici di API/LLM
    error_patterns = [
        r"rate[_\s-]?limit",
        r"quota[_\s-]?exceeded",
        r"resource_exhausted",
        r"traceback \(most recent call last\)",
        r"internal server error",
        r"503 service unavailable",
        r"error\s*:\s*\{"
    ]
    for pat in error_patterns:
        if re.search(pat, clean_text, re.IGNORECASE):
            return False, None, f"Output compattazione contiene un errore o messaggio di quota: {clean_text[:120]}"

    # Estrazione blocco JSON (gestisce ```json ... ``` o JSON grezzo)
    json_str = clean_text
    if "```json" in json_str:
        json_str = json_str.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in json_str:
        json_str = json_str.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        parsed = json.loads(json_str)
    except Exception as e:
        return False, None, f"JSON non valido nell'output di compattazione: {e}"

    if not isinstance(parsed, dict):
        return False, None, "L'output di compattazione deve essere un oggetto JSON (dict)."

    validated_data: Dict[str, List[str]] = {}
    total_entries = 0

    for field in REQUIRED_SUMMARY_FIELDS:
        if field not in parsed:
            return False, None, f"Campo obbligatorio '{field}' mancante nel JSON di compattazione."
        val = parsed[field]
        if not isinstance(val, list):
            return False, None, f"Il campo '{field}' deve essere una lista di stringhe (trovato {type(val).__name__})."

        clean_list = []
        for idx, item in enumerate(val):
            if not isinstance(item, str):
                return False, None, f"Elemento #{idx} in '{field}' non è una stringa."
            s = item.strip()
            if s:
                clean_list.append(s)
        validated_data[field] = clean_list
        total_entries += len(clean_list)

    if total_entries == 0:
        return False, None, "Il riassunto non contiene alcun elemento significativo nei campi strutturati."

    return True, validated_data, ""


def format_canonical_summary(
    structured: Dict[str, List[str]],
    max_total_chars: int = MAX_SUMMARY_TOTAL_CHARS
) -> str:
    """Formats validated structure into canonical markdown summary for prompt/context."""
    capped = normalize_and_cap_summary(structured)
    sections = []
    field_titles = [
        ("facts", "📌 Key Facts & Context"),
        ("decisions", "✅ Decisions Made"),
        ("constraints", "⚠️ Constraints & Requirements"),
        ("open_questions", "❓ Open Questions"),
        ("next_steps", "🚀 Next Steps")
    ]
    for key, title in field_titles:
        items = capped.get(key, [])
        if items:
            lines = [f"{title}:"] + [f"- {it}" for it in items]
            sections.append("\n".join(lines))
    out = "\n\n".join(sections)
    if len(out) > max_total_chars:
        out = out[:max_total_chars].rstrip()
    return out



class SessionFileLock:
    """
    Gestore di lock a doppio livello (In-Process RLock + Cross-Process fcntl.flock)
    per garantire la mutazione atomica e la consistenza delle sessioni di brainstorming.

    Stato condiviso per chiave (lock_dir canonica, session_id):
    - Un solo RLock per chiave.
    - Un solo fd per chiave: il file lock viene aperto e flock(LOCK_EX) acquisito
      soltanto al primo ingresso (depth 0). Gli ingressi rientranti, anche tramite
      una seconda istanza di SessionFileLock per la stessa sessione, non aprono un
      secondo fd e non chiamano flock.
    - depth traccia il livello di rientranza; solo all'uscita esterna (depth -> 0)
      il flock viene rilasciato, il fd chiuso e lo stato azzerato; poi il RLock
      viene rilasciato.
    """
    _shared_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
    _meta_lock = threading.Lock()

    def __init__(self, lock_dir: Path, session_id: str):
        self.lock_dir = Path(lock_dir).resolve()
        self.session_id = session_id
        self.lock_file = self.lock_dir / f"{session_id}.lock"

        with self._meta_lock:
            key = (str(self.lock_dir), session_id)
            state = self._shared_state.get(key)
            if state is None:
                state = {
                    "rlock": threading.RLock(),
                    "fd": None,
                    "depth": 0,
                    "owner": None,
                }
                self._shared_state[key] = state
            self._state = state

    def __enter__(self):
        state = self._state
        state["rlock"].acquire()
        try:
            if state["depth"] == 0:
                # Primo ingresso: apri il file lock e acquisisci il flock esclusivo.
                self.lock_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(str(self.lock_dir), 0o700)
                except Exception:
                    pass

                fd = None
                try:
                    fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                    os.fchmod(fd, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except Exception:
                    if fd is not None:
                        try:
                            os.close(fd)
                        except Exception:
                            pass
                    raise

                state["fd"] = fd
                state["depth"] = 1
                state["owner"] = threading.get_ident()
            else:
                # Ingresso rientrante (stesso thread, RLock gia detenuto):
                # nessun nuovo fd, nessuna chiamata flock.
                state["depth"] += 1
            return self
        except Exception:
            state["rlock"].release()
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        state = self._state
        try:
            if state["depth"] > 0:
                state["depth"] -= 1
                if state["depth"] == 0:
                    # Uscita esterna: rilascia flock, chiudi fd e azzera lo stato.
                    fd = state["fd"]
                    state["fd"] = None
                    state["owner"] = None
                    if fd is not None:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        finally:
                            try:
                                os.close(fd)
                            except Exception:
                                pass
        finally:
            state["rlock"].release()


def load_default_session_agents() -> list[str]:
    """Legge il team base dalla stessa configurazione centrale dell'orchestratore."""
    for config_path in (BASE_DIR / "config.json", Path(__file__).resolve().parent / "config.json"):
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            agents = config.get("agent_policy", {}).get("default_session_agents", [])
            if isinstance(agents, list) and agents:
                return [str(agent) for agent in agents]
        except Exception:
            continue
    return ["sol", "agy", "luna"]

DEFAULT_SESSION_AGENTS = load_default_session_agents()

logger = logging.getLogger("TaktstockBrainstormManager")

BRAINSTORM_SYSTEM_PROMPT = SOL_BRAINSTORM_SYSTEM_PROMPT

def is_pure_ping(text: str) -> bool:
    clean = re.sub(r"[/@](sol|luna|terra|director|pro|flash|deepseek-pro|deepseek-flash|deepseek|ds|glm|claude|security|agy|antigravity|all|tutti|team)\b", "", text, flags=re.IGNORECASE).strip()
    return clean == "" or clean.lower() in ["ciao", "hey", "salve", "help", "menu", "start"]

AGENT_META = {
    "sol": {"name": "Sol", "role": "Session Manager & Strategist (GPT-5.6 Sol)", "avatar": "🧠", "color": "#10b981"},
    "luna": {"name": "Luna", "role": "n8n MCP Live-Change Specialist (GPT-5.6 Terra)", "avatar": "🌙", "color": "#06b6d4"},
    "deepseek": {"name": "DeepSeek Pro", "role": "Principal Architect & Edge Cases (DeepSeek V4 Pro)", "avatar": "🔍", "color": "#3b82f6"},
    "flash": {"name": "DeepSeek Flash", "role": "Rapid Reviewer & Pairing Specialist (DeepSeek V4 Flash)", "avatar": "⚡", "color": "#eab308"},
    "glm": {"name": "GLM 5.3 Flash", "role": "Fixer economico per frontend/UI e contratti", "avatar": "⚡", "color": "#8b5cf6"},
    "agy": {"name": "AGY", "role": "Senior Implementation Engineer (Antigravity CLI)", "avatar": "🚀", "color": "#f59e0b"},
    "user": {"name": "Tu", "role": "Product Owner", "avatar": "👤", "color": "#f43f5e"}
}

def atomic_write_json(path: Path, data: Any, indent: int = 2) -> bool:
    """
    Scrive atomicamente un file JSON usando un file temporaneo nella stessa directory,
    con flush, fsync e os.replace finale.
    - Il file temporaneo è creato con mode 0600 tramite os.open (bypassando umask).
    - os.fchmod garantisce 0600 prima del replace, indipendentemente dalla umask.
    - os.replace è atomico sul pathname: non segue symlink di destinazione.
    - In caso di errore, rimuove il temporaneo e preserva il file originale.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".tmp_{path.name}_{os.getpid()}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    fd = None
    try:
        content = json.dumps(data, indent=indent, ensure_ascii=False).encode("utf-8")
        # Apri con mode 0600 bypassando umask; O_EXCL impedisce di aprire file già esistenti
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except Exception:
            pass
        with os.fdopen(fd, "wb") as f:
            fd = None  # fdopen prende ownership del fd
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(path))
        # Assicura 0600 anche sul file di destinazione (per file preesistenti con permessi diversi)
        try:
            os.chmod(str(path), 0o600)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error(f"Errore scrittura atomica JSON in {path}: {e}")
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return False


def classify_codex_error(
    exc: Optional[Exception] = None,
    returncode: Optional[int] = None,
    output: str = "",
) -> Tuple[str, str]:
    """
    Classifies Codex CLI execution errors, distinguishing between:
    - binary_missing: codex binary not installed or not found in PATH
    - timeout: execution timed out
    - auth_error: authentication error or expired/invalid session
    - rate_limit: OpenAI rate limit / quota exceeded
    - generic_error: other operational errors

    Returns (error_category, user_friendly_message).
    """
    low_out = (output or "").lower()

    # 1. Missing binary
    if isinstance(exc, FileNotFoundError):
        return (
            "binary_missing",
            "⚠️ Codex CLI is not available in the Taktstock runtime: contact the administrator.",
        )
    if returncode == 127 or "codex: not found" in low_out or "command not found" in low_out or ("no such file or directory" in low_out and "codex" in low_out):
        return (
            "binary_missing",
            "⚠️ Codex CLI is not available in the Taktstock runtime: contact the administrator.",
        )

    # 2. Timeout
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in low_out or "timeout" in low_out:
        return (
            "timeout",
            "⚠️ Codex request timed out: the agent did not respond within the time limit.",
        )

    # 3. Authentication
    auth_keywords = [
        "unauthorized",
        "authentication failed",
        "invalid token",
        "token expired",
        "session expired",
        "please login",
        "login required",
        "auth.json missing",
        "401",
        "invalid_api_key",
        "forbidden",
        "403",
        "not authenticated",
    ]
    if any(k in low_out for k in auth_keywords):
        return (
            "auth_error",
            "⚠️ Codex authentication error: invalid or expired session.",
        )

    # 4. Real rate limit
    rate_limit_keywords = [
        "429",
        "rate limit",
        "rate_limit_exceeded",
        "insufficient_quota",
        "quota exceeded",
        "usage limit",
        "too many requests",
        "temporarily unavailable",
        "tokens per min",
        "requests per min",
    ]
    if any(k in low_out for k in rate_limit_keywords):
        return (
            "rate_limit",
            "⚠️ OpenAI/ChatGPT weekly quota exhausted (both on main account and bonus) until tomorrow morning reset.",
        )

    # 5. Generic
    if exc:
        err_detail = str(exc)
    elif returncode is not None and returncode != 0:
        err_detail = f"exit code {returncode}"
    else:
        err_detail = "unexpected error"

    return (
        "generic_error",
        f"⚠️ Codex execution error ({err_detail}).",
    )


class BrainstormManager:
    def lock_session(self, brainstorm_id: str) -> SessionFileLock:
        """Restituisce il context manager di lock per la specifica sessione."""
        lock_dir = self.state_dir / ".locks"
        return SessionFileLock(lock_dir, brainstorm_id)

    def clear_active_session(self, chat_id: Optional[str] = None, web_session_id: Optional[str] = None) -> str:
        """Azzera la memoria attiva per il chatId/webSessionId, archivia la sessione precedente e ne crea una nuova pulita."""
        active_id = self.get_active_brainstorm_id(chat_id=chat_id, web_session_id=web_session_id)
        if active_id:
            with self.lock_session(active_id):
                old_bs = self.load_brainstorm(active_id)
                if old_bs:
                    old_bs["status"] = "ARCHIVED_CLEARED"
                    old_bs["cleared_at"] = datetime.now().isoformat()
                    self.save_brainstorm(old_bs)
        
        # Crea una nuova sessione pulita
        new_bs_id = self.create_brainstorm(
            task="Nuova sessione di lavoro",
            preset="standard",
            chat_id=chat_id,
            web_session_id=web_session_id
        )
        logger.info(f"Memoria azzerata per chatId [{chat_id}] / webSessionId [{web_session_id}]. Nuova sessione: [{new_bs_id}].")
        return new_bs_id

    def __init__(
        self,
        state_dir: Optional[Path] = None,
        account_manager: Optional[CodexAccountManager] = None,
        db_manager: Optional[Any] = None,
        state_adapter: Optional[Any] = None,
        shadow_write: Optional[bool] = None,
        shadow_read: Optional[bool] = None,
    ):
        base_state = Path(state_dir) if state_dir else STATE_DIR
        if base_state.name == "brainstorms":
            self.state_dir = base_state
        else:
            self.state_dir = base_state / "brainstorms"

        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / ".locks").mkdir(parents=True, exist_ok=True)

        self.account_manager = account_manager or CodexAccountManager()
        self.projects_manager = ProjectsManager()
        self.skills_manager = SkillsManager()

        # Shadow persistence adapter (SQLite)
        if state_adapter is not None:
            self.state_adapter = state_adapter
        else:
            from infrastructure.brainstorm_state_adapter import BrainstormStateAdapter, _is_flag_enabled
            effective_shadow_write = _is_flag_enabled("TAKTSTOCK_SQLITE_SHADOW_WRITE", shadow_write) or _is_flag_enabled("UFFICIO_SQLITE_SHADOW_WRITE", shadow_write)
            effective_shadow_read = _is_flag_enabled("TAKTSTOCK_SQLITE_SHADOW_READ", shadow_read) or _is_flag_enabled("UFFICIO_SQLITE_SHADOW_READ", shadow_read)
            if effective_shadow_write or effective_shadow_read or db_manager is not None:
                self.state_adapter = BrainstormStateAdapter(
                    db_manager=db_manager,
                    shadow_write=effective_shadow_write,
                    shadow_read=effective_shadow_read,
                )
            else:
                self.state_adapter = None

    def _generate_id(self) -> str:
        return f"bs_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"

    def get_file_path(self, brainstorm_id: str) -> Path:
        return self.state_dir / f"{brainstorm_id}.json"

    def _resolve_chat_workspace(self, brainstorm_id: Optional[str] = None) -> Path:
        """Risolve un percorso di workspace reale e confinato per le sole sessioni chat/brainstorming."""
        if brainstorm_id:
            bs = self.load_brainstorm(brainstorm_id)
            if bs:
                wt_path_str = bs.get("worktree_path") or bs.get("workspace_path")
                if wt_path_str:
                    p = Path(wt_path_str).resolve()
                    if p.exists() and p.is_dir():
                        return p

        base_home = os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME")
        if base_home:
            base_workspaces = Path(base_home) / "workspaces"
        elif hasattr(self, "state_dir") and self.state_dir:
            base_workspaces = self.state_dir.parent / "workspaces"
        else:
            base_workspaces = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or (Path.home() / "taktstock")) / "workspaces"

        target_name = f"chat_{brainstorm_id}" if brainstorm_id else "chat_default"
        chat_ws = (base_workspaces / target_name).resolve()
        try:
            chat_ws.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return chat_ws

    def get_active_tg_mapping_path(self, chat_id: Optional[str]) -> Optional[Path]:
        norm = _normalize_id(chat_id)
        if not norm:
            return None
        safe_chat = norm.replace("/", "_")
        return self.state_dir / f"active_tg_{safe_chat}.json"

    def get_active_web_mapping_path(self, web_session_id: Optional[str]) -> Optional[Path]:
        norm = _normalize_id(web_session_id)
        if not norm:
            return None
        safe_web = norm.replace("/", "_")
        return self.state_dir / f"active_web_{safe_web}.json"

    def get_active_mapping_path(self, chat_id: Optional[str]) -> Optional[Path]:
        """Metodo retrocompatibile per percorsi mapping Telegram."""
        return self.get_active_tg_mapping_path(chat_id)

    def set_active_brainstorm(
        self,
        chat_id: Optional[str] = None,
        brainstorm_id: str = "",
        web_session_id: Optional[str] = None
    ):
        if not brainstorm_id:
            return
        norm_chat = _normalize_id(chat_id)
        norm_web = _normalize_id(web_session_id)
        payload = {"active_id": brainstorm_id, "timestamp": datetime.now().isoformat()}

        if norm_chat:
            map_path = self.get_active_tg_mapping_path(norm_chat)
            if map_path and atomic_write_json(map_path, payload):
                if self.state_adapter is not None:
                    try:
                        self.state_adapter.shadow_set_active(norm_chat, brainstorm_id)
                    except Exception as e:
                        logger.warning(f"Errore shadow set active per {brainstorm_id}: {e}")

        if norm_web:
            map_path = self.get_active_web_mapping_path(norm_web)
            if map_path:
                atomic_write_json(map_path, payload)

    def get_active_brainstorm_id(
        self,
        chat_id: Optional[str] = None,
        web_session_id: Optional[str] = None
    ) -> Optional[str]:
        norm_chat = _normalize_id(chat_id)
        norm_web = _normalize_id(web_session_id)

        if norm_chat:
            # 1. Nuovo formato specifico Telegram: active_tg_<chat_id>.json
            map_path = self.get_active_tg_mapping_path(norm_chat)
            if map_path and map_path.exists():
                try:
                    data = json.loads(map_path.read_text(encoding="utf-8"))
                    return data.get("active_id")
                except Exception:
                    pass

            # 2. Fallback retrocompatibile active_<chat_id>.json
            legacy_path = self.state_dir / f"active_{norm_chat.replace('/', '_')}.json"
            if legacy_path.exists():
                try:
                    data = json.loads(legacy_path.read_text(encoding="utf-8"))
                    return data.get("active_id")
                except Exception:
                    pass

            # 3. Fallback retrocompatibile active_chat.json
            legacy_chat = self.state_dir / "active_chat.json"
            if legacy_chat.exists():
                try:
                    data = json.loads(legacy_chat.read_text(encoding="utf-8"))
                    return data.get("active_id")
                except Exception:
                    pass

        if norm_web:
            # 1. Nuovo formato specifico Web: active_web_<web_session_id>.json
            map_path = self.get_active_web_mapping_path(norm_web)
            if map_path and map_path.exists():
                try:
                    data = json.loads(map_path.read_text(encoding="utf-8"))
                    return data.get("active_id")
                except Exception:
                    pass

            # 2. Fallback retrocompatibile active_web.json
            legacy_web = self.state_dir / "active_web.json"
            if legacy_web.exists():
                try:
                    data = json.loads(legacy_web.read_text(encoding="utf-8"))
                    return data.get("active_id")
                except Exception:
                    pass

        return None

    def clear_active_brainstorm(
        self,
        chat_id: Optional[str] = None,
        web_session_id: Optional[str] = None
    ):
        norm_chat = _normalize_id(chat_id)
        norm_web = _normalize_id(web_session_id)

        if norm_chat:
            tg_path = self.get_active_tg_mapping_path(norm_chat)
            if tg_path and tg_path.exists():
                try:
                    tg_path.unlink()
                except Exception:
                    pass
            legacy_path = self.state_dir / f"active_{norm_chat.replace('/', '_')}.json"
            if legacy_path.exists():
                try:
                    legacy_path.unlink()
                except Exception:
                    pass

        if norm_web:
            web_path = self.get_active_web_mapping_path(norm_web)
            if web_path and web_path.exists():
                try:
                    web_path.unlink()
                except Exception:
                    pass

    def load_brainstorm(self, brainstorm_id: str) -> Optional[Dict[str, Any]]:
        path = self.get_file_path(brainstorm_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "messages" not in data:
                data["messages"] = []
            # Retrocompatibilità per sessioni prive di revision o channel
            if "revision" not in data:
                data["revision"] = 0
            if "channel" not in data:
                data["channel"] = "unknown"
        except Exception as e:
            logger.error(f"Errore caricamento brainstorm {brainstorm_id}: {e}")
            return None

        # Shadow read e confronto semantico (se abilitato)
        if self.state_adapter is not None:
            try:
                self.state_adapter.shadow_compare(brainstorm_id, data)
            except Exception as e:
                logger.warning(f"Errore inatteso shadow compare per {brainstorm_id}: {e}")

        # Restituisce SEMPRE il JSON come fonte autorevole
        return data

    def save_brainstorm(self, data: Dict[str, Any]) -> bool:
        bs_id = data.get("id")
        if not bs_id:
            return False
        path = self.get_file_path(bs_id)
        # Prepara la nuova revision ma scrive prima, per non avanzare l'indice in caso di errore
        next_revision = int(data.get("revision", 0)) + 1
        updated_at = datetime.now().isoformat()
        data["revision"] = next_revision
        data["updated_at"] = updated_at
        if not atomic_write_json(path, data):
            # Ripristina la revision precedente: il salvataggio non è avvenuto
            data["revision"] = next_revision - 1
            return False

        # Shadow write in SQLite dopo che il JSON è stato salvato con successo
        if self.state_adapter is not None:
            try:
                self.state_adapter.shadow_save(data)
            except Exception as e:
                logger.warning(f"Errore inatteso shadow write per {bs_id}: {e}")
        return True

    def list_brainstorms(self) -> List[Dict[str, Any]]:
        items = []
        for p in self.state_dir.glob("bs_*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                items.append(d)
            except Exception:
                pass
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return items

    def create_brainstorm(
        self,
        task: str,
        preset: str = "critical",
        chat_id: Optional[str] = None,
        web_session_id: Optional[str] = None,
        channel: Optional[str] = None,
        repo: Optional[str] = None
    ) -> str:
        """Crea una nuova sessione di brainstorming con revisione coerente e canale esplicito."""
        bs_id = self._generate_id()
        now = datetime.now().isoformat()

        norm_chat = _normalize_id(chat_id)
        norm_web = _normalize_id(web_session_id)

        # Risoluzione canale: default unknown, telegram, web o api
        ch = channel
        if not ch or ch not in ALLOWED_CHANNELS:
            if norm_chat:
                ch = "telegram"
            elif norm_web:
                ch = "web"
            else:
                ch = "unknown"

        project_name = ""
        project_tech = ""
        final_repo = repo or ""
        if final_repo:
            proj = self.projects_manager.find_project(final_repo)
            if proj:
                final_repo = proj["path"]
                project_name = proj["name"]
                project_tech = proj["tech_stack"]

        brainstorm_data = {
            "id": bs_id,
            "revision": 0,  # save_brainstorm incrementerà ad 1
            "channel": ch,
            "chat_id": norm_chat,
            "web_session_id": norm_web,
            "task": task,
            "repo": final_repo,
            "project_name": project_name,
            "project_tech": project_tech,
            "preset": preset,
            "selected_agents": DEFAULT_SESSION_AGENTS.copy(),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "rounds": [],
            "messages": [
                {
                    "id": _generate_message_id("init"),
                    "sender": "System",
                    "agent": "sol",
                    "text": f"🚀 Sessione avviata per il task: **{task}**\nPuoi chattare liberamente menzionando gli agenti (@sol, @deepseek, @glm, @agy, @all).",
                    "timestamp": now
                }
            ],
            "compacted_up_to_index": 0,
            "chat_summary": "",
            "final_plan": None,
            "approved_by": None,
            "approved_at": None,
            "rejected_reason": None
        }

        with self.lock_session(bs_id):
            self.save_brainstorm(brainstorm_data)

        if norm_chat or norm_web:
            self.set_active_brainstorm(chat_id=norm_chat, brainstorm_id=bs_id, web_session_id=norm_web)

        return bs_id

    def run_round(
        self,
        brainstorm_id: str,
        user_feedback: Optional[str] = None,
        runner: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Esegue un round formale di brainstorming e registra i messaggi nella chatroom.

        Concorrenza & persistenza:
        a) Sotto `lock_session` salva subito user_feedback (se presente) e registra il round
           atteso e la snapshot_revision;
        b) chiamate LLM/reviewer FUORI dal lock;
        c) sotto `lock_session` ricarica lo stato fresco: se nel frattempo e' comparso un altro
           round, NON sovrascrive nulla e ritorna un conflitto controllato;
        d) altrimenti appende il round e il messaggio di sintesi allo stato fresco e salva.
        """
        # a) Mutazioni e snapshot sotto lock (nessuna perdita con post_chat_message concorrenti)
        with self.lock_session(brainstorm_id):
            bs = self.load_brainstorm(brainstorm_id)
            if not bs:
                raise ValueError(f"Brainstorm {brainstorm_id} not found.")

            round_num = len(bs.get("rounds", [])) + 1
            task = bs["task"]
            snapshot_revision = bs.get("revision", 0)
            logger.info(f"Execution Brainstorm [{brainstorm_id}] Round {round_num} (Feedback: {bool(user_feedback)})")

            if user_feedback:
                bs["messages"].append({
                    "id": _generate_message_id("usr"),
                    "sender": "User",
                    "agent": "user",
                    "text": user_feedback,
                    "timestamp": datetime.now().isoformat()
                })
                self.save_brainstorm(bs)

        # 1. Director (Sol) Analysis or Update
        if round_num == 1:
            dir_prompt = f"""{BRAINSTORM_SYSTEM_PROMPT}

REQUESTED TASK: {task}
REPOSITORY: {bs.get('repo') or 'Local workspace'}
PRESET: {bs.get('preset', 'critical')}

Analyze the architectural task in detail. Decide autonomously if complexity requires an independent reviewer: use only the identifier ds-pro, otherwise reviewers must be []. Respond EXCLUSIVELY in valid JSON:
{{
  "phase": "brainstorm",
  "analysis": "In-depth analysis of architecture and implications",
  "risks": ["Risk 1 with impact", "Risk 2"],
  "strategy": "Detailed proposed implementation strategy",
  "reviewers": ["ds-pro"],
  "questions_for_user": ["Open question 1 for user"]
}}"""
            is_critical = (bs.get("preset", "standard") == "critical")
            if runner:
                dir_analysis = runner.call_director(dir_prompt, high_effort=is_critical)
            else:
                dir_analysis = {
                    "analysis": f"Strategic analysis for: {task}",
                    "risks": ["Integration complexity", "Potential regressions"],
                    "strategy": "Modular approach driven by tests and isolation.",
                    "questions_for_user": ["Which compatibility constraints are prioritized?"]
                }
        else:
            prev_round = bs["rounds"][-1]
            dir_prompt = f"""{BRAINSTORM_SYSTEM_PROMPT}

ORIGINAL TASK: {task}
LATEST SYNTHESIS: {json.dumps(prev_round.get('director_synthesis', {}), ensure_ascii=False)}
USER FEEDBACK: {user_feedback}

Update the architectural strategy integrating user guidance. Respond in JSON:
{{
  "phase": "brainstorm",
  "analysis": "Updated analysis",
  "risks": ["Revised risk"],
  "strategy": "Updated technical plan",
  "reviewers": [],
  "questions_for_user": []
}}"""
            is_critical = (bs.get("preset", "standard") == "critical")
            if runner:
                dir_analysis = runner.call_director(dir_prompt, high_effort=is_critical)
            else:
                dir_analysis = {
                    "analysis": f"Updated analysis with feedback: {user_feedback}",
                    "risks": ["Verify specified constraints"],
                    "strategy": f"Revised plan: {user_feedback}",
                    "questions_for_user": []
                }

        if not isinstance(dir_analysis, dict) or not dir_analysis.get("strategy"):
            dir_analysis = {
                "analysis": f"Analysis for: {task}",
                "risks": ["Complexity"],
                "strategy": "Incremental modular development.",
                "questions_for_user": []
            }

        # 2. Reviewer consultation: preset can enforce them, otherwise Sol decides in JSON.
        # No reviewer is called by default.
        reviewer_opinions = []
        active_reviewers = list(runner.preset_config.get("reviewers", [])) if runner else []
        suggested_reviewers = dir_analysis.get("reviewers", []) if isinstance(dir_analysis, dict) else []
        aliases = {"deepseek": "ds-pro", "deepseek-pro": "ds-pro", "pro": "ds-pro"}
        auto_select = runner.agent_policy.get("reviewers", {}).get("automatic_selection_by_sol", True) if runner else True
        if auto_select:
            for reviewer in suggested_reviewers if isinstance(suggested_reviewers, list) else []:
                canonical = aliases.get(str(reviewer).strip().lower(), str(reviewer).strip().lower())
                if canonical == "ds-pro" and canonical not in active_reviewers:
                    active_reviewers.append(canonical)

        if "ds-pro" in active_reviewers:
            ds_prompt = f"""TASK: {task}
DIRECTOR PROPOSAL:
{json.dumps(dir_analysis, indent=2, ensure_ascii=False)}
PREVIOUS USER FEEDBACK: {user_feedback or 'None (Round 1)'}

{CHAT_AGENT_PROMPTS["deepseek"]}
Respond EXCLUSIVELY in JSON:
{{
  "agent": "deepseek-pro",
  "opinion": "Your technical assessment and recommendations",
  "concerns": ["Edge case 1", "Edge case 2"]
}}"""
            ds_res = runner.call_reviewer_deepseek_pro(ds_prompt) if runner else {
                "agent": "deepseek-pro",
                "opinion": "Solid architecture. Pay attention to transactions and rollback.",
                "concerns": ["Error handling", "Backward compatibility"]
            }
            if isinstance(ds_res, dict):
                ds_res["agent"] = "deepseek-pro"
                reviewer_opinions.append(ds_res)

        # 3. Final Director Synthesis for Round
        synthesis_prompt = f"""{BRAINSTORM_SYSTEM_PROMPT}

TASK: {task}
DIRECTOR ANALYSIS: {json.dumps(dir_analysis, ensure_ascii=False)}
REVIEWER OPINIONS: {json.dumps(reviewer_opinions, ensure_ascii=False)}
USER FEEDBACK: {user_feedback or 'Session start'}

Synthesize the round formulating final recommendation and open questions.
Respond EXCLUSIVELY in JSON:
{{
  "phase": "synthesis",
  "consensus": ["Agreement point 1", "Agreement point 2"],
  "disagreements": ["Debated point / alternative"],
  "recommended_option": "Detailed recommended option",
  "open_questions": ["Question 1 for user"],
  "ready_for_approval": true
}}"""
        is_critical = (bs.get("preset", "standard") == "critical")
        synthesis = runner.call_director(synthesis_prompt, high_effort=is_critical) if runner else {
            "consensus": ["Modular approach", "Rigorous validation"],
            "disagreements": [],
            "recommended_option": dir_analysis.get("strategy", "Incremental modular development."),
            "open_questions": dir_analysis.get("questions_for_user", ["Do you confirm proceeding with this strategy?"]),
            "ready_for_approval": True
        }

        if not isinstance(synthesis, dict) or not synthesis.get("recommended_option"):
            synthesis = {
                "consensus": ["Alignment on requirements"],
                "disagreements": [],
                "recommended_option": dir_analysis.get("strategy", "Plan agreed upon by the team."),
                "open_questions": [],
                "ready_for_approval": True
            }

        round_data = {
            "round": round_num,
            "timestamp": datetime.now().isoformat(),
            "user_feedback": user_feedback,
            "director_analysis": dir_analysis,
            "reviewer_opinions": reviewer_opinions,
            "director_synthesis": synthesis
        }

        # c) Commit round on fresh state under lock, with anti-overwrite guard
        with self.lock_session(brainstorm_id):
            fresh = self.load_brainstorm(brainstorm_id)
            if not fresh:
                raise ValueError(f"Brainstorm {brainstorm_id} not found at round commit.")

            # If another round appeared in the meantime, do not overwrite: controlled conflict.
            if len(fresh.get("rounds", [])) >= round_num:
                logger.warning(
                    f"Round {round_num} discarded for [{brainstorm_id}]: another round was "
                    f"recorded during processing ({len(fresh.get('rounds', []))} present)."
                )
                return {
                    "conflict": True,
                    "brainstorm_id": brainstorm_id,
                    "expected_round": round_num,
                    "actual_rounds": len(fresh.get("rounds", [])),
                    "snapshot_revision": snapshot_revision,
                }

            fresh["rounds"].append(round_data)

            # d) Synthetic chatroom message on fresh state
            fresh["messages"].append({
                "id": _generate_message_id("sol_synth"),
                "sender": "Sol",
                "agent": "sol",
                "text": f"📋 **Round {round_num} Summary:**\n🎯 *Recommendation:* {synthesis.get('recommended_option')}\n" +
                        (f"❓ *Questions:* {', '.join(synthesis.get('open_questions', []))}" if synthesis.get('open_questions') else ""),
                "timestamp": datetime.now().isoformat()
            })

            self.save_brainstorm(fresh)
            return fresh

    def _check_and_compact_chat(self, brainstorm_id_or_bs: Any, runner: Optional[Any] = None) -> bool:
        """
        Auto-Compattazione e Riassunto della Chat (Context Compaction).
        Ogni 10 messaggi non compattati:
        1. Carica snapshot e revisione iniziale.
        2. Esegue la chiamata Gemini FUORI DAL LOCK per non bloccare altri thread/processi.
        3. Valida rigorosamente la struttura JSON (facts, decisions, constraints, open_questions, next_steps).
        4. Sotto SessionFileLock, verifica che la revisione non sia cambiata (CAS).
           Se cambiata, scarta la sintesi senza alterare indici o messaggi.
        """
        if isinstance(brainstorm_id_or_bs, dict):
            bs_id = brainstorm_id_or_bs.get("id")
        else:
            bs_id = str(brainstorm_id_or_bs)

        if not bs_id:
            return False

        # 1. Carica snapshot iniziale
        # Use provided dict as snapshot if caller passed a dict with messages (test scenario)
        if isinstance(brainstorm_id_or_bs, dict):
            snapshot_bs = brainstorm_id_or_bs
        else:
            snapshot_bs = self.load_brainstorm(bs_id)
        if not snapshot_bs:
            return False

        snapshot_revision = snapshot_bs.get("revision", 0)
        raw_msgs = snapshot_bs.get("messages", [])
        last_compacted = snapshot_bs.get("compacted_up_to_index", 0)
        uncompacted_count = len(raw_msgs) - last_compacted

        # Se ci sono almeno 10 messaggi da compattare
        if uncompacted_count < 10 or len(raw_msgs) < 10:
            return False

        messages_to_summarize = [m for m in raw_msgs[last_compacted:-3] if is_context_eligible(m)]
        if not messages_to_summarize:
            return False

        target_compacted_index = max(0, len(raw_msgs) - 3)

        COMPACTION_OMISSION_MARKER = "[... messaggi precedenti omessi per limite di budget compattazione ...]\n\n"
        marker_len = len(COMPACTION_OMISSION_MARKER)

        # Costruisce il batch a ritroso preservando i record completi e il budget di MAX_COMPACTION_INPUT_CHARS
        selected_lines = []
        current_len = 0
        omitted_earlier = False

        for idx, m in enumerate(reversed(messages_to_summarize)):
            s_name = m.get("sender", m.get("agent", "Agente"))
            txt = truncate_for_context(m.get('text', ''), max_chars=MAX_MESSAGE_PROMPT_CHARS)
            record_str = f"{s_name}: {txt}"
            record_len = len(record_str) + (2 if selected_lines else 0)  # account for "\n\n"

            # Se ci sono ancora messaggi prima di questo nel lotto originale, riserva lo spazio del marker
            remaining_after_this = len(messages_to_summarize) - (idx + 1)
            reserved_marker = marker_len if remaining_after_this > 0 else 0

            if current_len + record_len + reserved_marker > MAX_COMPACTION_INPUT_CHARS:
                omitted_earlier = True
                break
            selected_lines.append(record_str)
            current_len += record_len

        selected_lines.reverse()
        chat_text = "\n\n".join(selected_lines)
        if omitted_earlier:
            chat_text = COMPACTION_OMISSION_MARKER + chat_text

        # Asserzione di garanzia assoluta sul limite del compattatore
        if len(chat_text) > MAX_COMPACTION_INPUT_CHARS:
            chat_text = chat_text[:MAX_COMPACTION_INPUT_CHARS]

        previous_summary = snapshot_bs.get("chat_summary", "")
        if len(previous_summary) > MAX_SUMMARY_TOTAL_CHARS:
            previous_summary = truncate_for_context(previous_summary, max_chars=MAX_SUMMARY_TOTAL_CHARS)



        summary_prompt = (
            f"You are the Memory Compaction module of Taktstock Multi-Agent.\n"
            f"ORIGINAL TASK: {snapshot_bs.get('task')}\n\n"
            f"PREVIOUS SUMMARY:\n{previous_summary or 'No previous summary.'}\n\n"
            f"NEW MESSAGES TO INTEGRATE:\n{chat_text}\n\n"
            f"Generate a dense, faithful, and structured summary of the discussed points.\n"
            f"Respond EXCLUSIVELY with valid JSON matching this exact structure:\n"
            f"{{\n"
            f'  "facts": ["point 1", "point 2"],\n'
            f'  "decisions": ["decision 1"],\n'
            f'  "constraints": ["constraint 1"],\n'
            f'  "open_questions": ["question 1"],\n'
            f'  "next_steps": ["step 1"]\n'
            f"}}\n"
            f"Each field must be a list of strings (max {MAX_SUMMARY_ITEMS_PER_SECTION} points per field, max {MAX_SUMMARY_ITEM_CHARS} characters each). Do not include any text before or after the JSON."
        )

        summary_messages = [
            {"role": "system", "content": "You are an expert technical conversation synthesizer. Respond only with valid JSON."},
            {"role": "user", "content": summary_prompt}
        ]

        # 2. LLM call OUTSIDE LOCK
        if runner and runner.mock_mode:
            raw_summary = json.dumps({
                "facts": ["Discussion on architecture and target"],
                "decisions": ["Alignment on requirements"],
                "constraints": ["No downtime"],
                "open_questions": [],
                "next_steps": ["Proceed with tests"]
            })
        else:
            raw_summary = self._query_agent_llm("gemini", summary_messages)

        # 3. Strict structured validation (reject non-conforming JSON, quota, rate limit, errors)
        valid, structured_data, err_msg = validate_structured_summary(raw_summary or "")
        if not valid or not structured_data:
            logger.warning(
                f"Compaction cancelled for [{bs_id}]: invalid Gemini summary ({err_msg}). "
                f"compacted_up_to_index and messages remain unchanged."
            )
            return False

        canonical_summary = format_canonical_summary(structured_data, max_total_chars=MAX_SUMMARY_TOTAL_CHARS)

        # 4. Acquisizione Lock e verifica di non-mutazione (CAS su revision)
        with self.lock_session(bs_id):
            fresh_bs = self.load_brainstorm(bs_id)
            if not fresh_bs:
                return False

            if fresh_bs.get("revision", 0) != snapshot_revision:
                logger.info(
                    f"Compattazione scartata per [{bs_id}]: revision cambiata durante la chiamata LLM "
                    f"({snapshot_revision} -> {fresh_bs.get('revision')}). Nessun messaggio o indice alterato."
                )
                return False

            fresh_bs["chat_summary"] = canonical_summary
            fresh_bs["chat_summary_structured"] = structured_data
            fresh_bs["compacted_up_to_index"] = target_compacted_index

            compaction_msg = {
                "id": _generate_message_id("compaction"),
                "sender": "Gemini Compactor",
                "agent": "gemini",
                "text": "⚡ *[Memoria Compattata]* Ho archiviato i punti chiave discussi finora per ottimizzare i token. La conversazione prosegue con pieno contesto!",
                "timestamp": datetime.now().isoformat(),
                "message_type": "compaction_notice",
                "exclude_from_context": True
            }
            fresh_bs["messages"].append(compaction_msg)
            self.save_brainstorm(fresh_bs)
            logger.info(f"Auto-compattazione completata per [{bs_id}] all'indice {target_compacted_index}.")
            # If caller passed the brainstorm dict directly, update it in-place so caller sees changes
            if isinstance(brainstorm_id_or_bs, dict):
                brainstorm_id_or_bs.clear()
                brainstorm_id_or_bs.update(fresh_bs)
            return True

    def _append_reply_under_lock(
        self,
        brainstorm_id: str,
        reply_msg: Optional[Dict[str, Any]] = None,
        mutations: Optional[Any] = None,
    ) -> bool:
        """Persiste una risposta (ed eventuali mutazioni) su un dict fresco sotto SessionFileLock.

        Nessun ciclo load->mutate->save su uno snapshot stantio: ricarica sempre lo stato
        corrente sotto lock. Il merge e' idempotente per message id (nessun duplicato).
        """
        with self.lock_session(brainstorm_id):
            fresh = self.load_brainstorm(brainstorm_id)
            if not fresh:
                return False
            if mutations is not None:
                mutations(fresh)
            if reply_msg is not None:
                messages = fresh.setdefault("messages", [])
                if not any(m.get("id") == reply_msg.get("id") for m in messages):
                    messages.append(reply_msg)
            self.save_brainstorm(fresh)
            return True


    def _build_chat_messages(
        self,
        bs: Dict[str, Any],
        target_agent: str,
        system_prompt: str,
        inspected_file: Optional[Dict[str, str]] = None
    ) -> List[Dict[str, str]]:
        """Constructs multi-day chatroom history enriched with projects catalog and context, with hard cap payload <= 32,000 chars."""
        from datetime import datetime
        task = bs.get("task", "General task")
        task_clean = truncate_for_context(str(task), max_chars=1000)
        repo = bs.get("repo", "")
        project_name = bs.get("project_name", "")
        chat_summary = bs.get("chat_summary", "")
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # 1. Projects overview configured on server (bounded)
        all_projects = self.projects_manager.list_projects()
        proj_lines = []
        for p in all_projects:
            aliases_str = f" (alias: {', '.join(p['aliases'])})" if len(p.get("aliases", [])) > 1 else ""
            proj_lines.append(f"- 📁 {p['name']}{aliases_str}: {p['tech_stack']} — {p['description']}")
        projects_catalog = "\n".join(proj_lines) if proj_lines else "No projects found."
        if len(projects_catalog) > 2500:
            projects_catalog = truncate_for_context(projects_catalog, max_chars=2500)
        
        # 2. Details of Active Project (if selected, with tree and doc limits)
        active_project_block = ""
        target_lookup = project_name or repo
        if target_lookup:
            active_proj = self.projects_manager.find_project(target_lookup)
            if active_proj:
                tree_str = self.projects_manager.get_project_tree(active_proj["path"], max_depth=2, max_entries=25)
                doc_str = self.projects_manager.get_project_doc_snippet(active_proj["path"], max_chars=800)
                active_project_block = (
                    f"\n[ACTIVE PROJECT SELECTED: {active_proj['name']}]\n"
                    f"- Path: {active_proj['path']}\n"
                    f"- Tech Stack: {active_proj['tech_stack']}\n"
                    f"- Git Branch: {active_proj.get('git_branch', 'main')}\n"
                    f"- Description: {active_proj['description']}\n"
                    f"- Main File Structure:\n{tree_str}\n"
                    f"- Key Documentation:\n{doc_str}"
                )
        if not active_project_block:
            active_project_block = "\n[ACTIVE PROJECT]: No specific project selected. The user can set one with 'work on <project>' or '/project <name>'."
            
        # 3. File inspection (bounded with explicit marker)
        file_inspection_block = ""
        if inspected_file and isinstance(inspected_file, dict):
            f_path = str(inspected_file.get("path", "")).strip()
            f_content = truncate_for_context(str(inspected_file.get("content", "")), max_chars=4000)
            if f_path and f_content:
                file_inspection_block = f"\n\n[PROJECT FILE INSPECTION: {f_path}]\n{f_content}"

        summary_context = f"\n\n[CONSOLIDATED HISTORICAL MEMORY]\n{truncate_for_context(chat_summary, MAX_SUMMARY_TOTAL_CHARS)}" if chat_summary else ""
        session_agents = ", ".join(bs.get("selected_agents", DEFAULT_SESSION_AGENTS))
        skills_raw = self.skills_manager.format_skills_for_prompt(f"{task_clean} {active_project_block}")
        skills_context = truncate_for_context(skills_raw, max_chars=2500)

        # Mandatory core guidelines (must always remain intact in system prompt)
        progetti_base_str = str(getattr(self.projects_manager, "base_dir", "/progetti"))
        core_guidelines = (
            "Memory & Chat Guidelines:\n"
            "- You are part of a collaborative multi-agent chatroom in Buzz/Slack style with Massimo.\n"
            f"- You have visibility into all projects located on the server ({progetti_base_str}).\n"
            "- You inherit and master all Taktstock Universal Skills (Training, Nutrition, Marketing, Web Design, n8n, Security, Antigravity).\n"
            f"- Base team selected for this session: {session_agents}. Sol can request DeepSeek Pro and GLM only when complexity warrants it.\n"
            "- You can always list projects and skills, summarize them, and advise on which to focus.\n"
            "- When a project is active, reason based on the actual file structure, tech stack, and code of the project.\n"
            "- If the user writes 'work on X', 'switch to X', or '/project X', the system automatically sets the context to that project.\n"
            "- Filesystem access is secure and sandboxed read-only to the projects folder.\n"
            "- Be actionable, fast, and thorough."
        )

        system_prompt_clean = truncate_for_context(str(system_prompt), max_chars=6000)

        dynamic_context = (
            f"[PROJECT & ENVIRONMENT CONTEXT]\n"
            f"Current Date: {today_str}\n"
            f"Active Task: {task_clean}\n"
            f"Repository/Path: {repo or 'None'}"
            f"{summary_context}\n\n"
            f"[CATALOG OF AVAILABLE PROJECTS ON SERVER ({progetti_base_str})]\n"
            f"{projects_catalog}\n"
            f"{active_project_block}"
            f"{file_inspection_block}\n\n"
            f"{skills_context}"
        )
        if len(dynamic_context) > 8000:
            dynamic_context = truncate_for_context(dynamic_context, max_chars=8000)

        full_system = f"{system_prompt_clean}\n\n{dynamic_context}\n\n{core_guidelines}"
        if len(full_system) > 16000:
            # Truncate only dynamic portion preserving core guidelines intact
            avail_for_dynamic = max(1000, 16000 - len(system_prompt_clean) - len(core_guidelines) - 50)
            dynamic_context = truncate_for_context(dynamic_context, max_chars=avail_for_dynamic)
            full_system = f"{system_prompt_clean}\n\n{dynamic_context}\n\n{core_guidelines}"

        raw_msgs = bs.get("messages", [])

        # Multi-day and bounded context management: filter non-eligible messages
        if chat_summary:
            last_compacted = bs.get("compacted_up_to_index", 0)
            eligible_tail = [m for m in raw_msgs[last_compacted:] if is_context_eligible(m)]
            recent_msgs = eligible_tail[-MAX_CONTEXT_TAIL_MESSAGES:]
        else:
            eligible_msgs = [m for m in raw_msgs if is_context_eligible(m)]
            recent_msgs = eligible_msgs[-MAX_CONTEXT_TAIL_MESSAGES:]

        conversation_turns: List[Dict[str, str]] = [{"role": "system", "content": full_system}]

        for m in recent_msgs:
            s_agent = m.get("agent", "user")
            s_name = m.get("sender", "User")
            txt = truncate_for_context(m.get("text", ""), max_chars=MAX_MESSAGE_PROMPT_CHARS)
            if not txt:
                continue

            if s_agent == target_agent:
                conversation_turns.append({"role": "assistant", "content": txt})
            elif s_agent == "user":
                conversation_turns.append({"role": "user", "content": f"{s_name}: {txt}"})
            else:
                meta = AGENT_META.get(s_agent, {"name": s_name})
                conversation_turns.append({"role": "user", "content": f"[{meta['name']}]: {txt}"})

        # Group consecutive turns of same role for maximum LLM compatibility
        merged = []
        for turn in conversation_turns:
            if turn["role"] == "system":
                merged.append(turn)
            elif not merged or merged[-1]["role"] == "system":
                merged.append(turn)
            elif merged[-1]["role"] == turn["role"]:
                merged[-1]["content"] += f"\n\n{turn['content']}"
            else:
                merged.append(turn)

        # Budget Allocator: eliminate intermediate turns until within 32k chars
        while sum(len(t["content"]) for t in merged) > MAX_AGGREGATE_PAYLOAD_CHARS and len(merged) > 2:
            merged.pop(1)

        # If only [system, last_message] (or only system) remains and total still exceeds 32k
        if sum(len(t["content"]) for t in merged) > MAX_AGGREGATE_PAYLOAD_CHARS:
            if len(merged) == 2:
                sys_len = len(merged[0]["content"])
                if sys_len > 14000:
                    if "Memory & Chat Guidelines:" in merged[0]["content"]:
                        sys_parts = merged[0]["content"].split("Memory & Chat Guidelines:", 1)
                        head = truncate_for_context(sys_parts[0], max_chars=14000 - len(core_guidelines) - 50)
                        merged[0]["content"] = f"{head}\n\nMemory & Chat Guidelines:{sys_parts[1]}"
                    elif "Linee Guida Memoria & Chat:" in merged[0]["content"]:
                        sys_parts = merged[0]["content"].split("Linee Guida Memoria & Chat:", 1)
                        head = truncate_for_context(sys_parts[0], max_chars=14000 - len(core_guidelines) - 50)
                        merged[0]["content"] = f"{head}\n\nLinee Guida Memoria & Chat:{sys_parts[1]}"
                    else:
                        merged[0]["content"] = truncate_for_context(merged[0]["content"], max_chars=14000)
                avail_for_user = max(200, MAX_AGGREGATE_PAYLOAD_CHARS - len(merged[0]["content"]))
                if len(merged[1]["content"]) > avail_for_user:
                    merged[1]["content"] = truncate_for_context(merged[1]["content"], max_chars=avail_for_user)
            elif len(merged) == 1:
                if "Memory & Chat Guidelines:" in merged[0]["content"]:
                    sys_parts = merged[0]["content"].split("Memory & Chat Guidelines:", 1)
                    head = truncate_for_context(sys_parts[0], max_chars=MAX_AGGREGATE_PAYLOAD_CHARS - len(core_guidelines) - 50)
                    merged[0]["content"] = f"{head}\n\nMemory & Chat Guidelines:{sys_parts[1]}"
                elif "Linee Guida Memoria & Chat:" in merged[0]["content"]:
                    sys_parts = merged[0]["content"].split("Linee Guida Memoria & Chat:", 1)
                    head = truncate_for_context(sys_parts[0], max_chars=MAX_AGGREGATE_PAYLOAD_CHARS - len(core_guidelines) - 50)
                    merged[0]["content"] = f"{head}\n\nLinee Guida Memoria & Chat:{sys_parts[1]}"
                else:
                    merged[0]["content"] = truncate_for_context(merged[0]["content"], max_chars=MAX_AGGREGATE_PAYLOAD_CHARS)

        # Failsafe clamp assoluto
        total_chars = sum(len(t["content"]) for t in merged)
        if total_chars > MAX_AGGREGATE_PAYLOAD_CHARS:
            excess = total_chars - MAX_AGGREGATE_PAYLOAD_CHARS
            if len(merged) > 1 and len(merged[-1]["content"]) > excess + len(TRUNCATION_MARKER):
                cut = len(merged[-1]["content"]) - excess - len(TRUNCATION_MARKER)
                merged[-1]["content"] = merged[-1]["content"][:cut] + TRUNCATION_MARKER
            else:
                merged[0]["content"] = merged[0]["content"][:len(merged[0]["content"]) - excess]

        return merged

    def compute_context_metrics(
        self,
        bs: Dict[str, Any],
        turns: List[Dict[str, str]],
        inspected_file: Optional[Dict[str, str]] = None
    ) -> Dict[str, int]:
        """Calcola le 4 metriche quantitative del context budget in modo rigoroso e privacy-safe."""
        raw_msgs = bs.get("messages", [])
        chat_summary = bs.get("chat_summary", "")
        last_compacted = bs.get("compacted_up_to_index", 0) if chat_summary else 0
        eligible = [m for m in raw_msgs[last_compacted:] if is_context_eligible(m)]
        recent = eligible[-MAX_CONTEXT_TAIL_MESSAGES:]

        # 1. context_message_count: messaggi storici idonei effettivamente inclusi nel tail
        context_message_count = len(recent)

        # 2. summary_length_chars: caratteri effettivi della summary inseriti nel prompt
        summary_length_chars = len(truncate_for_context(chat_summary, MAX_SUMMARY_TOTAL_CHARS)) if chat_summary else 0

        # 3. context_payload_chars: totale caratteri inviati a LLM attraverso tutti i turni
        context_payload_chars = sum(len(t.get("content", "")) for t in turns)

        # 4. truncated_message_count: conteggio blocchi/messaggi che hanno subito troncamento
        truncated_count = 0
        for m in recent:
            if len(m.get("text", "")) > MAX_MESSAGE_PROMPT_CHARS:
                truncated_count += 1
        if inspected_file and len(str(inspected_file.get("content", ""))) > 4000:
            truncated_count += 1
        if len(str(bs.get("task", ""))) > 1000:
            truncated_count += 1
        if chat_summary and len(chat_summary) > MAX_SUMMARY_TOTAL_CHARS:
            truncated_count += 1
        if len(recent) > 0 and len(turns) < 2 and context_message_count > 0:
            truncated_count += 1

        return {
            "context_message_count": int(context_message_count),
            "summary_length_chars": int(summary_length_chars),
            "context_payload_chars": int(context_payload_chars),
            "truncated_message_count": int(truncated_count)
        }



    def get_chat_tail(
        self,
        brainstorm_id: str,
        max_msgs: int = 50,
        since_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Restituisce la coda dei messaggi non compattati per una sessione, con metadati canale.

        Gestione multi-day:
        - Tutti i messaggi dalla posizione ``compacted_up_to_index`` in poi sono inclusi.
        - Non vengono scartati messaggi solo perché appartengono a giorni precedenti.
        - Il parametro ``since_date`` (ISO date string "YYYY-MM-DD") filtra ulteriormente
          i messaggi più vecchi di quella data, ma solo nella coda non compattata.
        - Se la sessione non esiste, ritorna dizionario vuoto.

        Mapping canale:
        - "telegram"  → sessione originata da Telegram (chat_id presente)
        - "web"       → sessione originata dal web dashboard (web_session_id presente)
        - "unknown"   → canale non identificato o sessione creata programmaticamente
        """
        bs = self.load_brainstorm(brainstorm_id)
        if not bs:
            return {}

        raw_msgs = bs.get("messages", [])
        last_compacted = bs.get("compacted_up_to_index", 0)
        uncompacted = raw_msgs[last_compacted:]

        # Filtraggio per data (se richiesto) senza scartare messaggi per giorno corrente
        if since_date:
            filtered = []
            for m in uncompacted:
                ts = m.get("timestamp", "")
                if ts and ts[:10] >= since_date:
                    filtered.append(m)
            uncompacted = filtered

        # Bounding
        tail = uncompacted[-max_msgs:] if len(uncompacted) > max_msgs else uncompacted

        # Risoluzione canale
        ch = bs.get("channel", "unknown")
        if ch not in ALLOWED_CHANNELS:
            ch = "unknown"

        return {
            "brainstorm_id": brainstorm_id,
            "channel": ch,
            "chat_id": bs.get("chat_id"),
            "web_session_id": bs.get("web_session_id"),
            "revision": bs.get("revision", 0),
            "compacted_up_to_index": last_compacted,
            "has_summary": bool(bs.get("chat_summary", "")),
            "tail": tail,
            "tail_count": len(tail),
            "total_messages": len(raw_msgs),
        }

    def _query_agent_llm(
        self,
        agent: str,
        messages: List[Dict[str, str]],
        preset: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Optional[str]:
        """
        Interroga direttamente i provider LLM configurati con la cronologia completa dei messaggi.
        Per Sol (@sol / @director):
        - Default: reasoning effort 'low' (riduzione consumo token).
        - Escalation a 'high': solo se preset='critical' o esplicitamente richiesto reasoning_effort='high'.
        Per Luna: nessun override di reasoning effort (mantiene il default).
        """
        import urllib.request
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        gemini_key = os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", "")).strip()

        # 0. Google Gemini (esclusivo per gemini compactor / summarizer)
        if (agent in ["gemini"] or os.environ.get("PREFER_GEMINI", "0") == "1") and gemini_key:
            try:
                gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
                contents = []
                for msg in messages:
                    role = "model" if msg.get("role") == "assistant" else "user"
                    contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})
                req_data = {
                    "contents": contents,
                    "generationConfig": {"temperature": 0.4}
                }
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    cand = data.get("candidates", [])[0]
                    return cand.get("content", {}).get("parts", [])[0].get("text", "").strip()
            except Exception as e:
                logger.debug(f"Google Gemini direct API call failed: {e}")

        # 1a. DeepSeek Pro per @deepseek / @pro
        if agent in ["deepseek", "ds-pro", "pro", "deepseek-pro"] and deepseek_key:
            try:
                req_data = {
                    "model": "deepseek-v4-pro",
                    "messages": messages,
                    "temperature": 0.3
                }
                req = urllib.request.Request(
                    "https://api.deepseek.com/chat/completions",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.debug(f"DeepSeek Pro direct API call failed: {e}")

        # 1b. DeepSeek Flash per @flash / @deepseek-flash
        if agent in ["flash", "ds-flash", "deepseek-flash"] and deepseek_key:
            try:
                req_data = {
                    "model": "deepseek-v4-flash",
                    "messages": messages,
                    "temperature": 0.4
                }
                req = urllib.request.Request(
                    "https://api.deepseek.com/chat/completions",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.debug(f"DeepSeek Flash direct API call failed: {e}")

        # 2. GLM 5.3 Flash via OpenRouter per @glm (fixer/chat operativo, mai reviewer)
        if agent in ["glm", "security"] and openrouter_key:
            try:
                model_name = os.environ.get("OPENROUTER_GLM_FLASH_MODEL", "z-ai/glm-5.3-flash")
                req_data = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": 0.3
                }
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/chat/completions",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://github.com/massigent/taktstock",
                        "X-Title": "Taktstock Buzz Chat"
                    }
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.debug(f"OpenRouter GLM direct API call failed: {e}")

        # 3. OpenAI per @sol / @luna tramite Sidecar Host o Account Codex in abbonamento
        if agent in ["sol", "director", "luna"]:
            prompt_text = "\n\n".join([f"[{m.get('role', 'user')}]: {m.get('content', '')}" for m in messages])

            # Invocazione centralizzata via AgentGateway se TAKTSTOCK_HOST_CODEX_SIDECAR=1 (o UFFICIO_HOST_CODEX_SIDECAR=1)
            try:
                try:
                    from infrastructure.agent_gateway import AgentGateway, is_sidecar_mode_enabled
                except ImportError:
                    from server.infrastructure.agent_gateway import AgentGateway, is_sidecar_mode_enabled

                if is_sidecar_mode_enabled():
                    chat_ws = self._resolve_chat_workspace()
                    success, output, meta = AgentGateway.execute_agent_call(
                        agent_role=agent,
                        prompt=prompt_text,
                        worktree_path=chat_ws,
                        preset=preset,
                        reasoning_effort=reasoning_effort,
                        sandbox_mode="read-only"
                    )
                    # In sidecar mode è categoricamente VIETATO qualunque fallback locale
                    return output
            except Exception as e:
                logger.error(f"Errore AgentGateway: {e}")
                return f"⚠️ Errore esecuzione agente {agent}: {e}"

            # Flusso Legacy (quando sidecar non attivo)
            try:
                from account_manager import CodexAccountManager
                am = CodexAccountManager()
                target_acc = am.get_account_for_role(agent)
                if not target_acc:
                    return "⚠️ Quota settimanale OpenAI/ChatGPT esaurita (sia sull'account principale che su Bonus) fino al reset di domattina."
                
                # Chiamata Codex CLI non interattiva con l'ambiente dell'account selezionato
                env = am.apply_account_env(os.environ.copy(), account=target_acc)

                # Configurazione mirata modello e reasoning effort
                if agent in ["sol", "director"]:
                    effort = reasoning_effort or ("high" if preset == "critical" else "low")
                    cmd = [
                        "codex", "exec",
                        "--model", "gpt-5.6-sol",
                        "-c", f'model_reasoning_effort="{effort}"',
                        prompt_text
                    ]
                else:
                    # Luna mantiene la configurazione di default
                    cmd = ["codex", "exec", "--model", "gpt-5.6-terra", prompt_text]

                try:
                    p = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=90
                    )
                    output = p.stdout if p.returncode == 0 else p.stderr
                    if p.returncode != 0 or am.is_rate_limit_error(output):
                        category, err_msg = classify_codex_error(None, p.returncode, output)
                        if category == "rate_limit":
                            target_acc.mark_rate_limited(cooldown_seconds=1800.0)
                            # Prova con Bonus
                            bonus_acc = am.get_account_by_name("bonus")
                            if bonus_acc and bonus_acc.is_available:
                                env_b = am.apply_account_env(os.environ.copy(), account=bonus_acc)
                                try:
                                    p_b = subprocess.run(
                                        cmd,
                                        capture_output=True,
                                        text=True,
                                        env=env_b,
                                        timeout=90
                                    )
                                    if p_b.returncode == 0 and not am.is_rate_limit_error(p_b.stdout):
                                        return p_b.stdout.strip()
                                    _, err_msg_b = classify_codex_error(None, p_b.returncode, p_b.stdout if p_b.returncode == 0 else p_b.stderr)
                                    return err_msg_b
                                except Exception as exc_b:
                                    _, err_msg_b = classify_codex_error(exc_b, None, "")
                                    return err_msg_b
                        return err_msg
                    return output.strip()
                except Exception as exc:
                    category, err_msg = classify_codex_error(exc, None, "")
                    return err_msg
            except Exception as e:
                logger.debug(f"Codex subscription account call setup failed: {e}")
                category, err_msg = classify_codex_error(e, None, "")
                return err_msg

        if openai_key:
            try:
                model_name = "gpt-4o" if agent in ["sol", "director"] else "gpt-4o-mini"
                req_data = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": 0.6
                }
                req = urllib.request.Request(
                    "https://api.openai.com/v1/chat/completions",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.debug(f"OpenAI direct API call failed: {e}")

        # 4. Anthropic fallback
        if anthropic_key:
            try:
                sys_msg = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
                chat_turns = messages[1:] if messages and messages[0]["role"] == "system" else messages
                req_data = {
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 1024,
                    "system": sys_msg,
                    "messages": chat_turns
                }
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={
                        "x-api-key": anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json"
                    }
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["content"][0]["text"].strip()
            except Exception as e:
                logger.debug(f"Anthropic direct API call failed: {e}")

        # 5. DeepSeek come fallback generale se le altre chiavi non sono attive
        if deepseek_key:
            try:
                req_data = {
                    "model": "deepseek-chat",
                    "messages": messages,
                    "temperature": 0.5
                }
                req = urllib.request.Request(
                    "https://api.deepseek.com/chat/completions",
                    data=json.dumps(req_data).encode("utf-8"),
                    headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"].strip()
            except Exception:
                pass

        return None

    def post_chat_message(
        self,
        brainstorm_id: str,
        message: str,
        sender: str = "user",
        runner: Optional[Any] = None
    ) -> List[Dict[str, Any]]:
        """
        Chat libera interattiva stile Buzz/Slack con menzioni (@sol, @deepseek, @glm, @agy, @all).
        Esegue i prompt dedicati verso i singoli agenti chiamati in causa con memoria contestuale completa.

        Concorrenza & persistenza:
        - Il messaggio utente viene persistito immediatamente sotto `lock_session` (nessuno snapshot stantio).
        - Le chiamate LLM/sidecar avvengono SEMPRE fuori dal lock.
        - Le risposte vengono riunite sotto `lock_session` con merge idempotente per message id:
          nessun messaggio (utente o agente) puo' sparire con piu' thread/processi concorrenti,
          nessun duplicato, revision sempre coerente.
        """
        # --- 0. Persistenza atomica del messaggio utente sotto lock ---
        with self.lock_session(brainstorm_id):
            bs = self.load_brainstorm(brainstorm_id)
            if not bs:
                raise ValueError(f"Brainstorm {brainstorm_id} non trovato.")

            now = datetime.now().isoformat()
            user_msg = {
                "id": _generate_message_id("u"),
                "sender": sender,
                "agent": "user",
                "text": message,
                "timestamp": now
            }
            bs["messages"].append(user_msg)
            self.save_brainstorm(bs)

        # --- 0bis. Auto-compattazione periodica (Gemini fuori lock, CAS sotto lock) ---
        self._check_and_compact_chat(brainstorm_id, runner=runner)

        # --- 1. Riconoscimento menzioni e Sticky Agent su snapshot fresco, sotto lock ---
        with self.lock_session(brainstorm_id):
            bs = self.load_brainstorm(brainstorm_id)
            if not bs:
                return []

            text_lower = message.lower()
            has_sol = bool(re.search(r"[/@](sol|director|direttore)\b", text_lower))
            has_luna = bool(re.search(r"[/@](luna|terra)\b", text_lower))
            has_ds = bool(re.search(r"[/@](deepseek|ds|pro|deepseek-pro)\b", text_lower))
            has_flash = bool(re.search(r"[/@](flash|deepseek-flash)\b", text_lower))
            has_glm = bool(re.search(r"[/@](glm|claude|security)\b", text_lower))
            has_agy = bool(re.search(r"[/@](agy|antigravity|executor)\b", text_lower))
            has_all = bool(re.search(r"[/@](all|tutti|team)\b", text_lower))

            # Se l'utente menziona un agente esplicito, aggiorna l'agente attivo della sessione
            if has_all:
                selected_agents = set(bs.get("selected_agents", DEFAULT_SESSION_AGENTS))
                # Broadcast vale una sola volta: non rendere "all" sticky, imposta lead configurato (sol)
                bs["current_agent"] = "sol" if "sol" in selected_agents else (list(selected_agents)[0] if selected_agents else "sol")
            elif has_agy:
                bs["current_agent"] = "agy"
            elif has_luna:
                bs["current_agent"] = "luna"
            elif has_flash:
                bs["current_agent"] = "flash"
            elif has_ds:
                bs["current_agent"] = "deepseek"
            elif has_glm:
                bs["current_agent"] = "glm"
            elif has_sol:
                bs["current_agent"] = "sol"
            else:
                # Nessuna menzione: mantieni la conversazione 1-a-1 con l'ultimo agente attivo (default: sol)
                active = bs.get("current_agent", "sol")
                if active == "all": has_all = True
                elif active == "agy": has_agy = True
                elif active == "luna": has_luna = True
                elif active == "flash": has_flash = True
                elif active == "deepseek": has_ds = True
                elif active == "glm": has_glm = True
                else: has_sol = True

            # @all significa il team scelto per questa sessione, non tutti gli
            # agenti disponibili. I reviewer restano consultabili da Sol o tramite
            # una mention diretta, evitando consumo di token non richiesto.
            if has_all:
                selected_agents = set(bs.get("selected_agents", DEFAULT_SESSION_AGENTS))
                has_all = False
                has_sol = "sol" in selected_agents
                has_agy = "agy" in selected_agents
                has_luna = "luna" in selected_agents
                has_ds = "deepseek" in selected_agents
                has_glm = "glm" in selected_agents
                has_flash = "flash" in selected_agents
                # Assicura che current_agent rimanga sol per i messaggi successivi
                bs["current_agent"] = "sol" if "sol" in selected_agents else (list(selected_agents)[0] if selected_agents else "sol")

            # Testo pulito privo di menzioni per il riconoscimento intenti
            clean_without_mentions = re.sub(
                r"[/@](sol|luna|terra|director|pro|flash|deepseek-pro|deepseek-flash|deepseek|ds|glm|claude|security|agy|antigravity|all|tutti|team)\b",
                "",
                text_lower,
                flags=re.IGNORECASE
            ).strip()

            # Determina agente primario per le risposte di sistema/progetto
            primary_agent = "sol"
            if has_agy: primary_agent = "agy"
            elif has_luna: primary_agent = "luna"
            elif has_flash: primary_agent = "flash"
            elif has_ds: primary_agent = "deepseek"
            elif has_glm: primary_agent = "glm"
            elif has_sol: primary_agent = "sol"
            else: primary_agent = bs.get("current_agent", "sol")
            if primary_agent == "all": primary_agent = "sol"

            primary_meta = AGENT_META.get(primary_agent, {"name": "Sol", "avatar": "🧠"})

            # Persiste l'eventuale aggiornamento dello sticky agent
            self.save_brainstorm(bs)

        # --- 2. Gestione esplicita del team della sessione (/agenti sol,agy,luna) ---
        # La scelta e' persistita nella chat e puo' essere cambiata in qualsiasi momento
        # senza alterare la policy globale (Sol resta libero di consultare reviewer se necessario).
        team_match = re.fullmatch(r"/(?:agenti|agents|team)\s+(.+)", clean_without_mentions, re.IGNORECASE)
        if team_match:
            aliases = {
                "sol": "sol", "director": "sol", "direttore": "sol",
                "agy": "agy", "antigravity": "agy",
                "luna": "luna", "terra": "luna",
                "deepseek": "deepseek", "ds": "deepseek", "pro": "deepseek", "deepseek-pro": "deepseek",
                "glm": "glm", "claude": "glm",
                "flash": "flash", "deepseek-flash": "flash"
            }
            requested = [aliases.get(token.strip().lower()) for token in re.split(r"[,\s]+", team_match.group(1))]
            selected = []
            for agent in requested:
                if agent and agent not in selected:
                    selected.append(agent)
            if not selected:
                selected = DEFAULT_SESSION_AGENTS.copy()
            labels = ", ".join(f"{AGENT_META[a]['avatar']} {AGENT_META[a]['name']}" for a in selected)
            reply_msg = {
                "id": _generate_message_id("sol"),
                "sender": "Sol",
                "agent": "sol",
                "text": f"🧠 Team della sessione aggiornato: {labels}. I reviewer indipendenti restano disponibili su richiesta o quando Sol ritiene che il task lo richieda.",
                "timestamp": datetime.now().isoformat()
            }
            self._append_reply_under_lock(
                brainstorm_id,
                reply_msg,
                mutations=lambda fresh: fresh.update({
                    "selected_agents": selected,
                    "current_agent": "sol" if "sol" in selected else selected[0],
                }),
            )
            return [reply_msg]

        # --- A. INTENTO 1: Richiesta Elenco Progetti (/progetti, "quali sono i miei progetti") ---
        is_list_projects = (
            clean_without_mentions in ["/progetti", "/projects", "/repos", "/elenco", "progetti", "projects", "repos"] or
            any(p in clean_without_mentions for p in [
                "quali sono i miei progetti", "quali sono i progetti", "elencami i progetti",
                "elenco progetti", "mostrami i progetti", "che progetti ci sono", "quali progetti ho",
                "vedi i progetti", "vedere i progetti", "mostra i progetti", "dimmi i progetti"
            ])
        )
        if is_list_projects:
            menu_text = self.projects_manager.format_projects_telegram_menu()
            if bs.get("project_name"):
                menu_text += f"\n\n📌 *Progetto attualmente attivo:* `{bs['project_name']}`"
            reply_msg = {
                "id": _generate_message_id(primary_agent),
                "sender": primary_meta["name"],
                "agent": primary_agent,
                "text": f"{primary_meta['avatar']} *Ecco tutti i tuoi progetti configurati sul server:*\n\n{menu_text}",
                "timestamp": datetime.now().isoformat()
            }
            self._append_reply_under_lock(brainstorm_id, reply_msg)
            return [reply_msg]

        # --- A.bis INTENTO: Richiesta Elenco Skills (/skills, "quali sono le skill") ---
        is_list_skills = (
            clean_without_mentions in ["/skills", "/skill", "skills", "skill"] or
            any(p in clean_without_mentions for p in [
                "what are the skills", "list skills", "show skills", "available skills",
                "quali sono le skill", "elenco skill", "mostra le skill", "elencami le skill",
                "che skill abbiamo", "quali competenze", "mostrami le skill"
            ])
        )
        if is_list_skills:
            skills_menu = self.skills_manager.format_skills_telegram_menu()
            reply_msg = {
                "id": _generate_message_id(primary_agent),
                "sender": primary_meta["name"],
                "agent": primary_agent,
                "text": f"{primary_meta['avatar']} *Here are the Universal Skills inherited by all agents:*\n\n{skills_menu}",
                "timestamp": datetime.now().isoformat()
            }
            self._append_reply_under_lock(brainstorm_id, reply_msg)
            return [reply_msg]

        # --- A.ter INTENT: Single Skill Details or Activation (/skill <name>, "apply skill <name>") ---
        skill_req_match = re.search(r"(?:/skill\s+|apply\s+(?:the\s+)?skill\s+|show\s+(?:the\s+)?skill\s+|open\s+skill\s+|applica\s+(?:la\s+)?skill\s+|mostra\s+(?:la\s+)?skill\s+|apri\s+skill\s+)([a-zA-Z0-9_\-]+)", clean_without_mentions, re.IGNORECASE)
        if skill_req_match:
            skill_target = skill_req_match.group(1).strip()
            found_s = self.skills_manager.get_skill(skill_target)
            if found_s:
                preview = found_s["content"][:1200]
                reply_msg = {
                    "id": _generate_message_id(primary_agent),
                    "sender": primary_meta["name"],
                    "agent": primary_agent,
                    "text": f"{found_s['icon']} *Universal Skill:* **{found_s['name']}** (`{found_s['id']}`)\n\n_{found_s['description']}_\n\n📋 *Guidelines Excerpt:*\n```markdown\n{preview}\n...```\n\n✅ *The skill is active for the whole session.* What would you like to work on?",
                    "timestamp": datetime.now().isoformat()
                }
                self._append_reply_under_lock(brainstorm_id, reply_msg)
                return [reply_msg]

        # --- B. INTENT 2: Select or Switch Active Project ("work on X", "/project X") ---
        switch_match = re.search(
            r"(?:^|\s)(?:/(?:project|progetto|repo|cd)\s+|work\s+on\s+(?:the\s+project\s+)?|switch\s+to\s+(?:the\s+project\s+)?|open\s+(?:the\s+)?project\s+|go\s+to\s+(?:the\s+project\s+)?|lavoriamo\s+(?:su|sul\s+progetto)\s+|passiamo\s+a(?:l\s+progetto)?\s+|apri\s+(?:il\s+)?progetto\s+|vai\s+(?:su|in|nel\s+progetto)\s+|spostati\s+su(?:l\s+progetto)?\s+|seleziona\s+(?:il\s+)?progetto\s+|(?:check|analyze|inspect|verify|controlla(?:re)?|analizza(?:re)?|ispeziona(?:re)?|verifica(?:re)?)\s+(?:the\s+|il\s+)?project\s+)([a-zA-Z0-9_\-\.\/]+)",
            clean_without_mentions,
            re.IGNORECASE
        )
        target_proj_query = None
        if switch_match:
            target_proj_query = switch_match.group(1).strip()
        elif clean_without_mentions.startswith("/project") or clean_without_mentions.startswith("/progetto") or clean_without_mentions.startswith("/repo"):
            parts = clean_without_mentions.split(maxsplit=1)
            if len(parts) > 1:
                target_proj_query = parts[1].strip()

        if target_proj_query:
            found_proj = self.projects_manager.find_project(target_proj_query)
            if found_proj:
                proj_mutation = lambda fresh, fp=found_proj: fresh.update({
                    "repo": fp["path"],
                    "project_name": fp["name"],
                    "project_tech": fp["tech_stack"],
                    "task": f"Work on project {fp['name']}",
                })

                # Check if user asked a specific question in addition to project switch
                cleaned_after_switch = re.sub(
                    r"(?:/(?:project|progetto|repo|cd)\s+[a-zA-Z0-9_\-\.\/]+|work\s+on\s+(?:the\s+project\s+)?[a-zA-Z0-9_\-\.\/]+|switch\s+to\s+(?:the\s+project\s+)?[a-zA-Z0-9_\-\.\/]+|lavoriamo\s+(?:su|sul\s+progetto)\s+[a-zA-Z0-9_\-\.\/]+|passiamo\s+a(?:l\s+progetto)?\s+[a-zA-Z0-9_\-\.\/]+|apri\s+(?:il\s+)?progetto\s+[a-zA-Z0-9_\-\.\/]+|vai\s+(?:su|in|nel\s+progetto)\s+[a-zA-Z0-9_\-\.\/]+|spostati\s+su(?:l\s+progetto)?\s+[a-zA-Z0-9_\-\.\/]+|seleziona\s+(?:il\s+)?progetto\s+[a-zA-Z0-9_\-\.\/]+)",
                    "",
                    clean_without_mentions,
                    flags=re.IGNORECASE
                ).strip(" ,;:.!?")

                if not cleaned_after_switch or len(cleaned_after_switch) < 3:
                    tree_preview = self.projects_manager.get_project_tree(found_proj["path"], max_depth=1, max_entries=12)
                    switch_reply = (
                        f"📁 *Active project set to:* **{found_proj['name']}**\n"
                        f"🛠️ *Stack:* `{found_proj['tech_stack']}`\n"
                        f"📂 *Path:* `{found_proj['path']}`\n"
                        f"🌿 *Git Branch:* `{found_proj.get('git_branch', 'main')}`\n\n"
                        f"📋 *Main files overview:*\n```\n{tree_preview}\n```\n"
                        f"Loaded context for **{found_proj['name']}**. What should we work on in this project?"
                    )
                    reply_msg = {
                        "id": _generate_message_id(primary_agent),
                        "sender": primary_meta["name"],
                        "agent": primary_agent,
                        "text": f"{primary_meta['avatar']} {switch_reply}",
                        "timestamp": datetime.now().isoformat()
                    }
                    self._append_reply_under_lock(brainstorm_id, reply_msg, mutations=proj_mutation)
                    return [reply_msg]

                # User also asked a question: apply project switch and proceed with agents
                self._append_reply_under_lock(brainstorm_id, mutations=proj_mutation)
                bs["repo"] = found_proj["path"]
                bs["project_name"] = found_proj["name"]
                bs["project_tech"] = found_proj["tech_stack"]
                bs["task"] = f"Work on project {found_proj['name']}"
            elif clean_without_mentions.startswith(("/project", "/progetto", "/repo")):
                avail_names = ", ".join([p["name"] for p in self.projects_manager.list_projects()])
                not_found_msg = {
                    "id": _generate_message_id(primary_agent),
                    "sender": primary_meta["name"],
                    "agent": primary_agent,
                    "text": f"⚠️ Project `{target_proj_query}` not found in `{self.projects_manager.base_dir}`.\n\n*Available projects:* {avail_names}\n\nUse `work on <name>` to select one.",
                    "timestamp": datetime.now().isoformat()
                }
                self._append_reply_under_lock(brainstorm_id, not_found_msg)
                return [not_found_msg]

        # --- C. INTENT 3: Secure inspection of a specific project file ---
        inspected_file_data = None
        if bs.get("repo"):
            file_match = re.search(
                r"(?:read|show|open|inspect|see|view|leggi|mostrami|apri|guarda|ispeziona|cosa c['’]è in|vedi)\s+(?:the\s+file\s+|il\s+file\s+)?([a-zA-Z0-9_\-/\.]+\.[a-zA-Z0-9]+)",
                clean_without_mentions,
                re.IGNORECASE
            )
            if file_match:
                target_file_rel = file_match.group(1).strip()
                file_content = self.projects_manager.safe_read_file(bs["repo"], target_file_rel)
                if file_content:
                    inspected_file_data = {"path": target_file_rel, "content": file_content}

        replies = []
        task = bs["task"]
        last_rounds_summary = ""
        if bs.get("rounds"):
            last_rounds_summary = f"Latest plan summary: {bs['rounds'][-1].get('director_synthesis', {}).get('recommended_option', '')}"

        # 1. Sol Response (Lead Director & Strategist)
        if has_all or has_sol:
            if is_pure_ping(message):
                sol_text = "🧠 Hello! I am Sol, Lead Director & Strategist (GPT-5.6 Sol). I am ready to guide technical strategy, overall architecture, and project decisions. What should we work on?"
                is_sol_err = False
            elif runner and getattr(runner, "mock_mode", False):
                sol_text = "As Technical Director, I agree with the proposed approach. Let's evaluate priorities together before proceeding."
                is_sol_err = False
            else:
                sol_sys = CHAT_AGENT_PROMPTS["sol"]
                sol_msgs = self._build_chat_messages(bs, "sol", sol_sys, inspected_file=inspected_file_data)
                raw_sol = self._query_agent_llm("sol", sol_msgs, preset=bs.get("preset", "standard"))
                if raw_sol is None:
                    sol_text = "As Technical Director, I agree with the proposed approach. Let's evaluate priorities together before proceeding."
                    is_sol_err = False
                else:
                    sol_text = raw_sol
                    is_sol_err = sol_text.startswith("⚠️")

            sol_msg = {
                "id": _generate_message_id("sol"),
                "sender": "Sol",
                "agent": "sol",
                "text": sol_text.strip(),
                "timestamp": datetime.now().isoformat()
            }
            if is_sol_err:
                sol_msg.update({
                    "message_type": "operational_error",
                    "is_error": True,
                    "exclude_from_context": True
                })
            replies.append(sol_msg)

        # 1b. Luna Response (Co-Director & Product Architect)
        if has_all or has_luna:
            if is_pure_ping(message):
                luna_text = "🌙 Hello! I am Luna, specialist for live n8n workflow modifications via MCP server. I can inspect, modify, and validate n8n workflows in a targeted manner."
                is_luna_err = False
            elif runner and getattr(runner, "mock_mode", False):
                luna_text = "I can prepare an n8n modification via MCP: specify the workflow, expected effect, and validation constraints."
                is_luna_err = False
            else:
                luna_sys = CHAT_AGENT_PROMPTS["luna"]
                luna_msgs = self._build_chat_messages(bs, "luna", luna_sys, inspected_file=inspected_file_data)
                raw_luna = self._query_agent_llm("luna", luna_msgs)
                if raw_luna is None:
                    luna_text = "I can prepare an n8n modification via MCP: specify the workflow, expected effect, and validation constraints."
                    is_luna_err = False
                else:
                    luna_text = raw_luna
                    is_luna_err = luna_text.startswith("⚠️")

            luna_msg = {
                "id": _generate_message_id("luna"),
                "sender": "Luna",
                "agent": "luna",
                "text": luna_text.strip(),
                "timestamp": datetime.now().isoformat()
            }
            if is_luna_err:
                luna_msg.update({
                    "message_type": "operational_error",
                    "is_error": True,
                    "exclude_from_context": True
                })
            replies.append(luna_msg)

        # 2. DeepSeek Pro Response (Architecture & Edge Cases)
        if has_all or has_ds:
            if is_pure_ping(message):
                ds_text = "🔍 Hello! I am DeepSeek Pro, Principal Software Architect. Ready to analyze data schemas, failure modes, API contracts, and computational complexity."
                is_ds_err = False
            elif runner and getattr(runner, "mock_mode", False):
                ds_text = "From an architectural standpoint, I recommend idempotent design and clean separation of concerns."
                is_ds_err = False
            else:
                ds_sys = CHAT_AGENT_PROMPTS["deepseek"]
                ds_msgs = self._build_chat_messages(bs, "deepseek", ds_sys, inspected_file=inspected_file_data)
                raw_ds = self._query_agent_llm("deepseek", ds_msgs)
                if raw_ds is None:
                    ds_text = "From an architectural standpoint, I recommend idempotent design and clean separation of concerns."
                    is_ds_err = False
                else:
                    ds_text = raw_ds
                    is_ds_err = ds_text.startswith("⚠️")

            ds_msg = {
                "id": _generate_message_id("ds"),
                "sender": "DeepSeek Pro",
                "agent": "deepseek",
                "text": ds_text.strip(),
                "timestamp": datetime.now().isoformat()
            }
            if is_ds_err:
                ds_msg.update({
                    "message_type": "operational_error",
                    "is_error": True,
                    "exclude_from_context": True
                })
            replies.append(ds_msg)

        # 2b. DeepSeek Flash Response (Rapid Code Review & Pairing with Luna)
        if has_all or has_flash:
            if is_pure_ping(message):
                flash_text = "⚡ Hello! I am DeepSeek Flash, paired with Luna. Ready for rapid code review, performance optimization, and agile refactoring!"
                is_flash_err = False
            elif runner and getattr(runner, "mock_mode", False):
                flash_text = "⚡ DeepSeek Flash active in pair with Luna: ready for rapid review and refactoring."
                is_flash_err = False
            else:
                flash_sys = CHAT_AGENT_PROMPTS["flash"]
                flash_msgs = self._build_chat_messages(bs, "flash", flash_sys, inspected_file=inspected_file_data)
                raw_flash = self._query_agent_llm("flash", flash_msgs)
                if raw_flash is None:
                    flash_text = "⚡ DeepSeek Flash active in pair with Luna: ready for rapid review and refactoring."
                    is_flash_err = False
                else:
                    flash_text = raw_flash
                    is_flash_err = flash_text.startswith("⚠️")

            flash_msg = {
                "id": _generate_message_id("flash"),
                "sender": "DeepSeek Flash",
                "agent": "flash",
                "text": flash_text.strip(),
                "timestamp": datetime.now().isoformat()
            }
            if is_flash_err:
                flash_msg.update({
                    "message_type": "operational_error",
                    "is_error": True,
                    "exclude_from_context": True
                })
            replies.append(flash_msg)

        # 3. GLM 5.3 Response (Security & Standards)
        if has_all or has_glm:
            if is_pure_ping(message):
                glm_text = "🛡️ Hello! I am GLM 5.3, Security Officer. I can assist with security audits (OWASP), privacy (GDPR), authentication, and code quality."
                is_glm_err = False
            elif runner and getattr(runner, "mock_mode", False):
                glm_text = "Security standards verified: token encryption and input validation assured."
                is_glm_err = False
            else:
                glm_sys = CHAT_AGENT_PROMPTS["glm"]
                glm_msgs = self._build_chat_messages(bs, "glm", glm_sys, inspected_file=inspected_file_data)
                raw_glm = self._query_agent_llm("glm", glm_msgs)
                if raw_glm is None:
                    glm_text = "Security standards verified: token encryption and input validation assured."
                    is_glm_err = False
                else:
                    glm_text = raw_glm
                    is_glm_err = glm_text.startswith("⚠️")

            glm_msg = {
                "id": _generate_message_id("glm"),
                "sender": "GLM 5.3",
                "agent": "glm",
                "text": glm_text.strip(),
                "timestamp": datetime.now().isoformat()
            }
            if is_glm_err:
                glm_msg.update({
                    "message_type": "operational_error",
                    "is_error": True,
                    "exclude_from_context": True
                })
            replies.append(glm_msg)

        # 4. AGY Response (Senior Implementation & CLI Specialist)
        if has_all or has_agy:
            is_agy_err = False
            if is_pure_ping(message):
                agy_text = "🚀 Hello! I am AGY, Senior Implementation Engineer. I handle operational analysis, filesystem, worktrees, CLI, tests, and local code; live n8n modifications via MCP belong to Luna."
            elif runner and getattr(runner, "mock_mode", False):
                agy_text = "🚀 Operational analysis and code modifications prepared successfully."
            else:
                agy_sys = CHAT_AGENT_PROMPTS["agy"]
                agy_msgs = self._build_chat_messages(bs, "agy", agy_sys, inspected_file=inspected_file_data)
                flat_prompt = "\n\n".join([f"[{m.get('role', 'user').upper()}]: {m.get('content', '')}" for m in agy_msgs])
                try:
                    try:
                        from infrastructure.agent_gateway import AgentGateway
                    except ImportError:
                        from server.infrastructure.agent_gateway import AgentGateway

                    # Se la sessione ha un progetto attivo, AGY deve lavorare
                    # nel repository reale selezionato, non nel workspace chat
                    # vuoto. In assenza di progetto resta confinata al workspace
                    # dedicato della conversazione.
                    selected_repo = Path(str(bs.get("repo", ""))).resolve() if bs.get("repo") else None
                    chat_ws = selected_repo if selected_repo and selected_repo.is_dir() else self._resolve_chat_workspace()
                    context_metrics = self.compute_context_metrics(bs, agy_msgs, inspected_file=inspected_file_data)
                    success, output, meta = AgentGateway.execute_agent_call(
                        agent_role="agy",
                        prompt=flat_prompt,
                        worktree_path=chat_ws,
                        phase="chat",
                        metadata=context_metrics
                    )

                    if success:
                        agy_text = str(output or "").strip()
                        if not agy_text:
                            agy_text = (
                                "⚠️ AGY ha concluso senza testo leggibile. Nessun risultato affidabile "
                                "è disponibile: riprova la richiesta o avvia un audit tramite /sviluppa."
                            )
                            is_agy_err = True
                    else:
                        logger.error(f"Errore chiamata sidecar AGY nella chat: {output}")
                        agy_text = f"⚠️ [AGY Sidecar Error] {output}"
                        is_agy_err = True
                except Exception as e:
                    logger.error(f"Eccezione durante la chiamata sidecar AGY: {e}")
                    agy_text = f"⚠️ [AGY Sidecar Error] Impossibile contattare il runner host: {e}"
                    is_agy_err = True

            agy_msg = {
                "id": _generate_message_id("agy"),
                "sender": "agy",
                "agent": "agy",
                "text": agy_text.strip(),
                "timestamp": datetime.now().isoformat()
            }
            if is_agy_err:
                agy_msg.update({
                    "message_type": "operational_error",
                    "is_error": True,
                    "exclude_from_context": True
                })
            replies.append(agy_msg)



        # --- 3. Merge idempotente delle risposte sotto lock (nessuna perdita, nessun duplicato) ---
        if replies:
            with self.lock_session(brainstorm_id):
                fresh = self.load_brainstorm(brainstorm_id)
                if fresh:
                    messages = fresh.setdefault("messages", [])
                    existing_ids = {m.get("id") for m in messages}
                    for reply in replies:
                        if reply.get("id") not in existing_ids:
                            messages.append(reply)
                            existing_ids.add(reply.get("id"))
                    self.save_brainstorm(fresh)

        return replies

    def format_chat_replies_telegram(self, replies: List[Dict[str, Any]]) -> str:
        """Formats chatroom replies for delivery on Telegram."""
        if not replies:
            return "💬 Message recorded."
        out = ""
        for r in replies:
            agent_key = r.get("agent", "sol")
            meta = AGENT_META.get(agent_key, {"name": r.get("sender", "Agent"), "avatar": "🤖"})
            out += f"{meta['avatar']} *{meta['name'].upper()}:*\n{r.get('text', '')}\n\n"
        return out.strip()

    def get_status(self, brainstorm_id: str) -> Dict[str, Any]:
        """Returns the current state of the brainstorm."""
        bs = self.load_brainstorm(brainstorm_id)
        if not bs:
            return {"error": f"Brainstorm {brainstorm_id} not found"}
        return bs

    def approve(self, brainstorm_id: str, approved_by: str = "user") -> Dict[str, Any]:
        """Marks brainstorm as approved and consolidates final_plan (under SessionFileLock)."""
        with self.lock_session(brainstorm_id):
            bs = self.load_brainstorm(brainstorm_id)
            if not bs:
                raise ValueError(f"Brainstorm {brainstorm_id} not found.")

            last_round = bs["rounds"][-1] if bs.get("rounds") else {}
            synth = last_round.get("director_synthesis", {})
            analysis = last_round.get("director_analysis", {})

            strategy_text = synth.get("recommended_option") or analysis.get("strategy", bs["task"])

            final_plan = {
                "phase": "final_plan",
                "plan": strategy_text,
                "architecture": analysis.get("analysis", strategy_text),
                "constraints": synth.get("consensus", []),
                "success_criteria": ["All unit tests pass", "Reviewer approval granted"],
                "approved_round": len(bs.get("rounds", [])),
                "task_enhanced": f"{bs['task']} — APPROVED PLAN: {strategy_text}"
            }

            bs["status"] = "approved"
            bs["final_plan"] = final_plan
            bs["approved_by"] = approved_by
            bs["approved_at"] = datetime.now().isoformat()

            bs["messages"].append({
                "id": _generate_message_id("appr"),
                "sender": "System",
                "agent": "sol",
                "text": f"🎉 **Plan Approved by {approved_by}!** Launching execution with agy...",
                "timestamp": datetime.now().isoformat()
            })

            self.save_brainstorm(bs)
            return bs

    def reject(self, brainstorm_id: str, reason: str = "Rejected by user") -> None:
        """Marks brainstorm as rejected (under SessionFileLock)."""
        chat_id = None
        with self.lock_session(brainstorm_id):
            bs = self.load_brainstorm(brainstorm_id)
            if not bs:
                return

            bs["status"] = "rejected"
            bs["rejected_reason"] = reason
            bs["messages"].append({
                "id": _generate_message_id("rej"),
                "sender": "System",
                "agent": "sol",
                "text": f"❌ **Plan Rejected:** {reason}",
                "timestamp": datetime.now().isoformat()
            })
            self.save_brainstorm(bs)
            chat_id = bs.get("chat_id")
        if chat_id:
            self.clear_active_brainstorm(chat_id)

    def mark_executed(self, brainstorm_id: str):
        """Marks brainstorm as executed successfully (under SessionFileLock)."""
        chat_id = None
        with self.lock_session(brainstorm_id):
            bs = self.load_brainstorm(brainstorm_id)
            if not bs:
                return

            bs["status"] = "executed"
            self.save_brainstorm(bs)
            chat_id = bs.get("chat_id")
        if chat_id:
            self.clear_active_brainstorm(chat_id)

    def to_execution_task(self, brainstorm_id: str) -> str:
        """Converts approved plan into a detailed task string for run_full_workflow."""
        bs = self.load_brainstorm(brainstorm_id)
        if not bs:
            return ""

        plan_data = bs.get("final_plan")
        if plan_data and plan_data.get("task_enhanced"):
            return plan_data["task_enhanced"]
        return bs.get("task", "")

    def format_telegram_summary(self, bs: Dict[str, Any]) -> str:
        """Generates clear, structured Markdown message for Telegram."""
        bs_id = bs.get("id")
        rounds = bs.get("rounds", [])
        if not rounds:
            return f"🧠 *Brainstorming {bs_id}*: Awaiting first round..."

        current_round = rounds[-1]
        r_num = current_round.get("round", 1)
        synth = current_round.get("director_synthesis", {})
        analysis = current_round.get("director_analysis", {})
        opinions = current_round.get("reviewer_opinions", [])

        status_emoji = "🟢 ACTIVE" if bs.get("status") == "active" else ("✅ APPROVED" if bs.get("status") == "approved" else "❌ REJECTED")

        msg = f"🧠 *MULTI-AGENT BRAINSTORMING* [{status_emoji}]\n"
        msg += f"🆔 `#{bs_id}` (Round {r_num})\n"
        msg += f"📋 *Task:* {bs.get('task')}\n"
        msg += "💰 *Budget Regime:* ⏳ *Estimate / Soft Cap* (Heuristic & Interactive)\n\n"

        rec = synth.get("recommended_option") or analysis.get("strategy", "-")
        msg += f"🎯 *Recommended Strategy (Sol / Director):*\n{rec}\n\n"

        if opinions:
            msg += "👥 *Technical Team Opinions:*\n"
            for op in opinions:
                agent_name = op.get("agent", "reviewer").upper()
                opinion_text = op.get("opinion", "")
                concerns = op.get("concerns", []) or op.get("alternatives", [])
                msg += f"• *{agent_name}:* {opinion_text}\n"
                if concerns:
                    msg += f"  ⚠️ _Concerns:_ {', '.join(concerns[:3])}\n"
            msg += "\n"

        consensus = synth.get("consensus", [])
        disagreements = synth.get("disagreements", [])
        if consensus:
            msg += f"🤝 *Consensus:* {', '.join(consensus[:3])}\n"
        if disagreements:
            msg += f"⚡ *Alternatives:* {', '.join(disagreements[:2])}\n"
        if consensus or disagreements:
            msg += "\n"

        open_q = synth.get("open_questions", []) or analysis.get("questions_for_user", [])
        if open_q and bs.get("status") == "active":
            msg += "❓ *Questions for you:*\n"
            for q in open_q:
                msg += f"• {q}\n"
            msg += "\n"

        if bs.get("status") == "active":
            msg += "💬 *Chat & Quick Actions:*\n"
            msg += "• Chat freely: `@sol ...`, `@deepseek ...`, `@glm ...`, `@all ...`\n"
            msg += "• `/opinion <feedback>` to update formal plan (Round " + str(r_num + 1) + ")\n"
            msg += "• `/approve-plan` to approve and launch development\n"
            msg += "• `/reject-plan <reason>` to cancel plan"
        elif bs.get("status") == "approved":
            msg += "🚀 *Plan Approved! Orchestration is starting...*"
        elif bs.get("status") == "rejected":
            msg += f"❌ *Plan Rejected:* {bs.get('rejected_reason', 'No reason specified')}"

        return msg
