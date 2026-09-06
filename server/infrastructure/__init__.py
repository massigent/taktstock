"""
Infrastructure Layer for Taktstock
----------------------------------
Contiene la persistenza SQLite, i repository per sessioni e run,
e i moduli di gestione dello stato per l'ecosistema Taktstock.
"""

from .database import DatabaseManager
from .session_repository import SessionRepository
from .run_repository import RunRepository
from .legacy_state_importer import LegacyStateImporter, import_legacy_state
from .brainstorm_state_adapter import BrainstormStateAdapter
from .run_state_adapter import RunStateAdapter
from .run_queue import (
    QueueWorker,
    is_async_runs_enabled,
    get_global_queue_worker,
    stop_global_queue_worker,
)

from .telemetry_repository import TelemetryRepository, extract_reported_tokens
from .agent_gateway import AgentGateway

__all__ = [
    "DatabaseManager",
    "SessionRepository",
    "RunRepository",
    "LegacyStateImporter",
    "import_legacy_state",
    "BrainstormStateAdapter",
    "RunStateAdapter",
    "QueueWorker",
    "is_async_runs_enabled",
    "get_global_queue_worker",
    "stop_global_queue_worker",
    "TelemetryRepository",
    "extract_reported_tokens",
    "AgentGateway",
]
