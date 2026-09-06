#!/usr/bin/env python3
"""
Brainstorm State Adapter for Shadow Persistence
------------------------------------------------
Gestisce la shadow persistence (lettura e scrittura ombra) in SQLite per BrainstormManager,
mantenendo i file JSON come unica fonte autorevole della verità.

Flag supportati:
- TAKTSTOCK_SQLITE_SHADOW_WRITE (default 0, fallback UFFICIO_SQLITE_SHADOW_WRITE)
- TAKTSTOCK_SQLITE_SHADOW_READ (default 0, fallback UFFICIO_SQLITE_SHADOW_READ)

Principi:
1. Il JSON viene sempre scritto e letto per primo.
2. Shadow write: replica la sessione e i messaggi in SQLite senza mai bloccare o alterare il flusso JSON in caso di errore.
3. Shadow read: confronta semanticamente i dati JSON con SQLite e registra eventuali divergenze, restituendo sempre il JSON.
4. Con flag disattivati, non apre né crea il database SQLite.
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
    Verifica se un feature flag è abilitato rispettando la precedenza:
    1. Parametro esplicito (se non None)
    2. Variabile d'ambiente (se presente in os.environ)
    3. Variabile fallback / mappatura automatica TAKTSTOCK_ <-> UFFICIO_
    4. Sezione 'storage' in config.json
    5. Fallback fail-closed: False
    """
    if explicit_val is not None:
        return bool(explicit_val)

    # 1. Variabile d'ambiente esplicita
    if flag_name in os.environ:
        val = os.environ[flag_name].strip().lower()
        return val in ("1", "true", "yes", "on")

    # 1b. Fallback env o cross-mapping
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

    # 2. Fallback su config.json
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
    Adapter per shadow persistence in SQLite delle sessioni di brainstorming.
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

        # Inizializza DatabaseManager solo se almeno una shadow feature è attiva o db_manager è stato iniettato
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
        Replica in SQLite la sessione e i messaggi dopo un salvataggio JSON riuscito.
        Non solleva mai eccezioni verso il chiamante.
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

            # Preserva metadati non mappati
            reserved_keys = {"id", "chat_id", "title", "status", "created_at", "updated_at", "messages"}
            metadata = {k: v for k, v in data.items() if k not in reserved_keys}
            messages_raw = data.get("messages", [])

            with self._db_manager.transaction() as conn:
                # Upsert sessione
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

                # Upsert messaggi
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

            logger.debug(f"[SHADOW_WRITE] Sessione {session_id} e {len(messages_raw)} messaggi replicati in SQLite.")

        except Exception as e:
            logger.warning(f"[SHADOW_WRITE_ERROR] Errore replica SQLite per sessione {session_id}: {e}")

    def shadow_set_active(self, chat_id: str, brainstorm_id: str) -> None:
        """Aggiorna lo stato della sessione attiva in SQLite se abilitato."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            with self._db_manager.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET status = 'active', updated_at = ? WHERE id = ?",
                    (utc_now_iso(), brainstorm_id),
                )
        except Exception as e:
            logger.warning(f"[SHADOW_SET_ACTIVE_ERROR] Errore aggiornamento active session in SQLite {brainstorm_id}: {e}")

    def shadow_compare(self, session_id: str, json_data: Dict[str, Any]) -> List[str]:
        """
        Confronta semanticamente i dati JSON con i dati presenti in SQLite.
        Registra eventuali divergenze su status, chat_id, title, preset, final_plan,
        e messaggi (content, author, role, count).
        Restituisce la lista delle differenze. Non solleva mai eccezioni.
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
                diff = f"Sessione {session_id} presente nel JSON ma assente in SQLite"
                self.last_diffs.append(diff)
                logger.warning(f"[SHADOW_DIFF] Divergenza per sessione {session_id}: {diff}")
                return self.last_diffs

            diffs: List[str] = []

            # 1. Confronto status (normalizzato, senza presupporre entrambi truthy)
            j_status = str(json_data.get("status") or "")
            s_status = str(sql_data.get("status") or "")
            if j_status != s_status:
                diffs.append(f"status mismatch: json='{j_status}' vs sqlite='{s_status}'")

            # 2. Confronto chat_id
            j_chat = str(json_data.get("chat_id") or "")
            s_chat = str(sql_data.get("chat_id") or "")
            if j_chat != s_chat:
                diffs.append(f"chat_id mismatch: json='{j_chat}' vs sqlite='{s_chat}'")

            # 3. Confronto titolo / task
            j_title = str(json_data.get("task") or json_data.get("title") or "")
            s_title = str(sql_data.get("title") or "")
            if j_title != s_title:
                diffs.append(f"title mismatch: json='{j_title}' vs sqlite='{s_title}'")

            # 4. Confronto metadata essenziali (preset, final_plan)
            j_preset = str(json_data.get("preset") or "")
            s_preset = str((sql_data.get("metadata") or {}).get("preset") or "")
            if j_preset != s_preset:
                diffs.append(f"preset mismatch: json='{j_preset}' vs sqlite='{s_preset}'")

            j_plan = str(json_data.get("final_plan") or "")
            s_plan = str((sql_data.get("metadata") or {}).get("final_plan") or "")
            if j_plan != s_plan:
                diffs.append(f"final_plan mismatch: json='{j_plan}' vs sqlite='{s_plan}'")

            # 5. Confronto messaggi (conteggio, testo, autore, ruolo)
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
                logger.warning(f"[SHADOW_DIFF] Divergenze rilevate per sessione {session_id}: {diffs}")

            return diffs

        except Exception as e:
            logger.warning(f"[SHADOW_READ_ERROR] Errore durante confronto shadow read per {session_id}: {e}")
            return []
