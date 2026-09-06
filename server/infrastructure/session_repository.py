#!/usr/bin/env python3
"""
Session & Message Repository for Taktstock
------------------------------------------
SQLite persistence management for sessions and messages:
- Session creation
- Status updates
- Message logging
- Session retrieval with chronologically sorted messages
- Listing and filtering sessions
"""

import json
import uuid
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from .database import DatabaseManager, DatabaseError, utc_now_iso

logger = logging.getLogger("TaktstockSessionRepository")


class SessionRepository:
    """
    Repository for managing work/chat sessions and message histories.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    def _parse_json(self, value: Optional[str]) -> Dict[str, Any]:
        if not value:
            return {}
        try:
            return json.loads(value)
        except Exception:
            return {}

    def _format_session_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "chat_id": row["chat_id"],
            "title": row["title"],
            "status": row["status"],
            "metadata": self._parse_json(row["metadata"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _format_message_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "author": row["author"],
            "content": row["content"],
            "metadata": self._parse_json(row["metadata"]),
            "created_at": row["created_at"],
        }

    def create_session(
        self,
        chat_id: str,
        title: Optional[str] = None,
        status: str = "active",
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates a new persistent session.
        """
        if not chat_id:
            raise ValueError("chat_id is required to create a session.")

        sid = session_id or str(uuid.uuid4())
        now = utc_now_iso()
        meta_json = json.dumps(metadata or {})

        query = """
            INSERT INTO sessions (id, chat_id, title, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self.db_manager.transaction() as conn:
                conn.execute(query, (sid, str(chat_id), title, status, meta_json, now, now))
        except sqlite3.Error as e:
            logger.error(f"Error creating session {sid}: {e}")
            raise DatabaseError(f"Cannot create session: {e}") from e

        return {
            "id": sid,
            "chat_id": str(chat_id),
            "title": title,
            "status": status,
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }

    def update_session_status(
        self,
        session_id: str,
        status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Updates the status and optionally metadata of a session.
        """
        now = utc_now_iso()
        try:
            with self.db_manager.transaction() as conn:
                if metadata is not None:
                    # Retrieve existing metadata for merging
                    cur = conn.execute("SELECT metadata FROM sessions WHERE id = ?", (session_id,))
                    row = cur.fetchone()
                    if not row:
                        return False
                    existing_meta = self._parse_json(row["metadata"])
                    existing_meta.update(metadata)
                    cur = conn.execute(
                        "UPDATE sessions SET status = ?, metadata = ?, updated_at = ? WHERE id = ?",
                        (status, json.dumps(existing_meta), now, session_id),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                        (status, now, session_id),
                    )
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error updating session status {session_id}: {e}")
            raise DatabaseError(f"Cannot update session {session_id}: {e}") from e

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        author: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Adds a message to the session history.
        """
        if not session_id or not role or content is None:
            raise ValueError("session_id, role, and content are required.")

        mid = message_id or str(uuid.uuid4())
        now = utc_now_iso()
        meta_json = json.dumps(metadata or {})

        msg_query = """
            INSERT INTO messages (id, session_id, role, author, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        sess_query = "UPDATE sessions SET updated_at = ? WHERE id = ?"

        try:
            with self.db_manager.transaction() as conn:
                conn.execute(msg_query, (mid, session_id, role, author, content, meta_json, now))
                conn.execute(sess_query, (now, session_id))
        except sqlite3.IntegrityError as ie:
            logger.error(f"Message integrity error on session {session_id}: {ie}")
            raise DatabaseError(f"FK constraint or key violation for session {session_id}: {ie}") from ie
        except sqlite3.Error as e:
            logger.error(f"Error adding message to session {session_id}: {e}")
            raise DatabaseError(f"Cannot add message: {e}") from e

        return {
            "id": mid,
            "session_id": session_id,
            "role": role,
            "author": author,
            "content": content,
            "metadata": metadata or {},
            "created_at": now,
        }

    def get_session(self, session_id: str, include_messages: bool = True) -> Optional[Dict[str, Any]]:
        """
        Retrieves a session by ID, optionally including all chronologically sorted messages.
        """
        try:
            with self.db_manager.connection() as conn:
                cur = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
                row = cur.fetchone()
                if not row:
                    return None

                session_dict = self._format_session_row(row)
                if include_messages:
                    msg_cur = conn.execute(
                        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                        (session_id,),
                    )
                    session_dict["messages"] = [self._format_message_row(r) for r in msg_cur.fetchall()]
                return session_dict
        except sqlite3.Error as e:
            logger.error(f"Error retrieving session {session_id}: {e}")
            raise DatabaseError(f"Cannot retrieve session {session_id}: {e}") from e

    def get_active_session_by_chat_id(self, chat_id: str, include_messages: bool = True) -> Optional[Dict[str, Any]]:
        """
        Retrieves the most recent active session for a given chat_id.
        """
        try:
            with self.db_manager.connection() as conn:
                cur = conn.execute(
                    "SELECT * FROM sessions WHERE chat_id = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
                    (str(chat_id),),
                )
                row = cur.fetchone()
                if not row:
                    return None

                session_dict = self._format_session_row(row)
                if include_messages:
                    msg_cur = conn.execute(
                        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                        (session_dict["id"],),
                    )
                    session_dict["messages"] = [self._format_message_row(r) for r in msg_cur.fetchall()]
                return session_dict
        except sqlite3.Error as e:
            logger.error(f"Error retrieving active session for chat_id {chat_id}: {e}")
            raise DatabaseError(f"Cannot retrieve active session for chat_id {chat_id}: {e}") from e

    def list_sessions(
        self,
        chat_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Lists sessions with optional filters, sorted by update date descending.
        """
        clauses = []
        params: List[Any] = []

        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(str(chat_id))
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM sessions {where_sql} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([safe_limit, safe_offset])

        try:
            with self.db_manager.connection() as conn:
                cur = conn.execute(query, params)
                return [self._format_session_row(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error listing sessions: {e}")
            raise DatabaseError(f"Cannot list sessions: {e}") from e

    def delete_session(self, session_id: str) -> bool:
        """
        Deletes a session (and by ON DELETE CASCADE, its related messages).
        """
        try:
            with self.db_manager.transaction() as conn:
                cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error deleting session {session_id}: {e}")
            raise DatabaseError(f"Cannot delete session {session_id}: {e}") from e
