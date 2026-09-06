#!/usr/bin/env python3
"""
Database Management & Migration Engine for Taktstock
----------------------------------------------------
Thread-safe SQLite connection management with:
- PRAGMA journal_mode=WAL
- PRAGMA foreign_keys=ON
- PRAGMA busy_timeout=10000
- Explicit transactions with automatic rollback
- Versioned schema_migrations system
"""

import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, List, Optional, Tuple, Union

logger = logging.getLogger("TaktstockDatabase")


def utc_now_iso() -> str:
    """Returns the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class DatabaseError(Exception):
    """Base exception for database layer errors."""
    pass


class MigrationError(DatabaseError):
    """Exception raised when a schema migration fails."""
    pass


class DatabaseManager:
    """
    SQLite database manager with WAL support, foreign keys, and migrations.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None, auto_migrate: bool = True):
        if db_path is None:
            state_dir = Path(__file__).resolve().parent.parent.parent / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            if (state_dir / "taktstock.db").exists():
                self.db_path = state_dir / "taktstock.db"
            elif (state_dir / "ufficio.db").exists():
                self.db_path = state_dir / "ufficio.db"
            else:
                self.db_path = state_dir / "taktstock.db"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if auto_migrate:
            self.run_migrations()

    def get_connection(self) -> sqlite3.Connection:
        """
        Creates and configures a new SQLite connection with WAL, foreign keys, and configured timeout.
        """
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=10.0,
                isolation_level=None  # Explicit transaction management
            )
            conn.row_factory = sqlite3.Row
            # Essential PRAGMA configuration
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            return conn
        except sqlite3.Error as e:
            logger.error(f"Error opening database connection {self.db_path}: {e}")
            raise DatabaseError(f"Cannot open SQLite connection: {e}") from e

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for simple read connection or single query.
        """
        conn = self.get_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for atomic transactions with 'BEGIN IMMEDIATE' lock.
        Performs automatic rollback on error.
        """
        conn = self.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rb_err:
                logger.warning(f"Error during rollback: {rb_err}")
            logger.error(f"Transaction failed on {self.db_path}: {e}")
            raise
        finally:
            conn.close()

    def _init_migrations_table(self, conn: sqlite3.Connection) -> None:
        """Initializes the schema_migrations table if not present."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
        """)

    def get_applied_migrations(self) -> List[int]:
        """Returns the list of already applied migration versions."""
        with self.connection() as conn:
            self._init_migrations_table(conn)
            cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
            return [row["version"] for row in cursor.fetchall()]

    def run_migrations(self) -> None:
        """
        Runs all pending schema migrations in progressive order.
        """
        migrations: List[Tuple[int, str, List[str]]] = [
            (
                1,
                "create_initial_schema",
                [
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        chat_id TEXT NOT NULL,
                        title TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_sessions_chat_id ON sessions(chat_id);",
                    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);",
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        role TEXT NOT NULL,
                        author TEXT,
                        content TEXT NOT NULL,
                        metadata TEXT,
                        created_at TEXT NOT NULL
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, created_at ASC);",
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY,
                        session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'queued',
                        progress INTEGER NOT NULL DEFAULT 0,
                        current_step TEXT,
                        preset TEXT,
                        repo TEXT,
                        branch TEXT,
                        result TEXT,
                        error TEXT,
                        metadata TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_runs_session_id ON runs(session_id);",
                    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);",
                    "CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);"
                ]
            ),
            (
                2,
                "create_legacy_imports_table",
                [
                    """
                    CREATE TABLE IF NOT EXISTS legacy_imports (
                        file_path TEXT PRIMARY KEY,
                        file_hash TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        imported_at TEXT NOT NULL
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_legacy_imports_entity ON legacy_imports(entity_type, entity_id);"
                ]
            ),
            (
                3,
                "create_agent_usage_events_table",
                [
                    """
                    CREATE TABLE IF NOT EXISTS agent_usage_events (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        run_id TEXT,
                        session_id TEXT,
                        agent_role TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        status TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        reported_tokens INTEGER,
                        prompt_length INTEGER NOT NULL,
                        files_count INTEGER,
                        escalation_reason TEXT,
                        error_category TEXT,
                        metadata_json TEXT
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_agent_usage_created_at ON agent_usage_events(created_at DESC);",
                    "CREATE INDEX IF NOT EXISTS idx_agent_usage_agent_role ON agent_usage_events(agent_role);",
                    "CREATE INDEX IF NOT EXISTS idx_agent_usage_model ON agent_usage_events(model);",
                    "CREATE INDEX IF NOT EXISTS idx_agent_usage_status ON agent_usage_events(status);"
                ]
            ),
            (
                4,
                "add_telemetry_fields_and_budget_tracking",
                [
                    "ALTER TABLE agent_usage_events ADD COLUMN subtask_id TEXT;",
                    "ALTER TABLE agent_usage_events ADD COLUMN estimated_tokens INTEGER;",
                    "ALTER TABLE agent_usage_events ADD COLUMN attempts_count INTEGER DEFAULT 1;",
                    "ALTER TABLE agent_usage_events ADD COLUMN fallback_used TEXT;",
                    "ALTER TABLE agent_usage_events ADD COLUMN prompt_sha256 TEXT;",
                    "CREATE INDEX IF NOT EXISTS idx_agent_usage_run_id ON agent_usage_events(run_id);",
                    "CREATE INDEX IF NOT EXISTS idx_agent_usage_phase ON agent_usage_events(phase);"
                ]
            ),
            (
                5,
                "add_granular_token_breakdown_and_call_reason",
                [
                    "ALTER TABLE agent_usage_events ADD COLUMN input_tokens INTEGER;",
                    "ALTER TABLE agent_usage_events ADD COLUMN output_tokens INTEGER;",
                    "ALTER TABLE agent_usage_events ADD COLUMN thinking_tokens INTEGER;",
                    "ALTER TABLE agent_usage_events ADD COLUMN cache_read_tokens INTEGER;",
                    "ALTER TABLE agent_usage_events ADD COLUMN call_reason TEXT;"
                ]
            )
        ]

        with self.transaction() as conn:
            self._init_migrations_table(conn)
            cursor = conn.execute("SELECT version FROM schema_migrations")
            applied = {row["version"] for row in cursor.fetchall()}

            for version, name, statements in migrations:
                if version not in applied:
                    logger.info(f"Applying migration #{version}: {name}...")
                    try:
                        for stmt in statements:
                            conn.execute(stmt.strip())
                        conn.execute(
                            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                            (version, name, utc_now_iso())
                        )
                    except sqlite3.Error as e:
                        logger.error(f"Error applying migration #{version} ({name}): {e}")
                        raise MigrationError(f"Migration failed #{version} ({name}): {e}") from e
