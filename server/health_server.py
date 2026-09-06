#!/usr/bin/env python3
"""
Taktstock Web Dashboard & Buzz Multi-Agent Chat Server
------------------------------------------------------
Lightweight standalone HTTP server (zero external dependencies, Python standard library) providing:
- Interactive Web Dashboard (Dark Mode, visual, responsive, auto-refresh) on `GET /`
- Visual Diff Viewer on `GET /diff/<filename.html>`
- Agent status monitoring, Codex accounts, worktrees, and metrics
- Real-time Buzz Multi-Agent Chat interface (@sol, @deepseek, @glm, @agy, @all)
- Hardened authentication (Bearer Token, Header X-Taktstock-Token, Cookie HMAC-SHA256 HttpOnly)
- Endpoints for secure remote task execution via webhook/API
"""

import os
import sys
import json
import re
import time
import hmac
import hashlib
import http.cookies
import base64
import uuid
import logging
import urllib.parse
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List, Optional, Tuple, Set
from datetime import datetime

from brainstorm_manager import BrainstormManager, AGENT_META

PORT = int(os.environ.get("TAKTSTOCK_HEALTH_PORT") or os.environ.get("UFFICIO_HEALTH_PORT", "8765"))
SERVER_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or os.environ.get("ORCH_HOME") or (Path.home() / "taktstock"))
STATE_DIR = BASE_DIR / "state"
REPOS_DIR = BASE_DIR / "repos"
WORKTREES_DIR = BASE_DIR / "worktrees"
DIFFS_DIR = BASE_DIR / "diffs"
BRAINSTORMS_DIR = STATE_DIR / "brainstorms"

MIN_AUTH_TOKEN_LENGTH = 16
AUTH_SESSION_COOKIE_NAME = "taktstock_session"
AUTH_SESSION_DURATION_SECONDS = 2592000  # 30 giorni
MAX_POST_BODY_BYTES = 10 * 1024 * 1024   # 10 MB

logger = logging.getLogger("TaktstockWebDashboard")

def get_auth_secret(enforce_validity: bool = False) -> str:
    """
    Retrieves and validates the Taktstock secret authentication token.
    If enforce_validity is True (e.g. at server startup), raises ValueError if missing or < 16 characters.
    """
    auth_secret = (
        os.environ.get("TAKTSTOCK_AUTH_TOKEN")
        or os.environ.get("UFFICIO_AUTH_TOKEN")
        or os.environ.get("UFFICO_AUTH_TOKEN")
        or os.environ.get("DASHBOARD_PASSWORD", "")
    ).strip()
    if enforce_validity:
        if not auth_secret:
            raise ValueError(
                "Missing environment variable TAKTSTOCK_AUTH_TOKEN (or UFFICO_AUTH_TOKEN) (mancante). "
                "A secret authentication token is required to start the server."
            )
        if len(auth_secret) < MIN_AUTH_TOKEN_LENGTH:
            raise ValueError(
                f"TAKTSTOCK_AUTH_TOKEN is too weak (troppo debole: {len(auth_secret)} characters). "
                f"A token of at least {MIN_AUTH_TOKEN_LENGTH} characters is required."
            )
    return auth_secret

def create_session_cookie_value(auth_secret: str, duration_seconds: int = AUTH_SESSION_DURATION_SECONDS) -> str:
    """Generates a session cookie with expiration timestamp and HMAC-SHA256 signature."""
    expiry_ts = int(time.time()) + duration_seconds
    data_to_sign = str(expiry_ts).encode("utf-8")
    sig = hmac.new(auth_secret.encode("utf-8"), data_to_sign, hashlib.sha256).hexdigest()
    return f"{expiry_ts}.{sig}"

def verify_session_cookie_value(cookie_val: str, auth_secret: str) -> bool:
    """Verifies HMAC signature and expiration of the session cookie."""
    if not cookie_val or not auth_secret or len(auth_secret) < MIN_AUTH_TOKEN_LENGTH:
        return False
    if "." not in cookie_val:
        return False
    parts = cookie_val.split(".", 1)
    if len(parts) != 2:
        return False
    exp_str, sig = parts
    try:
        exp_ts = int(exp_str)
    except ValueError:
        return False

    current_ts = int(time.time())
    if current_ts > exp_ts:
        return False  # Expired cookie

    expected_sig = hmac.new(auth_secret.encode("utf-8"), exp_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)

ALLOWED_API_ACTIONS: Set[str] = {"chat", "brainstorm", "execute_task", "approve", "reject", "clear"}
ALLOWED_API_FIELDS: Set[str] = {"action", "chat_id", "message", "task", "repo", "preset"}
ALLOWED_API_PRESETS: Set[str] = {"standard", "critical", "light", "luna_flash", "sol_pro", "quick", "mechanical"}

def resolve_catalog_repo(repo_input: str) -> Optional[str]:
    """
    Resolves a repository exclusively via the local ProjectsManager catalog.
    Categorically rejects arbitrary URLs (http/https), path-traversal (..),
    or names not registered in the local projects catalog.
    Returns normalized project name or None if invalid.
    """
    if not repo_input or not isinstance(repo_input, str):
        return None
    clean = repo_input.strip()
    if not clean or "://" in clean or ".." in clean:
        return None

    try:
        from projects_manager import ProjectsManager
        pm = ProjectsManager()
        proj = pm.find_project(clean)
        if proj and proj.get("name"):
            p_path = Path(proj["path"]).resolve()
            if p_path.is_dir():
                return proj["name"]
    except Exception as e:
        logger.debug(f"Error resolving project '{repo_input}': {e}")
    return None

def resolve_active_project_for_chat(chat_id: Optional[str]) -> Optional[str]:
    """Resolves the selected project in the chat session, without implicit fallbacks."""
    if not chat_id:
        return None
    try:
        manager = BrainstormManager(BRAINSTORMS_DIR)
        brainstorm_id = manager.get_active_brainstorm_id(chat_id=chat_id)
        brainstorm = manager.load_brainstorm(brainstorm_id) if brainstorm_id else None
        if not brainstorm:
            return None
        return resolve_catalog_repo(brainstorm.get("repo") or brainstorm.get("project_name") or "")
    except Exception as e:
        logger.debug(f"Error resolving active project for chat {chat_id}: {e}")
        return None


def parse_command_text(
    text: str,
    default_action: str = "chat",
    default_preset: str = "standard",
    default_repo: Optional[str] = None
) -> Dict[str, Any]:
    """
    Canonical deterministic command parser (used by Web UI, API, and Telegram).
    Recognizes:
    - /task <task>, /sviluppa <task> => action: 'execute_task'
    - /brainstorm <task> => action: 'brainstorm', preset: 'critical'
    - /approve => action: 'approve'
    - /reject [reason] => action: 'reject'
    - /clear => action: 'clear'
    - /chat <msg> or free text => action: 'chat'
    - Preset specifier: preset:<name>, --preset <name>, --preset=<name>
    - Preset flags: --critical, /critical, --light, /light, --luna_flash, /luna_flash, --standard, /standard, --mechanical, /mechanical, --quick, /quick, --sol_pro, /sol_pro
    - Repo specifier: repo:<name> or project:<name>
    """
    clean_text = (text or "").strip()
    action = default_action
    preset = default_preset
    repo = default_repo
    invalid_preset = None

    # 1. Recognize explicit actions
    if clean_text.startswith(("/task", "/sviluppa", "!task", "!sviluppa")):
        action = "execute_task"
        clean_text = re.sub(r"^[!/](?:task|sviluppa)\s*", "", clean_text, flags=re.IGNORECASE).strip()
    elif clean_text.startswith(("/brainstorm", "!brainstorm")):
        action = "brainstorm"
        preset = "critical"
        clean_text = re.sub(r"^[!/]brainstorm\s*", "", clean_text, flags=re.IGNORECASE).strip()
    elif clean_text.startswith(("/approve", "!approve")):
        action = "approve"
        clean_text = re.sub(r"^[!/]approve\s*", "", clean_text, flags=re.IGNORECASE).strip()
    elif clean_text.startswith(("/reject", "!reject")):
        action = "reject"
        clean_text = re.sub(r"^[!/]reject\s*", "", clean_text, flags=re.IGNORECASE).strip()
    elif clean_text.startswith(("/clear", "!clear")):
        action = "clear"
        clean_text = re.sub(r"^[!/]clear\s*", "", clean_text, flags=re.IGNORECASE).strip()
    elif clean_text.startswith(("/chat", "!chat")):
        action = "chat"
        clean_text = re.sub(r"^[!/]chat\s*", "", clean_text, flags=re.IGNORECASE).strip()

    # 2. Explicit presets: preset:<name> or --preset <name> or --preset=<name>
    preset_match = re.search(r"(?:^|\s)(?:--preset[=\s]+|preset:)(\w[\w-]*)\b", clean_text, flags=re.IGNORECASE)
    if preset_match:
        extracted_preset = preset_match.group(1).lower()
        clean_text = re.sub(r"(?:^|\s)(?:--preset[=\s]+|preset:)\w[\w-]*\b", " ", clean_text, flags=re.IGNORECASE).strip()
        if extracted_preset in ALLOWED_API_PRESETS:
            preset = extracted_preset
        else:
            invalid_preset = extracted_preset

    # Shorthand presets
    elif "--mechanical" in clean_text or clean_text.startswith("/mechanical"):
        preset = "mechanical"
        clean_text = re.sub(r"/(?:mechanical)\b|--(?:mechanical)\b", "", clean_text, flags=re.IGNORECASE).strip()
    elif "--critical" in clean_text or clean_text.startswith("/critical"):
        preset = "critical"
        clean_text = re.sub(r"/(?:critical)\b|--(?:critical)\b", "", clean_text, flags=re.IGNORECASE).strip()
    elif "--light" in clean_text or clean_text.startswith("/light"):
        preset = "light"
        clean_text = re.sub(r"/(?:light)\b|--(?:light)\b", "", clean_text, flags=re.IGNORECASE).strip()
    elif "--luna_flash" in clean_text or clean_text.startswith("/luna_flash"):
        preset = "luna_flash"
        clean_text = re.sub(r"/(?:luna_flash)\b|--(?:luna_flash)\b", "", clean_text, flags=re.IGNORECASE).strip()
    elif "--quick" in clean_text or clean_text.startswith("/quick"):
        preset = "quick"
        clean_text = re.sub(r"/(?:quick)\b|--(?:quick)\b", "", clean_text, flags=re.IGNORECASE).strip()
    elif "--sol_pro" in clean_text or clean_text.startswith("/sol_pro"):
        preset = "sol_pro"
        clean_text = re.sub(r"/(?:sol_pro)\b|--(?:sol_pro)\b", "", clean_text, flags=re.IGNORECASE).strip()
    elif "--standard" in clean_text or clean_text.startswith("/standard"):
        preset = "standard"
        clean_text = re.sub(r"/(?:standard)\b|--(?:standard)\b", "", clean_text, flags=re.IGNORECASE).strip()

    # 3. Extract repo/project alias (e.g.: repo:Assistente or project:Assistente)
    words = clean_text.split()
    remaining_words = []
    for w in words:
        if re.match(r"^(?:repo|project):", w, re.IGNORECASE):
            extracted = re.sub(r"^(?:repo|project):", "", w, flags=re.IGNORECASE).strip()
            if extracted:
                repo = extracted
        else:
            remaining_words.append(w)

    final_text = " ".join(remaining_words).strip() or clean_text or text

    return {
        "action": action,
        "text": final_text,
        "preset": preset,
        "invalid_preset": invalid_preset,
        "repo": repo,
        "is_chat": (action == "chat")
    }


def validate_api_run_payload(payload: Any) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """
    Rigorously validates JSON payload for /api/run and /api/execute.
    Returns (is_valid, error_message, sanitized_data).
    """
    if not isinstance(payload, dict):
        return False, "Payload must be a JSON object (Il payload deve essere un oggetto JSON).", None

    # 1. Explicit rejection of cliCommand or unknown fields
    if "cliCommand" in payload:
        return False, "The 'cliCommand' field is deprecated and not permitted for security reasons.", None

    unknown_keys = set(payload.keys()) - ALLOWED_API_FIELDS
    if unknown_keys:
        return False, f"Disallowed fields in payload (Campi non consentiti): {', '.join(sorted(unknown_keys))}.", None

    # 2. Type validation for all fields (must be string or None)
    for key, value in payload.items():
        if value is not None and not isinstance(value, str):
            return False, f"Field '{key}' must be a string (deve essere una stringa).", None

    # 3. Action validation (required)
    raw_action = payload.get("action")
    if not raw_action or not isinstance(raw_action, str) or raw_action.strip() not in ALLOWED_API_ACTIONS:
        allowed_str = ", ".join(sorted(ALLOWED_API_ACTIONS))
        return False, f"Invalid or missing action (Azione non valida o mancante). Allowed actions: {allowed_str}.", None

    raw_message = (payload.get("message") or "").strip()
    raw_task = (payload.get("task") or "").strip()
    raw_repo = (payload.get("repo") or "").strip()
    raw_preset = (payload.get("preset") or "").strip()
    chat_id = str(payload.get("chat_id") or payload.get("chatId") or "").strip() or None

    # Command parsing from text (e.g. /task, /sviluppa, /brainstorm)
    text_to_parse = raw_message or raw_task
    parsed = parse_command_text(
        text_to_parse,
        default_action=raw_action.strip(),
        default_preset=raw_preset or "standard",
        default_repo=raw_repo or None
    )

    action = raw_action.strip()
    # If payload has action="chat" but text invokes /sviluppa or /task, promote to execute_task
    if action == "chat" and parsed["action"] == "execute_task":
        action = "execute_task"

    preset = parsed["preset"]
    extracted_repo = parsed["repo"]
    final_text = parsed["text"]

    # Preset validation
    if parsed.get("invalid_preset"):
        allowed_p = ", ".join(sorted(ALLOWED_API_PRESETS))
        return False, f"Invalid or unknown preset (Preset non valido o sconosciuto: '{parsed['invalid_preset']}'). Allowed presets: {allowed_p}.", None

    if preset:
        preset = preset.lower()
        if preset not in ALLOWED_API_PRESETS:
            allowed_p = ", ".join(sorted(ALLOWED_API_PRESETS))
            return False, f"Invalid or unknown preset (Preset non valido o sconosciuto: '{preset}'). Allowed presets: {allowed_p}.", None
    else:
        preset = None

    # Repo validation
    resolved_repo = None
    if extracted_repo:
        resolved_repo = resolve_catalog_repo(extracted_repo)
        if not resolved_repo:
            return False, f"Invalid repository or not found in catalog (Repository non valido): '{extracted_repo}'.", None
    elif raw_repo:
        resolved_repo = resolve_catalog_repo(raw_repo)
        if not resolved_repo:
            return False, f"Invalid repository or not found in catalog (Repository non valido): '{raw_repo}'.", None

    # Active chat repo resolution for execute_task if repo not explicit
    if action == "execute_task" and not resolved_repo and chat_id:
        resolved_repo = resolve_active_project_for_chat(chat_id)

    # REPO REQUIRED FOR EXECUTE_TASK: no fallback to default workspace
    if action == "execute_task" and not resolved_repo:
        return False, (
            "No project selected for task execution (Nessun progetto selezionato). "
            "Select a valid project from dashboard or specify repo:<name>."
        ), None

    # Content validation for chat and execute_task
    if action == "chat":
        if not final_text:
            return False, "The 'message' or 'task' field is required and cannot be empty (obbligatorio e non vuoto) for 'chat' action.", None
    elif action == "execute_task":
        if not final_text:
            return False, "The 'task' or 'message' field is required and cannot be empty (obbligatorio e non vuoto) for 'execute_task' action.", None

    sanitized = {
        "action": action,
        "chat_id": chat_id,
        "message": final_text if action in ["chat", "reject"] else None,
        "task": final_text if action in ["execute_task", "brainstorm"] else (raw_task or None),
        "repo": resolved_repo,
        "preset": preset,
    }
    return True, None, sanitized

def build_orchestrator_cmd_args(data: Dict[str, Any]) -> List[str]:
    """
    Costruisce la lista di argomenti fissa e sicura per orchestrator_core.py.
    Non usa shell=True e non permette mai di sostituire interprete o script.
    """
    cmd_args: List[str] = ["python3", str(SERVER_DIR / "orchestrator_core.py")]
    action = data["action"]
    raw_chat_id = data.get("chat_id") or data.get("chatId")
    chat_id = str(raw_chat_id).strip() if raw_chat_id is not None and str(raw_chat_id).strip() else None
    message = data.get("message")
    task = data.get("task")
    repo = data.get("repo")
    preset = data.get("preset")

    if chat_id:
        cmd_args.extend(["--chat-id", str(chat_id)])
    if repo:
        cmd_args.extend(["--repo", str(repo)])
    if preset:
        cmd_args.extend(["--preset", str(preset)])
    if data.get("read_only") or data.get("readonly"):
        cmd_args.append("--read-only")

    # Testo prioritario (message o task)
    text_content = message or task or ""

    # Controllo menzione chat per compatibilità funzionale con @sol, @luna, @agy, /sol, /luna, /chat, etc.
    is_chat_mention = bool(re.match(
        r"^(?:@(?:sol|luna|terra|agy|deepseek|ds|pro|flash|glm|all|team|director)\b|/(?:sol|luna|terra|agy|deepseek|ds|pro|flash|glm|all|team|director|chat|agenti|agents|progetti|progetto)\b)",
        text_content.strip(),
        re.IGNORECASE
    ))

    if action == "clear":
        cmd_args.append("--chat-clear")

    elif action == "chat" or (action == "execute_task" and is_chat_mention):
        cmd_args.extend(["--brainstorm-continue", "active"])
        if text_content:
            b64_msg = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
            cmd_args.extend(["--msg-b64", b64_msg])

    elif action == "brainstorm":
        cmd_args.append("--brainstorm")
        if text_content:
            b64_task = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
            cmd_args.extend(["--task-b64", b64_task])

    elif action == "execute_task":
        if text_content:
            b64_task = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
            cmd_args.extend(["--task-b64", b64_task])

    elif action == "approve":
        cmd_args.extend(["--brainstorm-approve", "active"])

    elif action == "reject":
        cmd_args.extend(["--brainstorm-reject", "active"])
        if text_content:
            b64_msg = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
            cmd_args.extend(["--msg-b64", b64_msg])

    return cmd_args

def get_system_data() -> Dict[str, Any]:
    """Raccoglie lo stato del sistema, account, worktree, brainstorming e cronologia esecuzioni."""
    # 1. Stato Account
    accounts_state_file = STATE_DIR / "accounts_state.json"
    accounts_info = []
    current_account = "default"

    if accounts_state_file.exists():
        try:
            data = json.loads(accounts_state_file.read_text(encoding="utf-8"))
            accounts_info = data.get("accounts", [])
            current_account = data.get("current_account", "default")
        except Exception:
            pass

    # 2. Ultime Esecuzioni
    runs = []
    history_file = STATE_DIR / "runs_history.jsonl"
    total_tokens = 0
    completed_runs = 0

    if history_file.exists():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        run_entry = json.loads(line)
                        runs.append(run_entry)
                        tokens_total = run_entry.get("tokens_used", {}).get("total", 0)
                        if isinstance(tokens_total, (int, float)):
                            total_tokens += tokens_total
                        if run_entry.get("status") == "COMPLETED":
                            completed_runs += 1
        except Exception:
            pass

    # 3. Worktrees attivi
    active_worktrees = []
    if WORKTREES_DIR.exists():
        for p in WORKTREES_DIR.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                active_worktrees.append(p.name)

    # 4. Sessioni Brainstorming
    bm = BrainstormManager(BRAINSTORMS_DIR)
    brainstorms = bm.list_brainstorms()

    # 5. Diff HTML files generati
    diff_files = []
    if DIFFS_DIR.exists():
        for f in sorted(DIFFS_DIR.glob("*.html"), key=os.path.getmtime, reverse=True):
            diff_files.append(f.name)

    success_rate = (completed_runs / max(1, len(runs))) * 100 if runs else 100.0
    est_cost = (total_tokens / 1_000_000) * 2.50

    return {
        "status": "healthy",
        "system": "Taktstock Multi-Agent Orchestrator",
        "current_account": current_account,
        "accounts_count": len(accounts_info),
        "accounts": accounts_info,
        "active_worktrees_count": len(active_worktrees),
        "active_worktrees": active_worktrees,
        "brainstorms_count": len(brainstorms),
        "active_brainstorms_count": sum(1 for b in brainstorms if b.get("status") == "active"),
        "brainstorms": brainstorms[:15],
        "total_runs_count": len(runs),
        "completed_runs_count": completed_runs,
        "success_rate": round(success_rate, 1),
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(est_cost, 3),
        "recent_runs": runs[-15:][::-1],
        "diff_files": diff_files[:15],
        "timestamp": datetime.now().isoformat()
    }

def render_html_dashboard(error_msg: str = "") -> str:
    """Generates standalone HTML page for Web Dashboard (modern dark theme)."""
    data = get_system_data()
    runs_rows = ""

    from projects_manager import ProjectsManager
    from skills_manager import SkillsManager
    pm = ProjectsManager()
    sm = SkillsManager()
    all_projects = pm.list_projects()
    all_skills = sm.list_skills()
    dashboard_proj_options = '<option value="">-- Select Project (Required) --</option>'
    for p in all_projects:
        icon = "📁"
        t = p["tech_stack"].lower()
        if "astro" in t: icon = "🚀"
        elif "react" in t: icon = "📊"
        elif "wordpress" in t: icon = "👤"
        elif "n8n" in t: icon = "🤖"
        elif "docs" in t or "config" in t: icon = "🏠"
        elif "knowledge" in t or "metodo" in t: icon = "📚"
        dashboard_proj_options += f'<option value="{p["name"]}">{icon} {p["name"]} ({p["tech_stack"]})</option>'

    for r in data["recent_runs"]:
        preset = r.get("preset", "standard").lower()
        preset_color = "#3b82f6" if preset == "standard" else ("#10b981" if preset == "light" else "#ef4444")
        status = str(r.get("status", "COMPLETED")).upper()
        if status == "COMPLETED":
            status_badge = '<span class="badge badge-success">COMPLETED</span>'
        elif status in ["BLOCKED_PREREQUISITE", "BLOCKED"]:
            status_badge = '<span class="badge badge-danger" style="background:#dc2626;">BLOCKED_PREREQUISITE</span>'
        elif status == "WAITING_FOR_APPROVAL":
            status_badge = '<span class="badge badge-info" style="background:#0284c7;">WAITING_FOR_APPROVAL</span>'
        else:
            status_badge = f'<span class="badge badge-warning">{status}</span>'
        
        diff_link = "-"
        if r.get("html_diff"):
            diff_filename = Path(r["html_diff"]).name
            diff_link = f'<a href="/diff/{diff_filename}" target="_blank" class="diff-btn">🔍 View Diff</a>'

        tokens_raw = r.get("tokens_used", {}).get("total", "not_measured")
        tokens = f"{tokens_raw:,}" if isinstance(tokens_raw, (int, float)) else str(tokens_raw)
        ts = r.get("timestamp", "")
        formatted_ts = ts.replace("T", " ")[:19] if ts else "-"

        project_badge = f'<code style="color:#60a5fa;">{r.get("project") or "-"}</code>'

        runs_rows += f"""
        <tr>
            <td style="color: #94a3b8; font-size: 0.85rem;">{formatted_ts}</td>
            <td><span style="color: {preset_color}; font-weight: 600;">{preset.upper()}</span></td>
            <td>
                <div style="font-weight: 500;">{r.get('task', '-')}</div>
                <div style="font-size: 0.75rem; color: #94a3b8; margin-top: 2px;">Project: {project_badge}</div>
            </td>
            <td><code>{r.get('branch', '-')}</code></td>
            <td style="font-family: monospace;">{tokens}</td>
            <td>{status_badge}</td>
            <td>{diff_link}</td>
        </tr>
        """

    accounts_cards = ""
    for a in data.get("accounts", []):
        is_avail = a.get("available", True) if "available" in a else (a.get("status") == "active")
        status_dot = "🟢" if is_avail else "🔴"
        is_curr = (a.get("name") == data.get("current_account")) or a.get("is_current", False)
        curr_badge = '<span class="badge badge-success" style="margin-left: 8px;">ACTIVE</span>' if is_curr else ""
        auth_type = a.get("auth_type", "token")
        rate_limit = a.get("rate_limit_reset") or "OK"
        accounts_cards += f"""
        <div class="card account-card" style="padding: 12px; margin-bottom: 8px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong>{status_dot} {a.get('name', 'Account')}</strong>
                <div>{curr_badge}</div>
            </div>
            <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">
                Auth: <code>{auth_type}</code> | Quota: {rate_limit}
            </div>
        </div>
        """

    worktrees_list = ""
    if data["active_worktrees"]:
        for wt in data["active_worktrees"]:
            worktrees_list += f'<div class="wt-item">🌿 <code>{wt}</code></div>'
    else:
        worktrees_list = '<div style="color: #64748b; font-size: 0.9rem; padding: 8px 0;">No active worktrees.</div>'

    brainstorm_rows = ""
    for bs in data["brainstorms"]:
        bs_status = bs.get("status", "active")
        b_badge = '<span class="badge badge-warning">ACTIVE</span>' if bs_status == "active" else ('<span class="badge badge-success">APPROVED</span>' if bs_status == "approved" else '<span class="badge badge-error">REJECTED</span>')
        rounds_cnt = len(bs.get("rounds", []))
        last_round = bs["rounds"][-1] if bs.get("rounds") else {}
        strategy = last_round.get("director_analysis", {}).get("strategy", "-")
        if len(strategy) > 120:
            strategy = strategy[:120] + "..."

        bs_proj = bs.get("project_name") or bs.get("repo") or "-"

        brainstorm_rows += f"""
        <tr>
            <td><code>{bs.get('id', '-')}</code></td>
            <td>
                <div style="font-weight: 500;">{bs.get('task', '-')}</div>
                <div style="font-size: 0.75rem; color: #94a3b8; margin-top: 2px;">Project: <code style="color:#60a5fa;">{bs_proj}</code></div>
            </td>
            <td>Round {rounds_cnt}</td>
            <td>{b_badge}</td>
            <td style="font-size: 0.85rem; color: #94a3b8;">{strategy}</td>
            <td>
                <a href="/brainstorm/{bs.get('id')}" class="diff-btn" style="background:#2563eb; color:#fff;">💬 Open Chat ({len(bs.get('messages', []))})</a>
            </td>
        </tr>
        """

    err_html = f'<div class="alert alert-danger" style="margin-bottom: 20px; background: #7f1d1d; color: #f87171; padding: 14px 18px; border-radius: 8px; border: 1px solid #b91c1c; font-weight: 500;">⚠️ {error_msg}</div>' if error_msg else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Taktstock Multi-Agent Dashboard</title>
    <style>
        :root {{
            --bg-main: #0f172a;
            --bg-card: #1e293b;
            --border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #3b82f6;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-main);
            color: var(--text-main);
            margin: 0;
            padding: 24px;
            box-sizing: border-box;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }}
        .header h1 {{ margin: 0; font-size: 1.5rem; display: flex; align-items: center; gap: 8px; }}
        .pulse {{
            width: 10px;
            height: 10px;
            background-color: var(--success);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
            animation: pulse 2s infinite;
        }}
        @keyframes pulse {{
            0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
            70% {{ transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }}
            100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
        }}
        .grid-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .stat-label {{ font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }}
        .stat-value {{ font-size: 1.8rem; font-weight: 700; margin-top: 4px; }}
        .section-title {{
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
            text-align: left;
        }}
        th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--border);
        }}
        th {{ background: #111827; color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; }}
        tr:hover {{ background: #26334d; }}
        code {{ background: #0f172a; padding: 2px 6px; border-radius: 4px; font-family: monospace; color: #38bdf8; }}
        .badge {{ padding: 4px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; }}
        .badge-success {{ background: #064e3b; color: #34d399; }}
        .badge-warning {{ background: #78350f; color: #fbbf24; }}
        .badge-error {{ background: #7f1d1d; color: #f87171; }}
        .diff-btn {{ background: #1e3a8a; color: #93c5fd; padding: 4px 10px; border-radius: 6px; text-decoration: none; font-size: 0.8rem; font-weight: 600; }}
        .two-cols {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 24px; }}
        @media (max-width: 900px) {{ .two-cols {{ grid-template-columns: 1fr; }} }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🎯 Taktstock Multi-Agent <span style="font-size: 1rem; color: var(--text-muted); font-weight: normal;">| Control Center</span></h1>
        <div style="display: flex; align-items: center; gap: 12px;">
            <span class="pulse"></span>
            <span style="font-size: 0.9rem; color: var(--success); font-weight: 500;">Online</span>
            <span style="font-size: 0.85rem; color: var(--text-muted); margin-left: 12px;">Auto-refresh: 30s</span>
        </div>
    </div>

    {err_html}

    <!-- Stat Cards -->
    <div class="grid-stats">
        <div class="card">
            <div class="stat-label">Total Runs</div>
            <div class="stat-value" style="color: #60a5fa;">{data['total_runs_count']}</div>
            <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">Success Rate: {data['success_rate']}%</div>
        </div>
        <div class="card">
            <div class="stat-label">Brainstormings</div>
            <div class="stat-value" style="color: #f59e0b;">{data['brainstorms_count']}</div>
            <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">Active: {data['active_brainstorms_count']}</div>
        </div>
        <div class="card">
            <div class="stat-label">Tokens Consumed</div>
            <div class="stat-value" style="color: #34d399;">{data['total_tokens']:,}</div>
            <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">Estimated cost: ~${data['estimated_cost_usd']}</div>
        </div>
        <div class="card">
            <div class="stat-label">Active Git Worktrees</div>
            <div class="stat-value" style="color: #c084fc;">{data['active_worktrees_count']}</div>
            <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">Per-task isolation</div>
        </div>
    </div>

    <!-- New Work Session / Task Execution Form -->
    <div class="card" style="margin-bottom: 24px; border: 1px solid #3b82f655; background: linear-gradient(180deg, #1e293b 0%, #162032 100%); padding: 20px;">
        <div class="section-title" style="color: #60a5fa; margin-bottom: 12px;">🚀 New Work Session / Task Execution</div>
        <form method="POST" action="/brainstorm/new" style="display:flex; flex-direction:column; gap: 14px;">
            <div style="display:grid; grid-template-columns: 2fr 1fr; gap: 14px;">
                <div>
                    <label style="font-size:0.8rem; color:#94a3b8; display:block; margin-bottom:6px; font-weight:600; text-transform:uppercase;">📁 Target Project (Required)</label>
                    <select name="repo" required style="width:100%; padding:10px 12px; background:#0f172a; border:1px solid #334155; border-radius:8px; color:#fff; font-size:0.95rem; cursor:pointer;">
                        {dashboard_proj_options}
                    </select>
                </div>
                <div>
                    <label style="font-size:0.8rem; color:#94a3b8; display:block; margin-bottom:6px; font-weight:600; text-transform:uppercase;">⚡ Multi-Agent Preset</label>
                    <select name="preset" style="width:100%; padding:10px 12px; background:#0f172a; border:1px solid #334155; border-radius:8px; color:#fff; font-size:0.95rem; cursor:pointer;">
                        <option value="standard">Standard (Sol + Reviewer)</option>
                        <option value="critical">Critical (All 7 Agents)</option>
                        <option value="luna_flash">Luna + Flash (Fast Pairing & UX)</option>
                        <option value="light">Light (Direct Development)</option>
                    </select>
                </div>
            </div>
            <div>
                <label style="font-size:0.8rem; color:#94a3b8; display:block; margin-bottom:6px; font-weight:600; text-transform:uppercase;">💬 Task or Objective (use /task or /sviluppa for direct execution, or free text for brainstorming)</label>
                <div style="display:flex; gap: 10px;">
                    <input type="text" name="task" placeholder="e.g.: /task fix routing or discuss architecture..." required style="flex:1; padding:12px 14px; background:#0f172a; border:1px solid #334155; border-radius:8px; color:#fff; font-size:0.95rem;">
                    <button type="submit" class="btn btn-primary" style="padding: 0 24px; font-weight:600; white-space:nowrap; background:#2563eb; color:#fff; border-radius:8px; border:none; cursor:pointer;">🚀 Launch</button>
                </div>
            </div>
        </form>
    </div>

    <!-- Main Section -->
    <div class="two-cols">
        <!-- Runs History Table -->
        <div class="card" style="padding: 0; overflow: hidden;">
            <div style="padding: 16px; border-bottom: 1px solid var(--border);" class="section-title">
                📊 Recent Executions
            </div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>Date/Time</th>
                            <th>Preset</th>
                            <th>Task</th>
                            <th>Branch</th>
                            <th>Tokens</th>
                            <th>Status</th>
                            <th>Visual Diff</th>
                        </tr>
                    </thead>
                    <tbody>
                        {runs_rows if runs_rows else '<tr><td colspan="7" style="text-align:center; color:#64748b; padding: 24px;">No executions recorded yet. Send a task from Telegram or CLI!</td></tr>'}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Sidebar Accounts & Worktrees -->
        <div style="display:flex; flex-direction:column; gap: 20px;">
            <div class="card">
                <div class="section-title">🔑 Accounts & Failover</div>
                <div class="account-grid">
                    {accounts_cards if accounts_cards else '<div style="color:#64748b; font-size:0.9rem;">No accounts configured.</div>'}
                </div>
            </div>

            <div class="card">
                <div class="section-title">🌳 Git Worktrees</div>
                {worktrees_list}
            </div>
        </div>
    </div>

    <!-- Brainstorming Section -->
    <div class="card" style="padding: 0; overflow: hidden; margin-bottom: 24px;">
        <div style="padding: 16px; border-bottom: 1px solid var(--border);" class="section-title">
            🧠 Buzz Multi-Agent Chat & Deliberation
        </div>
        <div style="overflow-x: auto;">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Requested Task</th>
                        <th>Round</th>
                        <th>Status</th>
                        <th>Recommended Strategy</th>
                        <th>Chat & Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {brainstorm_rows if brainstorm_rows else '<tr><td colspan="6" style="text-align:center; color:#64748b; padding: 20px;">No brainstorming sessions started yet. Use /brainstorm from Telegram or --brainstorm from CLI.</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        setTimeout(() => {{
            window.location.reload();
        }}, 30000);
    </script>
</body>
</html>
"""
    return html

def render_brainstorm_html_page(bs: Dict[str, Any]) -> str:
    """Renders the HTML page with a full multi-agent chat in Buzz/Slack style."""
    bs_id = bs.get("id")
    task = bs.get("task", "")
    status = bs.get("status", "active")
    messages = bs.get("messages", [])
    rounds = bs.get("rounds", [])

    from projects_manager import ProjectsManager
    pm = ProjectsManager()
    all_projects = pm.list_projects()
    
    current_proj_name = bs.get("project_name") or ""
    current_repo = bs.get("repo") or ""
    if not current_proj_name and current_repo:
        found_p = pm.find_project(current_repo)
        if found_p:
            current_proj_name = found_p["name"]

    proj_options = '<option value="">-- Select or Switch Project --</option>'
    for p in all_projects:
        sel = 'selected' if (current_proj_name and current_proj_name.lower() == p["name"].lower()) else ''
        icon = "📁"
        t = p["tech_stack"].lower()
        if "astro" in t: icon = "🚀"
        elif "react" in t: icon = "📊"
        elif "wordpress" in t: icon = "👤"
        elif "n8n" in t: icon = "🤖"
        elif "docs" in t or "config" in t: icon = "🏠"
        proj_options += f'<option value="{p["name"]}" {sel}>{icon} {p["name"]} ({p["tech_stack"]})</option>'

    # Chat Messages Feed
    chat_bubbles = ""
    for m in messages:
        agent_key = m.get("agent", "user")
        meta = AGENT_META.get(agent_key, {"name": m.get("sender", "Agent"), "avatar": "🤖", "color": "#38bdf8"})
        is_user = agent_key == "user"
        align = "flex-end" if is_user else "flex-start"
        bubble_bg = "#1e293b" if not is_user else "#334155"
        border_color = meta["color"]
        formatted_ts = m.get("timestamp", "").replace("T", " ")[:19]

        text_content = m.get("text", "").replace("\n", "<br>")

        chat_bubbles += f"""
        <div style="display:flex; justify-content:{align}; margin-bottom: 16px;">
            <div style="max-width: 80%; background: {bubble_bg}; border: 1px solid {border_color}44; border-left: 4px solid {border_color}; border-radius: 10px; padding: 14px 18px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);">
                <div style="display:flex; align-items:center; gap: 8px; margin-bottom: 6px;">
                    <span style="font-size: 1.2rem;">{meta['avatar']}</span>
                    <strong style="color: {meta['color']}; font-size: 0.95rem;">{meta['name']}</strong>
                    <span style="font-size: 0.75rem; color: #94a3b8; margin-left: auto;">{formatted_ts}</span>
                </div>
                <div style="font-size: 0.95rem; line-height: 1.6; color: #f1f5f9;">{text_content}</div>
            </div>
        </div>
        """

    # Collapsible Rounds Synthesis
    rounds_accordion = ""
    for r in rounds:
        r_num = r.get("round", 1)
        dir_analysis = r.get("director_analysis", {})
        opinions = r.get("reviewer_opinions", [])
        synth = r.get("director_synthesis", {})

        rounds_accordion += f"""
        <details style="background: #111827; border: 1px solid #334155; border-radius: 8px; margin-bottom: 10px; padding: 12px 16px;">
            <summary style="font-weight: 600; cursor: pointer; color: #38bdf8;">📋 Round {r_num} Synthesis & Technical Details</summary>
            <div style="margin-top: 12px; font-size: 0.9rem;">
                <p><strong>🧠 Sol Strategy:</strong> {dir_analysis.get('strategy', '-')}</p>
                <p><strong>🎯 Recommendation:</strong> {synth.get('recommended_option', '-')}</p>
                <p><strong>🤝 Consensus:</strong> {', '.join(synth.get('consensus', [])) or '-'}</p>
            </div>
        </details>
        """

    actions_html = ""
    if status == "active":
        actions_html = f"""
        <div class="chat-input-container">
            <!-- Quick Mention Tags (All 7 Agents) -->
            <div style="display:flex; gap: 8px; margin-bottom: 12px; align-items:center; flex-wrap: wrap;">
                <span style="font-size: 0.85rem; color: #94a3b8; font-weight:600;">Mention agent:</span>
                <button type="button" class="tag-btn" onclick="addMention('@sol')">🧠 @Sol</button>
                <button type="button" class="tag-btn" onclick="addMention('@luna')">🌙 @Luna</button>
                <button type="button" class="tag-btn" onclick="addMention('@pro')">🔍 @DeepSeek Pro</button>
                <button type="button" class="tag-btn" onclick="addMention('@flash')">⚡ @Flash</button>
                <button type="button" class="tag-btn" onclick="addMention('@glm')">🛡️ @GLM</button>
                <button type="button" class="tag-btn" onclick="addMention('@agy')">🚀 @AGY</button>
                <button type="button" class="tag-btn" onclick="addMention('@all')">👥 @All</button>
            </div>

            <form method="POST" action="/brainstorm/{bs_id}/chat" id="chatForm" onsubmit="return handleChatSubmit(event)">
                <div style="display:flex; gap: 10px;">
                    <textarea id="chatInput" name="message" placeholder="Write a message or ask a question (e.g.: @luna what do you think of UX?, @agy analyze package.json, work on <Project>)..." required style="flex:1; height: 65px; padding: 12px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #fff; resize: none; font-size: 0.95rem;"></textarea>
                    <button type="submit" id="sendBtn" class="btn btn-primary" style="align-self: flex-end; height: 65px; padding: 0 24px;">💬 Send</button>
                </div>
            </form>
            
            <div style="display:flex; justify-content:space-between; align-items:center; margin-top: 16px; padding-top: 14px; border-top: 1px solid #334155;">
                <form method="POST" action="/brainstorm/{bs_id}/approve">
                    <button type="submit" class="btn btn-success">✅ Approve Plan & Launch Development</button>
                </form>
                
                <form method="POST" action="/brainstorm/{bs_id}/reject" style="display:flex; gap: 8px;">
                    <input type="text" name="reason" placeholder="Optional rejection reason..." style="padding: 8px 12px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; font-size: 0.85rem;">
                    <button type="submit" class="btn btn-danger">❌ Reject</button>
                </form>
            </div>
        </div>
        """
    elif status == "approved":
        actions_html = '<div class="alert alert-success">✅ <strong>Plan Approved!</strong> Multi-agent workflow is executing on git worktree.</div>'
    elif status == "rejected":
        actions_html = f'<div class="alert alert-danger">❌ <strong>Plan Rejected:</strong> {bs.get("rejected_reason", "No reason specified")}</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Buzz Chat: {task}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #0f172a;
            color: #f8fafc;
            padding: 24px;
            line-height: 1.6;
            max-width: 1000px;
            margin: 0 auto;
        }}
        h1 {{ font-size: 1.4rem; }}
        a {{ color: #38bdf8; text-decoration: none; }}
        .header-bar {{ margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #334155; display:flex; justify-content:space-between; align-items:center; }}
        .chat-container {{ background: #182234; border: 1px solid #334155; border-radius: 12px; padding: 24px; min-height: 400px; max-height: 600px; overflow-y: auto; margin-bottom: 20px; }}
        .chat-input-container {{ background: #1e293b; border: 1px solid #334155; padding: 20px; border-radius: 12px; }}
        .tag-btn {{ background: #0f172a; border: 1px solid #334155; color: #94a3b8; padding: 5px 12px; border-radius: 6px; font-size: 0.85rem; cursor: pointer; transition: all 0.2s; }}
        .tag-btn:hover {{ background: #2563eb; color: #fff; border-color: #2563eb; }}
        .btn {{ padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; border: none; font-size: 0.9rem; }}
        .btn-primary {{ background: #2563eb; color: #fff; }}
        .btn-primary:hover {{ background: #1d4ed8; }}
        .btn-success {{ background: #16a34a; color: #fff; }}
        .btn-success:hover {{ background: #15803d; }}
        .btn-danger {{ background: #dc2626; color: #fff; }}
        .alert {{ padding: 14px; border-radius: 8px; margin-top: 20px; }}
        .alert-success {{ background: #064e3b; color: #34d399; border: 1px solid #059669; }}
        .alert-danger {{ background: #7f1d1d; color: #f87171; border: 1px solid #b91c1c; }}
        .badge {{ padding: 4px 10px; border-radius: 6px; font-size: 0.8rem; font-weight: bold; }}
        .badge-warning {{ background: #78350f; color: #fbbf24; }}
        .badge-success {{ background: #064e3b; color: #34d399; }}
        .badge-danger {{ background: #7f1d1d; color: #f87171; }}
        @keyframes pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.4; }}
        }}
        .typing-pulse {{ animation: pulse 1.5s infinite; }}
    </style>
</head>
<body>
    <div class="header-bar">
        <div>
            <a href="/">⬅️ Back to Dashboard</a>
            <h1 style="margin-top: 6px;">💬 Buzz Chatroom: {task}</h1>
        </div>
        <div>
            Status: <span class="badge badge-{ 'warning' if status == 'active' else ('success' if status == 'approved' else 'danger') }">{status.upper()}</span>
        </div>
    </div>

    <!-- Project Selector Bar -->
    <div style="background:#1e293b; border:1px solid #334155; border-radius:8px; padding:12px 16px; margin-bottom:16px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
        <div style="display:flex; align-items:center; gap:10px;">
            <span style="font-size:0.9rem; color:#94a3b8; font-weight:600;">📁 ACTIVE PROJECT:</span>
            <select id="projectSelector" onchange="handleProjectChange(this.value)" style="background:#0f172a; border:1px solid #3b82f6; border-radius:6px; color:#f8fafc; padding:6px 12px; font-size:0.9rem; font-weight:600; cursor:pointer;">
                {proj_options}
            </select>
        </div>
        <div style="font-size:0.85rem; color:#94a3b8;">
            {f'📂 Path: <code style="color:#38bdf8;">{current_repo}</code>' if current_repo else '<em>No project selected</em>'}
        </div>
    </div>

    <!-- Accordion Round Syntheses -->
    {rounds_accordion}
    
    <!-- Chat Stream -->
    <div class="chat-container" id="chatContainer">
        {chat_bubbles}
    </div>
    
    <!-- Input Box & Actions -->
    {actions_html}

    <script>
        function addMention(tag) {{
            const input = document.getElementById('chatInput');
            if (input) {{
                input.value = tag + ' ' + input.value;
                input.focus();
            }}
        }}

        function handleProjectChange(projName) {{
            if (!projName) return;
            const input = document.getElementById('chatInput');
            if (input) {{
                input.value = 'work on ' + projName;
                document.getElementById('chatForm').requestSubmit();
            }}
        }}

        function scrollToBottom() {{
            const c = document.getElementById('chatContainer');
            if (c) c.scrollTop = c.scrollHeight;
        }}

        async function handleChatSubmit(e) {{
            e.preventDefault();
            const input = document.getElementById('chatInput');
            const btn = document.getElementById('sendBtn');
            const msg = input.value.trim();
            if (!msg) return false;

            // Disable button and input to avoid duplicate submission
            btn.disabled = true;
            btn.innerHTML = '⏳ Sending...';
            input.disabled = true;

            const container = document.getElementById('chatContainer');
            
            // Append user bubble immediately
            const userBubble = document.createElement('div');
            userBubble.style.cssText = 'display:flex; justify-content:flex-end; margin-bottom: 16px;';
            userBubble.innerHTML = `
                <div style="max-width: 80%; background: #334155; border: 1px solid #f43f5e44; border-left: 4px solid #f43f5e; border-radius: 10px; padding: 14px 18px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);">
                    <div style="display:flex; align-items:center; gap: 8px; margin-bottom: 6px;">
                        <span style="font-size: 1.2rem;">👤</span>
                        <strong style="color: #f43f5e; font-size: 0.95rem;">You</strong>
                        <span style="font-size: 0.75rem; color: #94a3b8; margin-left: auto;">Just now</span>
                    </div>
                    <div style="font-size: 0.95rem; line-height: 1.6; color: #f1f5f9;">${{msg.replace(/\\n/g, '<br>')}}</div>
                </div>
            `;
            container.appendChild(userBubble);

            // Show typing pulse indicator
            const typingBubble = document.createElement('div');
            typingBubble.id = 'typingBubble';
            typingBubble.style.cssText = 'display:flex; justify-content:flex-start; margin-bottom: 16px;';
            typingBubble.innerHTML = `
                <div class="typing-pulse" style="max-width: 80%; background: #1e293b; border: 1px dashed #38bdf8; border-radius: 10px; padding: 12px 18px; color: #38bdf8; font-size: 0.9rem;">
                    🤖 <em>Agents are preparing a response...</em>
                </div>
            `;
            container.appendChild(typingBubble);
            scrollToBottom();

            try {{
                const formData = new URLSearchParams();
                formData.append('message', msg);
                await fetch('/brainstorm/{bs_id}/chat', {{
                    method: 'POST',
                    body: formData
                }});
                window.location.reload();
            }} catch (err) {{
                if (typingBubble) typingBubble.innerHTML = '<span style="color:#ef4444;">⚠️ Error generating response. Please try again.</span>';
                btn.disabled = false;
                btn.innerHTML = '💬 Send';
                input.disabled = false;
            }}
            return false;
        }}

        window.onload = function() {{
            scrollToBottom();
            const input = document.getElementById('chatInput');
            if (input) {{
                input.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter' && !e.shiftKey) {{
                        e.preventDefault();
                        document.getElementById('chatForm').requestSubmit();
                    }}
                }});
            }}
        }};
    </script>
</body>
</html>
"""
    return html

def render_login_page(error_msg: str = "") -> str:
    err_html = f'<div style="background:#7f1d1d; color:#f87171; padding:10px 14px; border-radius:8px; margin-bottom:18px; font-size:0.9rem; border:1px solid #b91c1c;">⚠️ {error_msg}</div>' if error_msg else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Taktstock Multi-Agent | Restricted Access</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #0f172a;
            color: #f8fafc;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            padding: 16px;
            box-sizing: border-box;
        }}
        .login-card {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 14px;
            padding: 36px 32px;
            width: 100%;
            max-width: 400px;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.5);
            text-align: center;
        }}
        h1 {{ font-size: 1.35rem; margin: 0 0 8px 0; color: #fff; }}
        p {{ color: #94a3b8; font-size: 0.85rem; margin: 0 0 24px 0; line-height: 1.5; }}
        input {{
            width: 100%;
            padding: 12px 14px;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            color: #fff;
            font-size: 1rem;
            box-sizing: border-box;
            margin-bottom: 18px;
            transition: border-color 0.2s;
        }}
        input:focus {{ outline: none; border-color: #38bdf8; }}
        button {{
            width: 100%;
            padding: 12px;
            background: #2563eb;
            color: #fff;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            font-size: 0.95rem;
            cursor: pointer;
            transition: background 0.2s;
        }}
        button:hover {{ background: #1d4ed8; }}
    </style>
</head>
<body>
    <div class="login-card">
        <div style="font-size: 2.8rem; margin-bottom: 12px;">🎯</div>
        <h1>Taktstock Multi-Agent</h1>
        <p>Restricted access. Enter your security token to unlock the Dashboard and Chatrooms.</p>
        {err_html}
        <form method="POST" action="/login">
            <input type="password" name="auth_token" placeholder="Token / Password..." required autofocus>
            <button type="submit">Unlock Access 🔐</button>
        </form>
    </div>
</body>
</html>"""

class TaktstockHealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    @staticmethod
    def _safe_brainstorm_id(bm, bs_id: str) -> bool:
        """Verifies that the ID stays within the brainstorm directory (anti path traversal)."""
        if not bs_id:
            return False
        root = bm.state_dir.resolve()
        path = (bm.state_dir / f"{bs_id}.json").resolve()
        return str(path).startswith(str(root) + os.sep)

    def _is_authenticated(self) -> bool:
        """
        Verifies whether the request is authenticated.
        Allowed exclusively via:
        1. Header X-Taktstock-Token (or fallback X-Ufficio-Token)
        2. Header Authorization: Bearer <token>
        3. HttpOnly session cookie taktstock_session signed with HMAC with valid expiry
        """
        auth_secret = get_auth_secret()
        if not auth_secret or len(auth_secret) < MIN_AUTH_TOKEN_LENGTH:
            return False

        # 1. Header Token API (X-Taktstock-Token / X-Ufficio-Token)
        header_token = (self.headers.get("X-Taktstock-Token") or self.headers.get("X-Ufficio-Token") or "").strip()
        if header_token and hmac.compare_digest(header_token, auth_secret):
            return True

        # 2. Header Authorization: Bearer <token>
        auth_h = self.headers.get("Authorization", "").strip()
        if auth_h.startswith("Bearer "):
            bearer_token = auth_h[7:].strip()
            if bearer_token and hmac.compare_digest(bearer_token, auth_secret):
                return True

        # 3. Session cookie (Cookie: taktstock_session=<exp>.<hmac>)
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            try:
                cookies = http.cookies.SimpleCookie(cookie_header)
                if AUTH_SESSION_COOKIE_NAME in cookies:
                    cookie_val = cookies[AUTH_SESSION_COOKIE_NAME].value.strip()
                    if verify_session_cookie_value(cookie_val, auth_secret):
                        return True
                elif "ufficio_session" in cookies:
                    cookie_val = cookies["ufficio_session"].value.strip()
                    if verify_session_cookie_value(cookie_val, auth_secret):
                        return True
            except Exception:
                pass

        return False

    def do_GET(self):
        # Open endpoints for internal Docker health checks
        if self.path in ["/health", "/status"]:
            data = {"status": "ok", "timestamp": datetime.now().isoformat()}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        if self.path == "/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_login_page().encode("utf-8"))
            return

        if self.path == "/logout":
            self.send_response(303)
            self.send_header("Set-Cookie", f"{AUTH_SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/login")
            self.end_headers()
            return

        # General Authentication Check
        if not self._is_authenticated():
            if self.path.startswith("/api/"):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized. Send X-Taktstock-Token or Authorization: Bearer header"}')
                return
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(render_login_page().encode("utf-8"))
                return

        if self.path == "/" or self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_html_dashboard().encode("utf-8"))

        elif self.path in ["/api/status", "/metrics", "/api/runs"]:
            data = get_system_data()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
            self.end_headers()
            self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))

        elif self.path in ["/api/brainstorms", "/brainstorms"]:
            bm = BrainstormManager(BRAINSTORMS_DIR)
            bs_list = bm.list_brainstorms()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
            self.end_headers()
            self.wfile.write(json.dumps(bs_list, indent=2, ensure_ascii=False).encode("utf-8"))

        elif self.path in ["/api/projects", "/projects"]:
            from projects_manager import ProjectsManager
            pm = ProjectsManager()
            projects_list = pm.list_projects()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(projects_list, indent=2, ensure_ascii=False).encode("utf-8"))

        elif self.path in ["/api/skills", "/skills"]:
            from skills_manager import SkillsManager
            sm = SkillsManager()
            skills_list = sm.list_skills()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(skills_list, indent=2, ensure_ascii=False).encode("utf-8"))

        elif self.path.startswith("/brainstorm/"):
            bs_id = self.path.replace("/brainstorm/", "").split("?")[0].strip()
            bm = BrainstormManager(BRAINSTORMS_DIR)
            bs = bm.load_brainstorm(bs_id) if self._safe_brainstorm_id(bm, bs_id) else None
            if bs:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(render_brainstorm_html_page(bs).encode("utf-8"))
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>404 - Brainstorm not found</h1>")

        elif self.path.startswith("/api/runs/"):
            from infrastructure.run_queue import is_async_runs_enabled, get_global_queue_worker
            if not is_async_runs_enabled():
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"error": "Endpoint disabled."}')
                return

            run_id_raw = self.path[len("/api/runs/"):].split("?")[0].strip()
            try:
                valid_uuid = str(uuid.UUID(run_id_raw))
            except (ValueError, TypeError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid run_id format (UUID required)."}).encode("utf-8"))
                return

            worker = get_global_queue_worker()
            run_repo = worker.run_repo
            run_data = run_repo.get_run(valid_uuid)
            if not run_data:
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Run not found."}).encode("utf-8"))
                return

            filtered_run = {
                "run_id": run_data["id"],
                "action": run_data.get("action"),
                "preset": run_data.get("preset"),
                "repo": run_data.get("repo"),
                "branch": run_data.get("branch"),
                "status": run_data.get("status"),
                "progress": run_data.get("progress"),
                "current_step": run_data.get("current_step"),
                "error": run_data.get("error"),
                "created_at": run_data.get("created_at"),
                "started_at": run_data.get("started_at"),
                "completed_at": run_data.get("completed_at"),
                "result": run_data.get("result"),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
            self.end_headers()
            self.wfile.write(json.dumps(filtered_run, indent=2, ensure_ascii=False).encode("utf-8"))

        elif self.path.startswith("/diff/"):
            diff_name = self.path.replace("/diff/", "").split("?")[0].strip()
            diffs_root = DIFFS_DIR.resolve()
            diff_file = (DIFFS_DIR / diff_name).resolve()
            if str(diff_file).startswith(str(diffs_root) + os.sep) and diff_file.is_file():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(diff_file.read_bytes())
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>404 - Diff report not found</h1>")

        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Not found"}')

    def do_POST(self):
        """Handles interactive HTML form actions for Brainstorming & Buzz Chat."""
        content_length_header = self.headers.get("Content-Length")
        content_length = 0
        if content_length_header is not None:
            try:
                content_length = int(content_length_header)
            except (ValueError, TypeError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Invalid Content-Length."}')
                return

        if content_length > MAX_POST_BODY_BYTES:
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Payload Too Large. Maximum allowed size: 10MB."}')
            return

        post_body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
        form_data = urllib.parse.parse_qs(post_body)

        # 1. Login Handling
        if self.path == "/login":
            auth_secret = get_auth_secret()
            submitted = form_data.get("auth_token", [""])[0].strip()
            if auth_secret and len(auth_secret) >= MIN_AUTH_TOKEN_LENGTH and hmac.compare_digest(submitted, auth_secret):
                cookie_val = create_session_cookie_value(auth_secret)
                self.send_response(303)
                self.send_header(
                    "Set-Cookie",
                    f"{AUTH_SESSION_COOKIE_NAME}={cookie_val}; Path=/; Max-Age={AUTH_SESSION_DURATION_SECONDS}; HttpOnly; SameSite=Lax"
                )
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self.send_response(401)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(render_login_page("Invalid or unconfigured security token.").encode("utf-8"))
            return

        # Authentication Check for POST
        if not self._is_authenticated():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        bm = BrainstormManager(BRAINSTORMS_DIR)

        if self.path == "/brainstorm/new":
            task_raw = form_data.get("task", ["New work session"])[0].strip()
            repo_raw = form_data.get("repo", [""])[0].strip()
            preset_raw = form_data.get("preset", ["standard"])[0].strip()

            parsed = parse_command_text(
                task_raw,
                default_action="brainstorm",
                default_preset=preset_raw,
                default_repo=repo_raw or None
            )

            target_repo = resolve_catalog_repo(parsed["repo"] or repo_raw)
            if not target_repo:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(render_html_dashboard(error_msg="Please select a valid project from catalog before launching the task or session (Seleziona un progetto valido).").encode("utf-8"))
                return

            if parsed["action"] == "execute_task":
                cmd_args = build_orchestrator_cmd_args({
                    "action": "execute_task",
                    "task": parsed["text"],
                    "repo": target_repo,
                    "preset": parsed["preset"]
                })
                from infrastructure.run_queue import is_async_runs_enabled, get_global_queue_worker
                if is_async_runs_enabled():
                    worker = get_global_queue_worker()
                    worker.enqueue(cmd_args=cmd_args, action="execute_task", repo=target_repo, preset=parsed["preset"])
                else:
                    subprocess.Popen(cmd_args, cwd=str(BASE_DIR))

                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            # Brainstorming
            new_bs_id = bm.create_brainstorm(
                task=parsed["text"],
                preset=parsed["preset"],
                repo=target_repo
            )
            self.send_response(303)
            self.send_header("Location", f"/brainstorm/{new_bs_id}")
            self.end_headers()
            return

        elif "/chat" in self.path:
            # POST /brainstorm/<id>/chat
            parts = self.path.split("/")
            bs_id = parts[2] if len(parts) > 2 else ""
            msg = form_data.get("message", [""])[0].strip()
            if self._safe_brainstorm_id(bm, bs_id) and msg:
                bs = bm.load_brainstorm(bs_id)
                current_repo = bs.get("repo") or bs.get("project_name") if bs else None
                parsed = parse_command_text(msg, default_action="chat", default_repo=current_repo)

                if parsed["action"] == "execute_task":
                    target_repo = resolve_catalog_repo(parsed["repo"] or current_repo)
                    if not target_repo:
                        bm.post_chat_message(
                            bs_id,
                            message="⚠️ **No valid project selected for task execution.** Select a project first with `/project <name>` or specify `repo:<name>`.",
                            sender="Sol"
                        )
                    else:
                        cmd_args = build_orchestrator_cmd_args({
                            "action": "execute_task",
                            "task": parsed["text"],
                            "repo": target_repo,
                            "preset": parsed["preset"]
                        })
                        from infrastructure.run_queue import is_async_runs_enabled, get_global_queue_worker
                        if is_async_runs_enabled():
                            worker = get_global_queue_worker()
                            run_id = worker.enqueue(cmd_args=cmd_args, action="execute_task", repo=target_repo, preset=parsed["preset"])
                            bm.post_chat_message(
                                bs_id,
                                message=f"🚀 **Launching task execution on project {target_repo}:**\n_{parsed['text']}_\n\nPreset: `{parsed['preset']}` | Run queued (`{run_id[:8]}`).",
                                sender="Sol"
                            )
                        else:
                            subprocess.Popen(cmd_args, cwd=str(BASE_DIR))
                            bm.post_chat_message(
                                bs_id,
                                message=f"🚀 **Launching task execution on project {target_repo}:**\n_{parsed['text']}_\n\nPreset: `{parsed['preset']}` | Run started in background.",
                                sender="Sol"
                            )
                else:
                    bm.post_chat_message(bs_id, message=msg, sender="You")
            self.send_response(303)
            self.send_header("Location", f"/brainstorm/{bs_id}")
            self.end_headers()

        elif "/feedback" in self.path:
            # POST /brainstorm/<id>/feedback
            parts = self.path.split("/")
            bs_id = parts[2] if len(parts) > 2 else ""
            feedback = form_data.get("feedback", [""])[0]
            if self._safe_brainstorm_id(bm, bs_id) and feedback:
                bm.run_round(bs_id, user_feedback=feedback)
            self.send_response(303)
            self.send_header("Location", f"/brainstorm/{bs_id}")
            self.end_headers()

        elif "/approve" in self.path:
            # POST /brainstorm/<id>/approve
            parts = self.path.split("/")
            bs_id = parts[2] if len(parts) > 2 else ""
            if self._safe_brainstorm_id(bm, bs_id):
                bm.approve(bs_id, approved_by="web_dashboard")
            self.send_response(303)
            self.send_header("Location", f"/brainstorm/{bs_id}")
            self.end_headers()

        elif "/reject" in self.path:
            # POST /brainstorm/<id>/reject
            parts = self.path.split("/")
            bs_id = parts[2] if len(parts) > 2 else ""
            reason = form_data.get("reason", ["Rejected via Web Dashboard"])[0]
            if self._safe_brainstorm_id(bm, bs_id):
                bm.reject(bs_id, reason=reason)
            self.send_response(303)
            self.send_header("Location", f"/brainstorm/{bs_id}")
            self.end_headers()

        elif self.path in ["/api/run", "/api/execute"]:
            # API endpoint for secure and controlled execution of Taktstock actions
            if not post_body.strip():
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "ERROR",
                    "error": "Missing request body. Send a valid JSON object."
                }).encode("utf-8"))
                return

            try:
                payload = json.loads(post_body)
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "ERROR",
                    "error": f"Invalid JSON (JSON non valido): {str(e)}"
                }).encode("utf-8"))
                return

            # Rigorous payload validation
            is_valid, err_msg, sanitized_data = validate_api_run_payload(payload)
            if not is_valid:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "ERROR",
                    "error": err_msg
                }, ensure_ascii=False).encode("utf-8"))
                return

            # Safe command construction without shell=True (Anti Command-Injection)
            cmd_args = build_orchestrator_cmd_args(sanitized_data)
            logger.info(f"API Execution safely started (shell=False): {' '.join(cmd_args)}")

            from infrastructure.run_queue import is_async_runs_enabled
            if is_async_runs_enabled():
                from infrastructure.run_queue import get_global_queue_worker
                worker = get_global_queue_worker()
                run_id = worker.enqueue(
                    cmd_args=cmd_args,
                    cwd=BASE_DIR,
                    action=sanitized_data["action"],
                    preset=sanitized_data.get("preset"),
                    repo=sanitized_data.get("repo"),
                    metadata={
                        "task": sanitized_data.get("task"),
                        "chat_id": sanitized_data.get("chat_id"),
                        "message": sanitized_data.get("message"),
                    },
                )
                self.send_response(202)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "QUEUED",
                    "run_id": run_id,
                }).encode("utf-8"))
                return

            import subprocess
            try:
                proc = subprocess.run(
                    cmd_args,
                    shell=False,
                    capture_output=True,
                    text=True,
                    cwd=str(BASE_DIR),
                    timeout=1800
                )
                res_output = proc.stdout if proc.stdout else proc.stderr
                status_code = 200 if proc.returncode == 0 else 500
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "COMPLETED" if proc.returncode == 0 else "ERROR",
                    "code": proc.returncode,
                    "stdout": res_output.strip()
                }, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                logger.error(f"API subprocess execution error: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ERROR", "error": str(e)}).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "POST endpoint not found"}')

# Backward compatibility alias
UfficioHealthHandler = TaktstockHealthHandler

def run_health_server(port: int = PORT):
    # Mandatory authentication secret check at startup
    try:
        get_auth_secret(enforce_validity=True)
    except ValueError as e:
        logger.critical(f"❌ Critical security error on startup: {e}")
        sys.exit(1)

    from infrastructure.run_queue import is_async_runs_enabled, get_global_queue_worker, stop_global_queue_worker
    if is_async_runs_enabled():
        get_global_queue_worker()

    server_address = ("", port)
    httpd = HTTPServer(server_address, TaktstockHealthHandler)
    logger.info(f"🚀 Taktstock Web Dashboard running on http://0.0.0.0:{port}/")
    logger.info(f"📊 JSON API endpoint on http://0.0.0.0:{port}/health")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()
        if is_async_runs_enabled():
            stop_global_queue_worker()
        logger.info("Taktstock Web Dashboard stopped.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    run_health_server()
