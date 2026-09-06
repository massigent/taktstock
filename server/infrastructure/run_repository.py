#!/usr/bin/env python3
"""
Run & Execution Repository for Taktstock
----------------------------------------
SQLite persistence management for runs, executions, and async tasks:
- Run creation (initial state 'queued')
- Status, progress, and result/error updates
- Retrieval of run by ID
- Listing of recent runs with filters
"""

import json
import uuid
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from .database import DatabaseManager, DatabaseError, utc_now_iso

logger = logging.getLogger("TaktstockRunRepository")


class RunRepository:
    """
    Repository for monitoring and persisting orchestrator execution runs.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    def _parse_json(self, value: Optional[str]) -> Optional[Dict[str, Any]]:
        if not value:
            return None
        try:
            return json.loads(value)
        except Exception:
            return None

    def _format_run_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "action": row["action"],
            "status": row["status"],
            "progress": row["progress"],
            "current_step": row["current_step"],
            "preset": row["preset"],
            "repo": row["repo"],
            "branch": row["branch"],
            "result": self._parse_json(row["result"]),
            "error": row["error"],
            "metadata": self._parse_json(row["metadata"]) or {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    def create_run(
        self,
        action: str,
        session_id: Optional[str] = None,
        preset: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        status: str = "queued",
        progress: int = 0,
        current_step: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates a new entry for an execution run.
        """
        if not action:
            raise ValueError("action is required to create a run.")

        rid = run_id or str(uuid.uuid4())
        now = utc_now_iso()
        meta_json = json.dumps(metadata or {})

        query = """
            INSERT INTO runs (
                id, session_id, action, status, progress, current_step,
                preset, repo, branch, result, error, metadata,
                created_at, updated_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL)
        """
        try:
            with self.db_manager.transaction() as conn:
                conn.execute(
                    query,
                    (
                        rid,
                        session_id,
                        action,
                        status,
                        progress,
                        current_step,
                        preset,
                        repo,
                        branch,
                        meta_json,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as ie:
            logger.error(f"Integrity error for run {rid} (session_id={session_id}): {ie}")
            raise DatabaseError(f"FK constraint violation on session_id={session_id}: {ie}") from ie
        except sqlite3.Error as e:
            logger.error(f"Error creating run {rid}: {e}")
            raise DatabaseError(f"Cannot create run: {e}") from e

        return {
            "id": rid,
            "session_id": session_id,
            "action": action,
            "status": status,
            "progress": progress,
            "current_step": current_step,
            "preset": preset,
            "repo": repo,
            "branch": branch,
            "result": None,
            "error": None,
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }

    def update_run_status(
        self,
        run_id: str,
        status: str,
        progress: Optional[int] = None,
        current_step: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Updates status, progress, and outcome of a run.
        If terminal or waiting status, sets completed_at if not already set.
        """
        now = utc_now_iso()
        terminal_statuses = {"completed", "failed", "rejected", "waiting_for_approval", "cancelled"}

        try:
            with self.db_manager.transaction() as conn:
                cur = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
                row = cur.fetchone()
                if not row:
                    return False

                fields = ["status = ?", "updated_at = ?"]
                params: List[Any] = [status, now]

                if progress is not None:
                    fields.append("progress = ?")
                    params.append(progress)

                if current_step is not None:
                    fields.append("current_step = ?")
                    params.append(current_step)

                if result is not None:
                    fields.append("result = ?")
                    params.append(json.dumps(result))

                if error is not None:
                    fields.append("error = ?")
                    params.append(error)

                if metadata is not None:
                    existing_meta = self._parse_json(row["metadata"]) or {}
                    existing_meta.update(metadata)
                    fields.append("metadata = ?")
                    params.append(json.dumps(existing_meta))

                if status.lower() in terminal_statuses:
                    fields.append("completed_at = COALESCE(completed_at, ?)")
                    params.append(now)

                params.append(run_id)
                query = f"UPDATE runs SET {', '.join(fields)} WHERE id = ?"
                cur = conn.execute(query, params)
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error updating run {run_id}: {e}")
            raise DatabaseError(f"Cannot update run {run_id}: {e}") from e

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves details of a run by ID.
        """
        try:
            with self.db_manager.connection() as conn:
                cur = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return self._format_run_row(row)
        except sqlite3.Error as e:
            logger.error(f"Error retrieving run {run_id}: {e}")
            raise DatabaseError(f"Cannot retrieve run {run_id}: {e}") from e

    def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        action: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists runs with optional filters, sorted by creation date descending.
        """
        clauses = []
        params: List[Any] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)

        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM runs {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([safe_limit, safe_offset])

        try:
            with self.db_manager.connection() as conn:
                cur = conn.execute(query, params)
                return [self._format_run_row(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error listing runs: {e}")
            raise DatabaseError(f"Cannot list runs: {e}") from e

    def delete_run(self, run_id: str) -> bool:
        """
        Deletes a run by ID.
        """
        try:
            with self.db_manager.transaction() as conn:
                cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
                return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Error deleting run {run_id}: {e}")
            raise DatabaseError(f"Cannot delete run {run_id}: {e}") from e
