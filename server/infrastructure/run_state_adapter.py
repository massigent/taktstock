#!/usr/bin/env python3
"""
Run State Adapter for MultiAgentRunner Shadow Persistence
---------------------------------------------------------
Replicates orchestration run lifecycles (running, progress,
waiting_for_approval, completed, failed) into SQLite in shadow mode (optional observability).

Supported flag:
- TAKTSTOCK_SQLITE_RUN_SHADOW_WRITE (default 0, fallback UFFICIO_SQLITE_RUN_SHADOW_WRITE)

Principles:
1. When flag is disabled, neither opens nor creates the SQLite database.
2. JSON files (checkpoint_*.json, pending_*.json, runs_history.jsonl) remain the single source of truth.
3. Each SQLite method catches and logs its own errors without ever raising exceptions to the runner.
4. Each runner instance receives a unique and stable UUID4 run_id.
"""

import os
import json
import logging
from typing import Any, Dict, Optional

from .database import DatabaseManager
from .run_repository import RunRepository

from .brainstorm_state_adapter import _is_flag_enabled

logger = logging.getLogger("TaktstockRunShadow")


class RunStateAdapter:
    """
    Adapter for shadow persistence of orchestrated run lifecycles.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        shadow_write: Optional[bool] = None,
        config_path: Optional[Any] = None,
    ):
        self.shadow_write = _is_flag_enabled("TAKTSTOCK_SQLITE_RUN_SHADOW_WRITE", shadow_write, config_path=config_path, fallback_env="UFFICIO_SQLITE_RUN_SHADOW_WRITE")
        self._db_manager = db_manager
        self._run_repo: Optional[RunRepository] = None

        if self._db_manager is None and self.shadow_write:
            self._db_manager = DatabaseManager()

    @property
    def db_manager(self) -> Optional[DatabaseManager]:
        return self._db_manager

    def get_run_repo(self) -> Optional[RunRepository]:
        if self._run_repo is None and self._db_manager is not None:
            self._run_repo = RunRepository(self._db_manager)
        return self._run_repo

    def start_run(
        self,
        run_id: str,
        action: str = "execute_task",
        session_id: Optional[str] = None,
        preset: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records run startup in running state with progress 0."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            repo_m = self.get_run_repo()
            if repo_m:
                meta = dict(metadata or {})
                meta["source"] = "shadow"
                repo_m.create_run(
                    action=action,
                    session_id=session_id,
                    preset=preset,
                    repo=repo,
                    branch=branch,
                    status="running",
                    run_id=run_id,
                    metadata=meta,
                )
                logger.debug(f"[RUN_SHADOW] Run {run_id} recorded in running state.")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Error in start_run for {run_id}: {e}")

    def update_progress(
        self,
        run_id: str,
        progress: int,
        current_step: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Updates progress percentage and current step."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            repo_m = self.get_run_repo()
            if repo_m:
                repo_m.update_run_status(
                    run_id=run_id,
                    status="running",
                    progress=progress,
                    current_step=current_step,
                    metadata=metadata,
                )
                logger.debug(f"[RUN_SHADOW] Run {run_id} progress {progress}% ({current_step}).")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Error in update_progress for {run_id}: {e}")

    def waiting_approval_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Updates status to waiting_for_approval with diff review and workspace data."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            repo_m = self.get_run_repo()
            if repo_m:
                repo_m.update_run_status(
                    run_id=run_id,
                    status="waiting_for_approval",
                    progress=90,
                    current_step="waiting_approval",
                    result=result,
                    metadata=metadata,
                )
                logger.debug(f"[RUN_SHADOW] Run {run_id} set to waiting_for_approval.")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Error in waiting_approval_run for {run_id}: {e}")

    def complete_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records successful completion of run."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            repo_m = self.get_run_repo()
            if repo_m:
                repo_m.update_run_status(
                    run_id=run_id,
                    status="completed",
                    progress=100,
                    current_step="completed",
                    result=result,
                    metadata=metadata,
                )
                logger.debug(f"[RUN_SHADOW] Run {run_id} completed successfully.")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Error in complete_run for {run_id}: {e}")

    def fail_run(
        self,
        run_id: str,
        error: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records run failure with detailed error."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            repo_m = self.get_run_repo()
            if repo_m:
                repo_m.update_run_status(
                    run_id=run_id,
                    status="failed",
                    current_step="failed",
                    error=error,
                    metadata=metadata,
                )
                logger.debug(f"[RUN_SHADOW] Run {run_id} recorded as failed: {error}")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Error in fail_run for {run_id}: {e}")

    def block_prerequisite_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records run blocking due to missing prerequisite."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            repo_m = self.get_run_repo()
            if repo_m:
                repo_m.update_run_status(
                    run_id=run_id,
                    status="blocked_prerequisite",
                    progress=100,
                    current_step="blocked_prerequisite",
                    result=result,
                    error=error or result.get("blocker_reason"),
                    metadata=metadata,
                )
                logger.debug(f"[RUN_SHADOW] Run {run_id} recorded as blocked_prerequisite: {error}")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Error in block_prerequisite_run for {run_id}: {e}")

    def block_budget_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records run blocking due to operational/token/call budget exhaustion."""
        if not self.shadow_write or not self._db_manager:
            return

        try:
            repo_m = self.get_run_repo()
            if repo_m:
                repo_m.update_run_status(
                    run_id=run_id,
                    status="blocked_budget",
                    progress=100,
                    current_step="blocked_budget",
                    result=result,
                    error=error or result.get("blocker_reason"),
                    metadata=metadata,
                )
                logger.debug(f"[RUN_SHADOW] Run {run_id} recorded as blocked_budget: {error}")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Error in block_budget_run for {run_id}: {e}")

