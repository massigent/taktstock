#!/usr/bin/env python3
"""
Brainstorm State Adapter for Shadow Persistence
------------------------------------------------
Manages shadow persistence (shadow read and write) in SQLite for BrainstormManager,
maintaining JSON files as the single source of truth.

Supported flags:
- TAKTSTOCK_SQLITE_SHADOW_WRITE (default 0, fallback UFFICIO_SQLITE_SHADOW_WRITE)
- TAKTSTOCK_SQLITE_SHADOW_READ (default 0, fallback UFFICIO_SQLITE_SHADOW_READ)

Principles:
1. JSON is always written and read first.
2. Shadow write: replicates session and messages in SQLite without blocking or altering JSON flow on error.
3. Shadow read: semantically compares JSON data with SQLite and records divergences, always returning JSON.
4. When flags are disabled, neither opens nor creates the SQLite database.
"""

import os
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .database import DatabaseManager, DatabaseError, utc_now_iso
from .session_repository import SessionRepository

logger = logging.getLogger("TaktstockBrainstormShadow")


def _is_flag_enabled(
    flag_name: str,
    explicit_val: Optional[bool] = None,
    config_path: Optional[Union[str, Path]] = None,
    fallback_env: Optional[str] = None
) -> bool:
    """
    Checks whether a feature flag is enabled respecting precedence:
    1. Explicit parameter (if not None)
    2. Environment variable (if present in os.environ)
    3. Fallback variable / automatic mapping TAKTSTOCK_ <-> UFFICIO_
    4. Storage section in config.json
    5. Fallback fail-closed: False
    """
    if explicit_val is not None:
        return bool(explicit_val)

    # 1. Explicit environment variable
    if flag_name in os.environ:
        val = os.environ[flag_name].strip().lower()
        return val in ("1", "true", "yes", "on")

    # 1b. Fallback env or cross-mapping
    if fallback_env and fallback_env in os.environ:
        val = os.environ[fallback_env].strip().lower()
        return val in ("1", "true", "yes", "on")

    if flag_name.startswith("TAKTSTOCK_"):
        legacy = "UFFICIO_" + flag_name[len("TAKTSTOCK_"):]
        if legacy in os.environ:
            val = os.environ[legacy].strip().lower()
            return val in ("1", "true", "yes", "on")
    elif flag_name.startswith("UFFICIO_"):
        modern = "TAKTSTOCK_" + flag_name[len("UFFICIO_"):]
        if modern in os.environ:
            val = os.environ[modern].strip().lower()
            return val in ("1", "true", "yes", "on")

    # 2. Fallback to config.json
    target_key = flag_name.lower().replace("taktstock_", "").replace("ufficio_", "")
    candidate_paths: List[Path] = []
    if config_path is not None:
        candidate_paths.append(Path(config_path))
    else:
        current_dir = Path(__file__).resolve().parent
        candidate_paths.extend([
            current_dir / "config.json",
            current_dir.parent / "config.json",
            current_dir.parent.parent / "config.json",
            current_dir.parent / "server" / "config.json",
        ])

    for cp in candidate_paths:
        if cp.exists() and cp.is_file():
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if isinstance(cfg, dict):
                    storage = cfg.get("storage")
                    if isinstance(storage, dict) and target_key in storage:
                        s_val = storage[target_key]
                        if isinstance(s_val, bool):
                            return s_val
                        if isinstance(s_val, str):
                            return s_val.strip().lower() in ("1", "true", "yes", "on")
                        if isinstance(s_val, (int, float)):
                            return bool(s_val)
                    if target_key in cfg:
                        c_val = cfg[target_key]
                        if isinstance(c_val, bool):
                            return c_val
                        if isinstance(c_val, str):
                            return c_val.strip().lower() in ("1", "true", "yes", "on")
            except Exception:
                pass
            break

    return False


class BrainstormStateAdapter:
    """
    Adapter for shadow persistence in SQLite of brainstorming sessions.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        shadow_write: Optional[bool] = None,
        shadow_read: Optional[bool] = None,
        config_path: Optional[Union[str, Path]] = None,
    ):
        self.shadow_write = _is_flag_enabled("TAKTSTOCK_SQLITE_SHADOW_WRITE", shadow_write, config_path=config_path, fallback_env="UFFICIO_SQLITE_SHADOW_WRITE")
        self.shadow_read = _is_flag_enabled("TAKTSTOCK_SQLITE_SHADOW_READ", shadow_read, config_path=config_path, fallback_env="UFFICIO_SQLITE_SHADOW_READ")

        self._db_manager = db_manager
        self._session_repo: Optional[SessionRepository] = None
        self.last_diffs: List[str] = []

        # Initialize DatabaseManager only if at least one shadow feature is active or db_manager was injected
        if self._db_manager is None and (self.shadow_write or self.shadow_read):
            self._db_manager = DatabaseManager()

    @property
    def db_manager(self) -> Optional[DatabaseManager]:
        return self._db_manager

    def get_session_repo(self) -> Optional[SessionRepository]:
        if self._session_repo is None and self._db_manager is not None:
            self._session_repo = SessionRepository(self._db_manager)
        return self._session_repo

    def shadow_save(self, data: Dict[str, Any]) -> None:
        """
        Replicates session and messages to SQLite after a successful JSON save.
        Never raises exceptions to the caller.
        """
        if not self.shadow_write or not self._db_manager or not isinstance(data, dict):
            return

        session_id = data.get("id")
        if not session_id:
            return

        try:
            chat_id = str(data.get("chat_id") or "")
            title = data.get("task") or data.get("title") or "Brainstorming Session"
            status = data.get("status") or "active"
            created_at = data.get("created_at") or utc_now_iso()
            updated_at = data.get("updated_at") or created_at

            # Preserve unmapped metadata
            reserved_keys = {"id", "chat_id", "title", "status", "created_at", "updated_at", "messages"}
            metadata = {k: v for k, v in data.items() if k not in reserved_keys}
            messages_raw = data.get("messages", [])

            with self._db_manager.transaction() as conn:
                # Upsert session
                conn.execute(
                    """
                    INSERT INTO sessions (id, chat_id, title, status, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        chat_id = excluded.chat_id,
                        title = excluded.title,
                        status = excluded.status,
                        metadata = excluded.metadata,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, chat_id, title, status, json.dumps(metadata), created_at, updated_at),
                )

                # Upsert messages
                for idx, msg in enumerate(messages_raw):
                    if not isinstance(msg, dict):
                        continue
                    mid = str(msg.get("id") or f"{session_id}_msg_{idx}")
                    sender = msg.get("sender", "")
                    agent = msg.get("agent", "")
                    role = msg.get("role") or ("user" if sender.lower() == "user" else "assistant")
                    author = agent or sender or msg.get("author") or "system"
                    content = msg.get("text") or msg.get("content") or ""
                    m_created = msg.get("timestamp") or msg.get("created_at") or updated_at

                    msg_reserved = {"id", "sender", "agent", "author", "text", "content", "timestamp", "created_at", "role"}
                    m_meta = {k: v for k, v in msg.items() if k not in msg_reserved}

                    conn.execute(
                        """
                        INSERT INTO messages (id, session_id, role, author, content, metadata, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            role = excluded.role,
                            author = excluded.author,
                            content = excluded.content,
                            metadata = excluded.metadata,
                            created_at = excluded.created_at
                        """,
                        (mid, session_id, role, author, content, json.dumps(m_meta), m_created),
                    )

            logger.debug(f"[SHADOW_WRITE] Session {session_id} and {len(messages_raw)} messages replicated to SQLite.")

        except Exception as e:
            logger.warning(f"[SHADOW_WRITE_ERROR] SQLite replication error for session {session_id}: {e}")

    def shadow_set_active(self, chat_id: str, brainstorm_id: str) -> None:
        """Updates active session status in SQLite if enabled."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            with self._db_manager.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET status = 'active', updated_at = ? WHERE id = ?",
                    (utc_now_iso(), brainstorm_id),
                )
        except Exception as e:
            logger.warning(f"[SHADOW_SET_ACTIVE_ERROR] Error updating active session in SQLite {brainstorm_id}: {e}")

    def shadow_compare(self, session_id: str, json_data: Dict[str, Any]) -> List[str]:
        """
        Semantically compares JSON data with data present in SQLite.
        Records divergences on status, chat_id, title, preset, final_plan,
        and messages (content, author, role, count).
        Returns list of differences. Never raises exceptions.
        """
        self.last_diffs = []
        if not self.shadow_read or not self._db_manager or not isinstance(json_data, dict):
            return []

        try:
            repo = self.get_session_repo()
            if not repo:
                return []

            sql_data = repo.get_session(session_id, include_messages=True)
            if sql_data is None:
                diff = f"Session {session_id} present in JSON but missing in SQLite (presente nel JSON ma assente in SQLite)"
                self.last_diffs.append(diff)
                logger.warning(f"[SHADOW_DIFF] Divergence for session {session_id}: {diff}")
                return self.last_diffs

            diffs: List[str] = []

            # 1. Status comparison (normalized)
            j_status = str(json_data.get("status") or "")
            s_status = str(sql_data.get("status") or "")
            if j_status != s_status:
                diffs.append(f"status mismatch: json='{j_status}' vs sqlite='{s_status}'")

            # 2. chat_id comparison
            j_chat = str(json_data.get("chat_id") or "")
            s_chat = str(sql_data.get("chat_id") or "")
            if j_chat != s_chat:
                diffs.append(f"chat_id mismatch: json='{j_chat}' vs sqlite='{s_chat}'")

            # 3. Title / task comparison
            j_title = str(json_data.get("task") or json_data.get("title") or "")
            s_title = str(sql_data.get("title") or "")
            if j_title != s_title:
                diffs.append(f"title mismatch: json='{j_title}' vs sqlite='{s_title}'")

            # 4. Essential metadata comparison (preset, final_plan)
            j_preset = str(json_data.get("preset") or "")
            s_preset = str((sql_data.get("metadata") or {}).get("preset") or "")
            if j_preset != s_preset:
                diffs.append(f"preset mismatch: json='{j_preset}' vs sqlite='{s_preset}'")

            j_plan = str(json_data.get("final_plan") or "")
            s_plan = str((sql_data.get("metadata") or {}).get("final_plan") or "")
            if j_plan != s_plan:
                diffs.append(f"final_plan mismatch: json='{j_plan}' vs sqlite='{s_plan}'")

            # 5. Messages comparison (count, text, author, role)
            j_msgs = json_data.get("messages", [])
            s_msgs = sql_data.get("messages", [])
            if len(j_msgs) != len(s_msgs):
                diffs.append(f"messages count mismatch: json={len(j_msgs)} vs sqlite={len(s_msgs)}")
            else:
                for idx, (jm, sm) in enumerate(zip(j_msgs, s_msgs)):
                    j_content = str(jm.get("text") or jm.get("content") or "")
                    s_content = str(sm.get("content") or "")
                    if j_content != s_content:
                        diffs.append(f"message[{idx}] content mismatch: json='{j_content}' vs sqlite='{s_content}'")

                    j_author = str(jm.get("agent") or jm.get("sender") or jm.get("author") or "system")
                    s_author = str(sm.get("author") or "")
                    if j_author != s_author:
                        diffs.append(f"message[{idx}] author mismatch: json='{j_author}' vs sqlite='{s_author}'")

                    j_role = str(jm.get("role") or ("user" if str(jm.get("sender", "")).lower() == "user" else "assistant"))
                    s_role = str(sm.get("role") or "")
                    if j_role != s_role:
                        diffs.append(f"message[{idx}] role mismatch: json='{j_role}' vs sqlite='{s_role}'")

            if diffs:
                self.last_diffs = diffs
                logger.warning(f"[SHADOW_DIFF] Divergences detected for session {session_id}: {diffs}")

            return diffs

        except Exception as e:
            logger.warning(f"[SHADOW_READ_ERROR] Error during shadow read comparison for {session_id}: {e}")
            return []
