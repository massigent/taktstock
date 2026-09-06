#!/usr/bin/env python3
"""
Legacy State Importer for Taktstock
-----------------------------------
One-time, idempotent, and verifiable migration from legacy JSON state files to SQLite.
Maps:
- brainstorms (bs_*.json / brainstorm_*.json) -> sessions + messages
- active brainstorms (active_*.json / brainstorm_active.json) -> status='active' on sessions with timestamp protection
- pending runs (pending_*.json) -> runs (status='waiting_for_approval') with deterministic ID
- completed runs (completed_*.json) -> runs (status='completed') with deterministic ID
- checkpoint runs (checkpoint_*.json) -> runs (status='running', result with phase/tokens/data) with deterministic ID

Features:
- Idempotent: tracks SHA-256 fingerprint and canonical key <state_dir.resolve()>::<relative_path>
- Deterministic run identity: legacy_run_<sha256(import_key)[:32]> (stable for same file, distinct across state_dir)
- Total protection of newer SQLite data via datetime UTC parsing and normalization (sessions, messages, runs, active mapping)
- Ordered multi-pass execution: sessions first, then active mappings, finally runs
- Reports skipped_missing_session on orphan active mappings and skipped_sqlite_newer on newer SQLite records
- Supports dry_run mode
- Error tolerance on single corrupted JSON files
- Zero modifications or deletions of original JSON files
"""

import os
import json
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .database import DatabaseManager, DatabaseError, utc_now_iso

logger = logging.getLogger("TaktstockLegacyImporter")


def compute_file_sha256(file_path: Path) -> str:
    """Computes SHA-256 hash of file content."""
    hasher = hashlib.sha256()
    hasher.update(file_path.read_bytes())
    return hasher.hexdigest()


def generate_deterministic_run_id(import_key: str) -> str:
    """Generates a stable deterministic run_id based on canonical import key."""
    key_hash = hashlib.sha256(import_key.encode("utf-8")).hexdigest()[:32]
    return f"legacy_run_{key_hash}"


def parse_iso_utc(ts_val: Any) -> Optional[datetime]:
    """
    Parses and normalizes an ISO string or timestamp into UTC timezone datetime.
    Returns None if value is null, empty, or unparseable.
    """
    if not ts_val:
        return None
    if isinstance(ts_val, datetime):
        if ts_val.tzinfo is None:
            return ts_val.replace(tzinfo=timezone.utc)
        return ts_val.astimezone(timezone.utc)
    if not isinstance(ts_val, str):
        return None
    ts_str = ts_val.strip()
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def is_sqlite_record_newer(sqlite_ts_str: Optional[str], file_ts_str: Optional[str]) -> bool:
    """
    Determines if record in SQLite is newer than the legacy file data.
    Conservative behavior:
    - If SQLite has valid timestamp and file has null/invalid -> True (do not overwrite SQLite).
    - If both have valid timestamps: True if sqlite_dt > file_dt.
    - If SQLite lacks valid timestamp -> False.
    """
    sql_dt = parse_iso_utc(sqlite_ts_str)
    file_dt = parse_iso_utc(file_ts_str)
    if sql_dt is None:
        return False
    if file_dt is None:
        # SQLite has a valid timestamp but legacy file has invalid/missing timestamp -> protect SQLite
        return True
    return sql_dt > file_dt


class LegacyStateImporter:
    """
    Manages controlled import of legacy JSON state files into SQLite database.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    def _ensure_imports_table(self, conn: sqlite3.Connection):
        """Ensures existence of legacy_imports table."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS legacy_imports (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
        """)

    def _get_imported_hashes(self, conn: sqlite3.Connection) -> Dict[str, str]:
        """Retrieves import_key -> file_hash map of previously imported files."""
        self._ensure_imports_table(conn)
        cur = conn.execute("SELECT file_path, file_hash FROM legacy_imports")
        return {row["file_path"]: row["file_hash"] for row in cur.fetchall()}

    def _record_import(
        self,
        conn: sqlite3.Connection,
        import_key: str,
        file_hash: str,
        entity_type: str,
        entity_id: str,
    ):
        """Records successful import of a file with canonical key."""
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO legacy_imports (file_path, file_hash, entity_type, entity_id, imported_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                file_hash = excluded.file_hash,
                entity_type = excluded.entity_type,
                entity_id = excluded.entity_id,
                imported_at = excluded.imported_at
            """,
            (import_key, file_hash, entity_type, entity_id, now),
        )

    def import_legacy_state(
        self,
        state_dir: Union[str, Path],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Imports JSON files from state_dir (and subdirectories) into SQLite.
        Returns a detailed report with statistics and any errors encountered.
        """
        base_path = Path(state_dir)
        if not base_path.exists():
            return {
                "dry_run": dry_run,
                "status": "skipped",
                "message": f"Directory di stato non trovata: {base_path}",
                "scanned_files": 0,
                "imported_sessions": 0,
                "imported_messages": 0,
                "imported_runs": 0,
                "skipped_files": 0,
                "errors": [],
                "details": [],
            }

        canonical_base_path = str(base_path.resolve())

        report = {
            "dry_run": dry_run,
            "status": "completed",
            "state_dir": str(base_path),
            "scanned_files": 0,
            "imported_sessions": 0,
            "imported_messages": 0,
            "imported_runs": 0,
            "skipped_files": 0,
            "errors": [],
            "details": [],
        }

        # 1. Raccoglie tutti i file JSON da analizzare
        json_files: List[Path] = []
        for p in base_path.rglob("*.json"):
            if p.is_file():
                json_files.append(p)

        report["scanned_files"] = len(json_files)

        # 2. Carica hash dei file già importati
        imported_hashes: Dict[str, str] = {}
        if not dry_run:
            with self.db_manager.connection() as conn:
                imported_hashes = self._get_imported_hashes(conn)

        # 3. Categorizza i file per eseguire un'elaborazione in passaggi ordinati
        brainstorm_files: List[Path] = []
        active_mapping_files: List[Path] = []
        run_files: List[Path] = []
        other_files: List[Path] = []

        for p in json_files:
            fname = p.name
            if fname.startswith("bs_") or (fname.startswith("brainstorm_") and fname != "brainstorm_active.json"):
                brainstorm_files.append(p)
            elif fname.startswith("active_") or fname == "brainstorm_active.json":
                active_mapping_files.append(p)
            elif fname.startswith("pending_") or fname.startswith("completed_") or fname.startswith("checkpoint_"):
                run_files.append(p)
            else:
                other_files.append(p)

        # Ordina internamente ciascun gruppo
        brainstorm_files.sort(key=lambda x: str(x))
        active_mapping_files.sort(key=lambda x: str(x))
        run_files.sort(key=lambda x: str(x))

        # 4. Sequenza ordinata di elaborazione
        ordered_files = brainstorm_files + active_mapping_files + run_files + other_files

        for fpath in ordered_files:
            rel_path_str = str(fpath.relative_to(base_path))
            import_key = f"{canonical_base_path}::{rel_path_str}"

            try:
                content_bytes = fpath.read_bytes()
                if not content_bytes.strip():
                    report["skipped_files"] += 1
                    report["details"].append({"file": rel_path_str, "status": "skipped_empty"})
                    continue

                file_hash = hashlib.sha256(content_bytes).hexdigest()

                # Controllo Idempotenza
                if not dry_run and imported_hashes.get(import_key) == file_hash:
                    report["skipped_files"] += 1
                    report["details"].append({"file": rel_path_str, "status": "already_imported"})
                    continue

                data = json.loads(content_bytes.decode("utf-8"))
                if not isinstance(data, dict):
                    report["skipped_files"] += 1
                    report["details"].append({"file": rel_path_str, "status": "skipped_non_dict"})
                    continue

                fname = fpath.name
                if fname.startswith("bs_") or (fname.startswith("brainstorm_") and fname != "brainstorm_active.json"):
                    self._process_brainstorm_file(fpath, rel_path_str, import_key, file_hash, data, dry_run, report)
                elif fname.startswith("active_") or fname == "brainstorm_active.json":
                    self._process_active_mapping_file(fpath, rel_path_str, import_key, file_hash, data, dry_run, report)
                elif fname.startswith("pending_"):
                    self._process_pending_run_file(fpath, rel_path_str, import_key, file_hash, data, dry_run, report)
                elif fname.startswith("completed_"):
                    self._process_completed_run_file(fpath, rel_path_str, import_key, file_hash, data, dry_run, report)
                elif fname.startswith("checkpoint_"):
                    self._process_checkpoint_run_file(fpath, rel_path_str, import_key, file_hash, data, dry_run, report)
                else:
                    report["skipped_files"] += 1
                    report["details"].append({"file": rel_path_str, "status": "ignored_unknown_pattern"})

            except Exception as e:
                logger.warning(f"Error reading/importing legacy file {rel_path_str}: {e}")
                report["errors"].append({"file": rel_path_str, "error": str(e)})

        if report["errors"]:
            report["status"] = "completed_with_errors"

        return report

    def _process_brainstorm_file(
        self,
        fpath: Path,
        rel_path: str,
        import_key: str,
        file_hash: str,
        data: Dict[str, Any],
        dry_run: bool,
        report: Dict[str, Any],
    ):
        """Maps a brainstorming file into Session + Messages."""
        session_id = str(data.get("id") or fpath.stem)
        chat_id = str(data.get("chat_id") or "")
        title = data.get("task") or data.get("title") or "Brainstorming Session"
        status = data.get("status") or "active"
        created_at = data.get("created_at") or utc_now_iso()
        updated_at = data.get("updated_at") or created_at

        # Preserve all additional fields in metadata
        reserved_keys = {"id", "chat_id", "title", "status", "created_at", "updated_at", "messages"}
        metadata = {k: v for k, v in data.items() if k not in reserved_keys}
        messages_raw = data.get("messages", [])

        if dry_run:
            report["imported_sessions"] += 1
            report["imported_messages"] += len(messages_raw)
            report["details"].append({
                "file": rel_path,
                "type": "session",
                "id": session_id,
                "messages_count": len(messages_raw),
                "status": "dry_run_imported"
            })
            return

        with self.db_manager.transaction() as conn:
            # Existence check and UTC comparison to prevent overwriting newer SQLite data
            cur = conn.execute("SELECT updated_at FROM sessions WHERE id = ?", (session_id,))
            existing = cur.fetchone()
            if existing and is_sqlite_record_newer(existing["updated_at"], updated_at):
                logger.info(f"Session {session_id} in SQLite is newer than JSON file. Skip overwrite.")
                report["skipped_files"] += 1
                report["details"].append({"file": rel_path, "status": "skipped_sqlite_newer"})
                return

            # Insert or update session
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

            # Insert associated messages with timestamp check
            msg_count = 0
            for idx, msg in enumerate(messages_raw):
                if not isinstance(msg, dict):
                    continue
                mid = str(msg.get("id") or f"{session_id}_msg_{idx}")
                sender = msg.get("sender", "")
                agent = msg.get("agent", "")
                role = "user" if sender.lower() == "user" else "assistant"
                author = agent or sender or msg.get("author") or "system"
                content = msg.get("text") or msg.get("content") or ""
                m_created = msg.get("timestamp") or msg.get("created_at") or created_at

                # If message already exists and is newer, do not overwrite
                cur_m = conn.execute("SELECT created_at FROM messages WHERE id = ?", (mid,))
                existing_m = cur_m.fetchone()
                if existing_m and is_sqlite_record_newer(existing_m["created_at"], m_created):
                    logger.info(f"Message {mid} in SQLite is newer. Skip update.")
                    continue

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
                msg_count += 1

            self._record_import(conn, import_key, file_hash, "session", session_id)

        report["imported_sessions"] += 1
        report["imported_messages"] += msg_count
        report["details"].append({
            "file": rel_path,
            "type": "session",
            "id": session_id,
            "messages_count": msg_count,
            "status": "imported",
        })

    def _process_active_mapping_file(
        self,
        fpath: Path,
        rel_path: str,
        import_key: str,
        file_hash: str,
        data: Dict[str, Any],
        dry_run: bool,
        report: Dict[str, Any],
    ):
        """Processes active_*.json or brainstorm_active.json mapping files with timestamp protection."""
        active_id = data.get("active_id")
        map_ts_str = data.get("timestamp")
        if not active_id:
            report["skipped_files"] += 1
            report["details"].append({"file": rel_path, "status": "skipped_no_active_id"})
            return

        if dry_run:
            report["details"].append({
                "file": rel_path,
                "type": "active_mapping",
                "active_id": active_id,
                "status": "dry_run_mapped",
            })
            return

        with self.db_manager.transaction() as conn:
            # 1. Verify that target session exists in SQLite database
            cur = conn.execute("SELECT id, status, updated_at FROM sessions WHERE id = ?", (active_id,))
            session_row = cur.fetchone()
            if not session_row:
                logger.warning(f"Target session {active_id} not found for active mapping {rel_path}. Skip recording import.")
                report["skipped_files"] += 1
                report["details"].append({
                    "file": rel_path,
                    "type": "active_mapping",
                    "active_id": active_id,
                    "status": "skipped_missing_session",
                })
                return

            # 2. Timestamp protection: if SQLite session is newer than legacy mapping
            if is_sqlite_record_newer(session_row["updated_at"], map_ts_str):
                logger.info(f"Session {active_id} in SQLite is newer than active mapping {rel_path}. Skip status update.")
                report["skipped_files"] += 1
                report["details"].append({
                    "file": rel_path,
                    "type": "active_mapping",
                    "active_id": active_id,
                    "status": "skipped_sqlite_newer",
                })
                # Do not record hash so it can be re-evaluated in the future
                return

            # 3. Calculate updated_at without backdating the session
            new_updated_at = session_row["updated_at"]
            map_dt = parse_iso_utc(map_ts_str)
            sess_dt = parse_iso_utc(session_row["updated_at"])
            if map_dt is not None:
                if sess_dt is None or map_dt > sess_dt:
                    new_updated_at = map_dt.isoformat()

            conn.execute(
                "UPDATE sessions SET status = 'active', updated_at = ? WHERE id = ?",
                (new_updated_at, active_id),
            )
            self._record_import(conn, import_key, file_hash, "active_mapping", active_id)

        report["details"].append({
            "file": rel_path,
            "type": "active_mapping",
            "active_id": active_id,
            "status": "mapped",
        })

    def _process_pending_run_file(
        self,
        fpath: Path,
        rel_path: str,
        import_key: str,
        file_hash: str,
        data: Dict[str, Any],
        dry_run: bool,
        report: Dict[str, Any],
    ):
        """Maps pending_<branch>.json into Run with status 'waiting_for_approval' and deterministic ID."""
        branch = data.get("branch", "")
        run_id = generate_deterministic_run_id(import_key)
        status = "waiting_for_approval"
        task = data.get("task", "")
        workspace = data.get("workspace", "")
        html_diff_path = data.get("html_diff_path")
        now = data.get("timestamp") or utc_now_iso()

        result_data = {
            "workspace": workspace,
            "html_diff_path": html_diff_path,
            "status": "WAITING_FOR_APPROVAL",
        }
        reserved_keys = {"branch", "workspace", "html_diff_path", "status", "timestamp", "task"}
        metadata = {k: v for k, v in data.items() if k not in reserved_keys}
        metadata["task"] = task
        metadata["legacy_file_name"] = fpath.name
        metadata["legacy_file_path"] = rel_path
        metadata["legacy_import_key"] = import_key

        if dry_run:
            report["imported_runs"] += 1
            report["details"].append({"file": rel_path, "type": "run", "id": run_id, "status": "dry_run_imported"})
            return

        with self.db_manager.transaction() as conn:
            # Newer SQLite protection check
            cur = conn.execute("SELECT updated_at FROM runs WHERE id = ?", (run_id,))
            existing = cur.fetchone()
            if existing and is_sqlite_record_newer(existing["updated_at"], now):
                logger.info(f"Run {run_id} in SQLite is newer than JSON file. Skip overwrite.")
                report["skipped_files"] += 1
                report["details"].append({"file": rel_path, "status": "skipped_sqlite_newer"})
                return

            conn.execute(
                """
                INSERT INTO runs (
                    id, session_id, action, status, progress, current_step,
                    preset, repo, branch, result, error, metadata,
                    created_at, updated_at, completed_at
                )
                VALUES (?, NULL, 'execute_task', ?, 90, 'waiting_approval', 'standard', NULL, ?, ?, NULL, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    result = excluded.result,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (run_id, status, branch, json.dumps(result_data), json.dumps(metadata), now, now),
            )
            self._record_import(conn, import_key, file_hash, "run", run_id)

        report["imported_runs"] += 1
        report["details"].append({"file": rel_path, "type": "run", "id": run_id, "status": "imported"})

    def _process_completed_run_file(
        self,
        fpath: Path,
        rel_path: str,
        import_key: str,
        file_hash: str,
        data: Dict[str, Any],
        dry_run: bool,
        report: Dict[str, Any],
    ):
        """Maps completed_<id>.json into Run with status 'completed' and deterministic ID."""
        run_id = generate_deterministic_run_id(import_key)
        status = "completed"
        branch = data.get("branch", "")
        repo = data.get("repo")
        preset = data.get("preset", "standard")
        created_at = data.get("created_at") or data.get("timestamp") or utc_now_iso()
        completed_at = data.get("completed_at") or data.get("timestamp") or created_at

        result_data = data.get("result") or {}
        if not result_data and "files_changed" in data:
            result_data["files_changed"] = data.get("files_changed", [])
            result_data["status"] = "COMPLETED"

        reserved_keys = {"id", "branch", "repo", "preset", "status", "created_at", "completed_at", "timestamp", "result"}
        metadata = {k: v for k, v in data.items() if k not in reserved_keys}
        metadata["legacy_file_name"] = fpath.name
        metadata["legacy_file_path"] = rel_path
        metadata["legacy_import_key"] = import_key

        if dry_run:
            report["imported_runs"] += 1
            report["details"].append({"file": rel_path, "type": "run", "id": run_id, "status": "dry_run_imported"})
            return

        with self.db_manager.transaction() as conn:
            # Newer SQLite protection check
            cur = conn.execute("SELECT updated_at FROM runs WHERE id = ?", (run_id,))
            existing = cur.fetchone()
            if existing and is_sqlite_record_newer(existing["updated_at"], completed_at):
                logger.info(f"Run {run_id} in SQLite is newer than JSON file. Skip overwrite.")
                report["skipped_files"] += 1
                report["details"].append({"file": rel_path, "status": "skipped_sqlite_newer"})
                return

            conn.execute(
                """
                INSERT INTO runs (
                    id, session_id, action, status, progress, current_step,
                    preset, repo, branch, result, error, metadata,
                    created_at, updated_at, completed_at
                )
                VALUES (?, NULL, 'execute_task', ?, 100, 'completed', ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    result = excluded.result,
                    metadata = excluded.metadata,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                (run_id, status, preset, repo, branch, json.dumps(result_data), json.dumps(metadata), created_at, completed_at, completed_at),
            )
            self._record_import(conn, import_key, file_hash, "run", run_id)

        report["imported_runs"] += 1
        report["details"].append({"file": rel_path, "type": "run", "id": run_id, "status": "imported"})

    def _process_checkpoint_run_file(
        self,
        fpath: Path,
        rel_path: str,
        import_key: str,
        file_hash: str,
        data: Dict[str, Any],
        dry_run: bool,
        report: Dict[str, Any],
    ):
        """Maps checkpoint_<branch>.json into Run with orchestrator_core schema and deterministic ID."""
        branch = data.get("branch", "")
        run_id = generate_deterministic_run_id(import_key)
        status = "running"
        now = data.get("timestamp") or utc_now_iso()

        result_data = {
            "phase": data.get("phase") or data.get("last_phase"),
            "completed_task_ids": data.get("completed_task_ids", []),
            "completed_tasks_history": data.get("completed_tasks_history", {}),
            "tokens_used": data.get("tokens_used"),
            "data": data.get("data"),
        }
        reserved_keys = {
            "branch", "timestamp", "phase", "last_phase",
            "completed_task_ids", "completed_tasks_history",
            "tokens_used", "data", "status"
        }
        metadata = {k: v for k, v in data.items() if k not in reserved_keys}
        metadata["legacy_file_name"] = fpath.name
        metadata["legacy_file_path"] = rel_path
        metadata["legacy_import_key"] = import_key

        if dry_run:
            report["imported_runs"] += 1
            report["details"].append({"file": rel_path, "type": "run", "id": run_id, "status": "dry_run_imported"})
            return

        with self.db_manager.transaction() as conn:
            # Newer SQLite protection check
            cur = conn.execute("SELECT updated_at FROM runs WHERE id = ?", (run_id,))
            existing = cur.fetchone()
            if existing and is_sqlite_record_newer(existing["updated_at"], now):
                logger.info(f"Checkpoint run {run_id} in SQLite is newer than JSON file. Skip overwrite.")
                report["skipped_files"] += 1
                report["details"].append({"file": rel_path, "status": "skipped_sqlite_newer"})
                return

            conn.execute(
                """
                INSERT INTO runs (
                    id, session_id, action, status, progress, current_step,
                    preset, repo, branch, result, error, metadata,
                    created_at, updated_at, completed_at
                )
                VALUES (?, NULL, 'execute_task', ?, 50, 'checkpoint', 'standard', NULL, ?, ?, NULL, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    result = excluded.result,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (run_id, status, branch, json.dumps(result_data), json.dumps(metadata), now, now),
            )
            self._record_import(conn, import_key, file_hash, "run", run_id)

        report["imported_runs"] += 1
        report["details"].append({"file": rel_path, "type": "run", "id": run_id, "status": "imported"})


def import_legacy_state(
    state_dir: Union[str, Path],
    db_manager: Optional[DatabaseManager] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    High-level convenience function to import legacy JSON state into SQLite.
    """
    manager = db_manager or DatabaseManager()
    importer = LegacyStateImporter(manager)
    return importer.import_legacy_state(state_dir, dry_run=dry_run)
