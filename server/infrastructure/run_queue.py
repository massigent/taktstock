#!/usr/bin/env python3
"""
Async Run Queue and Worker for Taktstock MultiAgent Runs
--------------------------------------------------------
Provides an in-memory asynchronous queue (queue.Queue + threading) backed by SQLite for
queuing and sequential execution of orchestrator runs, behind the feature flag
TAKTSTOCK_ASYNC_RUNS (with fallback UFFICIO_ASYNC_RUNS).

Principles:
1. Standard library only: queue.Queue, threading, subprocess.
2. Commands executed strictly with shell=False and fixed argument lists.
3. Default timeout: 1800 seconds.
4. Results capped at returncode and maximum 32 KiB of output; never secrets or complete environments.
5. Bootstrap recovery: marks orphaned queued/running runs as failed (interrupted_by_server_restart) without re-executing.
6. Graceful shutdown via stop() and join().
"""

import os
import sys
import json
import uuid
import queue
import logging
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from .database import DatabaseManager, utc_now_iso
from .run_repository import RunRepository
from .brainstorm_state_adapter import _is_flag_enabled

logger = logging.getLogger("TaktstockRunQueue")

MAX_OUTPUT_BYTES = 32 * 1024  # 32 KiB
DEFAULT_TIMEOUT_SECONDS = 1800


def extract_terminal_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Returns the last complete JSON object emitted by the process, if present."""
    if not stdout:
        return None

    decoder = json.JSONDecoder()
    for start in range(len(stdout) - 1, -1, -1):
        if stdout[start] != "{":
            continue
        try:
            value, end = decoder.raw_decode(stdout[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict) and not stdout[start + end:].strip():
            return value
    return None


def is_async_runs_enabled(explicit_val: Optional[bool] = None, config_path: Optional[Any] = None) -> bool:
    """Checks whether asynchronous run queue is enabled."""
    return _is_flag_enabled("TAKTSTOCK_ASYNC_RUNS", explicit_val, config_path=config_path, fallback_env="UFFICIO_ASYNC_RUNS")


class QueueWorker:
    """
    Dedicated worker thread for asynchronous run execution.
    Features:
    - FIFO processing with in-memory Queue
    - Non-blocking crash recovery (stale runs marked failed at bootstrap)
    - Subprocess execution with explicit timeouts and process kill
    - Direct updates to SQLite via RunRepository
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        auto_start: bool = False,
    ):
        self.db_manager = db_manager or DatabaseManager()
        self.run_repo = RunRepository(self.db_manager)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

        self._queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._current_proc: Optional[subprocess.Popen[Any]] = None
        self._proc_lock = threading.Lock()

        if auto_start:
            self.start()

    def recover_stale_runs(self) -> int:
        """
        At bootstrap, marks any runs remaining in 'queued' or 'running' status as failed.
        Returns the count of stale runs recovered/marked as failed.
        """
        try:
            with self.db_manager.transaction() as conn:
                now = utc_now_iso()
                cursor = conn.execute(
                    """
                    UPDATE runs
                    SET status = 'failed',
                        current_step = 'failed',
                        error = 'interrupted_by_server_restart',
                        completed_at = ?,
                        updated_at = ?
                    WHERE status IN ('queued', 'running')
                    """,
                    (now, now),
                )
                count = cursor.rowcount
                if count > 0:
                    logger.warning(f"[RECOVERY] Marked {count} interrupted runs as failed (interrupted_by_server_restart).")
                return count
        except Exception as e:
            logger.error(f"[RECOVERY_ERROR] Error recovering stale runs: {e}")
            return 0

    def start(self) -> None:
        """Starts the worker thread and recovers pending runs."""
        if self._thread is not None and self._thread.is_alive():
            return

        self.recover_stale_runs()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="TaktstockRunQueueWorker")
        self._thread.start()
        logger.info("Taktstock QueueWorker started in background.")

    def stop(self, timeout: float = 5.0) -> None:
        """Stops the worker cleanly, terminating any pending processes."""
        self._stop_event.set()
        self._queue.put(None)

        with self._proc_lock:
            if self._current_proc is not None and self._current_proc.poll() is None:
                try:
                    self._current_proc.terminate()
                except Exception:
                    pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("Taktstock QueueWorker stopped.")

    def enqueue(
        self,
        cmd_args: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        action: str = "execute_task",
        preset: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Registers run in 'queued' status in SQLite and enqueues it for execution.
        Returns the UUID4 run_id.
        """
        rid = run_id or str(uuid.uuid4())
        meta = dict(metadata or {})
        meta["source"] = "async_queue"

        self.run_repo.create_run(
            action=action,
            preset=preset,
            repo=repo,
            branch=branch,
            status="queued",
            run_id=rid,
            metadata=meta,
        )

        item = {
            "run_id": rid,
            "cmd_args": cmd_args,
            "cwd": str(cwd) if cwd else None,
            "env": env,
        }
        self._queue.put(item)
        logger.info(f"[ASYNC_QUEUE] Run {rid} accodata con successo.")
        return rid

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                self._queue.task_done()
                break

            try:
                self._execute_run(item)
            except Exception as e:
                logger.error(f"[QUEUE_WORKER_ERROR] Unexpected run execution error: {e}")
            finally:
                self._queue.task_done()

    def _execute_run(self, item: Dict[str, Any]) -> None:
        run_id = item["run_id"]
        cmd_args = item["cmd_args"]
        cwd = item.get("cwd")
        env = item.get("env")

        # 1. Update status to running
        self.run_repo.update_run_status(
            run_id=run_id,
            status="running",
            progress=10,
            current_step="started",
        )

        # 2. Controlled subprocess execution
        try:
            with self._proc_lock:
                proc = subprocess.Popen(
                    cmd_args,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=cwd,
                    env=env,
                )
                self._current_proc = proc

            try:
                stdout_str, _ = proc.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_str, _ = proc.communicate()
                limited_out = (stdout_str or "")[: self.max_output_bytes]
                self.run_repo.update_run_status(
                    run_id=run_id,
                    status="failed",
                    current_step="failed",
                    error=f"Execution timed out after {self.timeout_seconds}s",
                    result={"returncode": -1, "output": limited_out},
                )
                return
            finally:
                with self._proc_lock:
                    self._current_proc = None

            limited_out = (stdout_str or "")[: self.max_output_bytes]
            ret = proc.returncode
            if ret == 0:
                result = {"returncode": ret, "output": limited_out}
                structured_result = extract_terminal_json(stdout_str or "")
                if structured_result is not None:
                    result["summary"] = structured_result
                
                final_status = "completed"
                if structured_result and isinstance(structured_result, dict):
                    st = str(structured_result.get("status", "")).upper()
                    if st == "BLOCKED_PREREQUISITE":
                        final_status = "blocked_prerequisite"
                    elif st == "WAITING_FOR_APPROVAL":
                        final_status = "waiting_for_approval"

                self.run_repo.update_run_status(
                    run_id=run_id,
                    status=final_status,
                    progress=100,
                    current_step=final_status,
                    result=result,
                )
            else:
                self.run_repo.update_run_status(
                    run_id=run_id,
                    status="failed",
                    current_step="failed",
                    error=f"Process exited with code {ret}",
                    result={"returncode": ret, "output": limited_out},
                )
        except Exception as e:
            logger.error(f"[QUEUE_WORKER_ERROR] Subprocess execution error for {run_id}: {e}")
            try:
                self.run_repo.update_run_status(
                    run_id=run_id,
                    status="failed",
                    current_step="failed",
                    error=str(e),
                )
            except Exception:
                pass


_global_queue_worker: Optional[QueueWorker] = None
_global_lock = threading.Lock()


def get_global_queue_worker(db_manager: Optional[DatabaseManager] = None) -> QueueWorker:
    """Returns the global QueueWorker singleton."""
    global _global_queue_worker
    with _global_lock:
        if _global_queue_worker is None:
            _global_queue_worker = QueueWorker(db_manager=db_manager, auto_start=True)
        return _global_queue_worker


def stop_global_queue_worker(timeout: float = 5.0) -> None:
    """Stops the global QueueWorker singleton if active."""
    global _global_queue_worker
    with _global_lock:
        if _global_queue_worker is not None:
            _global_queue_worker.stop(timeout=timeout)
            _global_queue_worker = None
