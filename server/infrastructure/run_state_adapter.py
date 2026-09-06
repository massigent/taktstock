#!/usr/bin/env python3
"""
Run State Adapter for MultiAgentRunner Shadow Persistence
---------------------------------------------------------
Replica in SQLite il ciclo di vita delle run di orchestrazione (running, progress,
waiting_for_approval, completed, failed) in modalità shadow (osservabilità opzionale).

Flag supportato:
- TAKTSTOCK_SQLITE_RUN_SHADOW_WRITE (default 0, fallback UFFICIO_SQLITE_RUN_SHADOW_WRITE)

Principi:
1. Con flag disattivato, non apre né crea il database SQLite.
2. I file JSON (checkpoint_*.json, pending_*.json, runs_history.jsonl) restano la fonte autorevole.
3. Ogni metodo SQLite cattura e logga i propri errori senza mai sollevare eccezioni verso il runner.
4. Ogni istanza di runner riceve un run_id UUID4 univoco e stabile.
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
    Adapter per la persistenza shadow del ciclo di vita delle run orchestrate.
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
        """Registra l'avvio della run in stato running con progress 0."""
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
                logger.debug(f"[RUN_SHADOW] Run {run_id} registrata in stato running.")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Errore start_run per {run_id}: {e}")

    def update_progress(
        self,
        run_id: str,
        progress: int,
        current_step: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Aggiorna la percentuale di avanzamento e lo step corrente."""
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
                logger.debug(f"[RUN_SHADOW] Run {run_id} avanzamento {progress}% ({current_step}).")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Errore update_progress per {run_id}: {e}")

    def waiting_approval_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Aggiorna lo stato a waiting_for_approval con i dati di diff review e workspace."""
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
                logger.debug(f"[RUN_SHADOW] Run {run_id} impostata su waiting_for_approval.")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Errore waiting_approval_run per {run_id}: {e}")

    def complete_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Registra il completamento con successo della run."""
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
                logger.debug(f"[RUN_SHADOW] Run {run_id} completata con successo.")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Errore complete_run per {run_id}: {e}")

    def fail_run(
        self,
        run_id: str,
        error: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Registra il fallimento della run con errore dettagliato."""
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
                logger.debug(f"[RUN_SHADOW] Run {run_id} registrata come fallita: {error}")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Errore fail_run per {run_id}: {e}")

    def block_prerequisite_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Registra il blocco della run per prerequisito mancante."""
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
                logger.debug(f"[RUN_SHADOW] Run {run_id} registrata come blocked_prerequisite: {error}")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Errore block_prerequisite_run per {run_id}: {e}")

    def block_budget_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Registra il blocco della run per superamento del budget operativo/token/chiamate."""
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
                logger.debug(f"[RUN_SHADOW] Run {run_id} registrata come blocked_budget: {error}")
        except Exception as e:
            logger.warning(f"[RUN_SHADOW_ERROR] Errore block_budget_run per {run_id}: {e}")

