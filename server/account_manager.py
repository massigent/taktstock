# ==============================================================================
# Taktstock Multi-Agent System - Multi-Account Codex & Failover Manager
# ==============================================================================
"""
Manages multiple OpenAI / Codex subscription accounts (Sol, Luna, Bonus):
- Assigns dedicated accounts per role (Sol -> sol, Luna -> luna)
- Performs hot-switching to the Bonus account if primary account runs out of quota
- Manages cooldown periods for temporarily blocked accounts
- Persists account state to disk (accounts_state.json) for sharing with health_server
"""

import os
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("TaktstockAccountManager")

RATE_LIMIT_KEYWORDS = [
    "429",
    "rate limit",
    "rate_limit_exceeded",
    "insufficient_quota",
    "quota exceeded",
    "usage limit",
    "capacity",
    "overloaded",
    "temporarily unavailable",
    "tokens per min",
    "requests per min"
]

DEFAULT_STATE_FILE = Path(os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME") or os.environ.get("ORCH_HOME") or (Path.home() / "taktstock")) / "state" / "accounts_state.json"

class CodexAccount:
    def __init__(self, name: str, api_key: Optional[str] = None, config_dir: Optional[Path] = None):
        self.name = name
        self.api_key = api_key
        self.config_dir = config_dir
        self.cooldown_until: float = 0.0
        self.failure_count: int = 0
        self.success_count: int = 0

    @property
    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def mark_rate_limited(self, cooldown_seconds: float = 1800.0):
        self.cooldown_until = time.time() + cooldown_seconds
        self.failure_count += 1
        logger.warning(
            f"Account [{self.name}] marked in cooldown for {cooldown_seconds}s (Failures: {self.failure_count})"
        )

    def mark_success(self):
        self.success_count += 1
        if self.failure_count > 0:
            self.failure_count = max(0, self.failure_count - 1)
        self.cooldown_until = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "available": self.is_available,
            "cooldown_until": self.cooldown_until,
            "failure_count": self.failure_count,
            "success_count": self.success_count
        }


class CodexAccountManager:
    def __init__(self, accounts: Optional[List[CodexAccount]] = None, state_file: Optional[Path] = None):
        self.accounts: List[CodexAccount] = accounts or []
        self.current_index: int = 0
        self.state_file = Path(state_file) if state_file else DEFAULT_STATE_FILE
        
        if not self.accounts:
            self._discover_accounts()

        self.load_state()

    def _discover_accounts(self):
        """Discovers accounts from ~/.codex/accounts/<name>/ directories or environment variables."""
        codex_home = Path.home() / ".codex"
        accounts_dir = codex_home / "accounts"
        if accounts_dir.exists() and accounts_dir.is_dir():
            for acc_path in sorted(accounts_dir.iterdir()):
                if acc_path.is_dir() and (acc_path / "auth.json").exists():
                    self.accounts.append(CodexAccount(name=acc_path.name, config_dir=acc_path))

        # If no local directories found, check if Codex sidecar is active on host
        if not self.accounts:
            sidecar_enabled = (os.environ.get("TAKTSTOCK_HOST_CODEX_SIDECAR") or os.environ.get("UFFICIO_HOST_CODEX_SIDECAR", "")).strip().lower() in {"1", "true", "yes", "on"}
            sidecar_token_file = os.environ.get("TAKTSTOCK_SIDECAR_TOKEN_FILE") or os.environ.get("UFFICIO_SIDECAR_TOKEN_FILE")
            if sidecar_enabled or os.environ.get("CODEX_SOCKET_PATH") or sidecar_token_file:
                for name in ["sol", "luna", "bonus"]:
                    self.accounts.append(CodexAccount(name=name))

        # If no directories, fallback to OPENAI_API_KEYS
        if not self.accounts:
            keys_env = os.environ.get("OPENAI_API_KEYS", "").strip()
            if keys_env:
                for idx, key in enumerate(keys_env.split(",")):
                    k = key.strip()
                    if k:
                        self.accounts.append(CodexAccount(name=f"env_key_{idx+1}", api_key=k))

        # Fallback default account
        if not self.accounts:
            self.accounts.append(CodexAccount(name="default_account", config_dir=codex_home))

        logger.info(f"Configured Codex accounts: {[a.name for a in self.accounts]}")

    def save_state(self):
        """Saves the current account state to disk."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state_data = {
                "accounts": [a.to_dict() for a in self.accounts],
                "current_index": self.current_index,
                "current_account": self.get_current_account().name,
                "timestamp": datetime.now().isoformat()
            }
            self.state_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Unable to save account state: {e}")

    def load_state(self):
        """Loads persisted state to maintain cooldowns and counters."""
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            saved_accounts = {a["name"]: a for a in data.get("accounts", [])}
            for acc in self.accounts:
                if acc.name in saved_accounts:
                    s = saved_accounts[acc.name]
                    acc.cooldown_until = s.get("cooldown_until", 0.0)
                    acc.failure_count = s.get("failure_count", 0)
                    acc.success_count = s.get("success_count", 0)
            self.current_index = data.get("current_index", 0) % max(1, len(self.accounts))
        except Exception as e:
            logger.debug(f"Unable to load account state: {e}")

    def get_account_by_name(self, name: str) -> Optional[CodexAccount]:
        for a in self.accounts:
            if a.name == name:
                return a
        return None

    def get_account_for_role(self, role: str = "sol") -> Optional[CodexAccount]:
        """
        Assigns the appropriate account for the requested role:
        - sol -> account 'sol', falls back to 'bonus' if in cooldown or unavailable
        - luna / terra -> account 'luna', falls back to 'bonus' if in cooldown
        - if bonus is also in cooldown or not configured, returns None
        """
        primary_name = "sol" if role.lower() in ["sol", "director"] else "luna"
        primary = self.get_account_by_name(primary_name)
        bonus = self.get_account_by_name("bonus")

        if primary and primary.is_available:
            return primary
        
        if bonus and bonus.is_available:
            logger.warning(f"Primary account [{primary_name}] unavailable or in cooldown. Using Bonus account.")
            return bonus

        if not primary and not bonus:
            logger.warning(f"No account [{primary_name}] or [bonus] configured.")
            return None

        # If both are in cooldown, log explicit warning
        logger.warning(f"Both [{primary_name}] and [bonus] accounts are temporarily in cooldown.")
        return None

    def get_current_account(self) -> CodexAccount:
        if not self.accounts:
            return CodexAccount(name="default")
        return self.accounts[self.current_index]

    def get_next_available_account(self) -> Optional[CodexAccount]:
        """Finds the next available account that is not in cooldown."""
        total = len(self.accounts)
        for offset in range(total):
            idx = (self.current_index + offset) % total
            candidate = self.accounts[idx]
            if candidate.is_available:
                self.current_index = idx
                self.save_state()
                return candidate
        best = min(self.accounts, key=lambda a: a.cooldown_until)
        self.current_index = self.accounts.index(best)
        self.save_state()
        return best

    def rotate_to_next(self, reason: str = "manual") -> CodexAccount:
        """Forces rotation to the next account."""
        prev = self.get_current_account().name
        self.current_index = (self.current_index + 1) % len(self.accounts)
        curr = self.get_current_account().name
        logger.info(f"Codex account rotation: [{prev}] -> [{curr}] (Reason: {reason})")
        self.save_state()
        return self.get_current_account()

    def is_rate_limit_error(self, output: str) -> bool:
        if not output:
            return False
        out_lower = output.lower()
        return any(kw in out_lower for kw in RATE_LIMIT_KEYWORDS)

    def handle_possible_error(self, output: str) -> bool:
        """
        If quota/rate limit error is detected, mark account and rotate.
        Returns True if a hot-switch occurred (caller should retry command).
        """
        if self.is_rate_limit_error(output):
            current = self.get_current_account()
            logger.warning(f"Rate Limit detected on account [{current.name}]!")
            current.mark_rate_limited(cooldown_seconds=1800.0)
            self.rotate_to_next(reason="rate_limit_failover")
            self.save_state()
            return True
        return False

    def apply_account_env(self, env: Dict[str, str], account: Optional[CodexAccount] = None) -> Dict[str, str]:
        acc = account or self.get_current_account()
        custom_env = env.copy()
        if acc.api_key:
            custom_env["OPENAI_API_KEY"] = acc.api_key
        if acc.config_dir:
            custom_env["CODEX_HOME"] = str(acc.config_dir)
        return custom_env
