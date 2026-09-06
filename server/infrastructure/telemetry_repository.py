#!/usr/bin/env python3
"""
Agent Usage Telemetry Repository (Phase 1 Observability)
--------------------------------------------------------
Registrazione append-only, non bloccante e privacy-safe di ogni invocazione agente.
Nessun salvataggio di prompt, risposte, chiavi, auth o percorsi assoluti.
"""

import os
import re
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Union

try:
    from infrastructure.database import DatabaseManager, utc_now_iso
except ImportError:
    from server.infrastructure.database import DatabaseManager, utc_now_iso

logger = logging.getLogger("TelemetryRepository")

# Regex per estrazione token riportati in modo affidabile da output CLI Codex
CODEX_TOKENS_REGEX = re.compile(r"tokens used\s+(\d+)", re.IGNORECASE)

SAFE_METADATA_KEYS = {
    "preset", "reasoning_effort", "sandbox", "files_count", "is_sidecar",
    "exit_code", "retry_count", "channel", "action", "subtask_id", "call_reason",
    "context_message_count", "summary_length_chars",
    "context_payload_chars", "truncated_message_count", "output_length",
    "input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens"
}



def sanitize_telemetry_metadata(raw_meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """Filtra e serializza i soli metadati sicuri, escludendo prompt, risposte e path."""
    if not isinstance(raw_meta, dict):
        return None
    safe_dict = {}
    for k, v in raw_meta.items():
        if k in SAFE_METADATA_KEYS and v is not None:
            if isinstance(v, (int, float, bool, str)):
                safe_dict[k] = v
            elif isinstance(v, (list, tuple)):
                safe_dict[k] = list(v)
    return json.dumps(safe_dict, separators=(",", ":")) if safe_dict else None


def extract_token_breakdown(raw_output: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[int]]:
    """
    Estrae i token in modo affidabile distinguendo:
    - total_tokens
    - input_tokens (prompt)
    - output_tokens (completion)
    - thinking_tokens (reasoning)
    - cache_read_tokens (prompt cached)
    Non somma mai i token di cache_read ai token fatturati senza etichetta esplicita.
    """
    breakdown: Dict[str, Optional[int]] = {
        "total_tokens": None,
        "input_tokens": None,
        "output_tokens": None,
        "thinking_tokens": None,
        "cache_read_tokens": None,
    }

    if isinstance(meta, dict):
        usage = meta.get("usage")
        if isinstance(usage, dict):
            if "total_tokens" in usage:
                try:
                    breakdown["total_tokens"] = int(usage["total_tokens"])
                except Exception:
                    pass
            elif "total" in usage:
                try:
                    breakdown["total_tokens"] = int(usage["total"])
                except Exception:
                    pass

            for in_k in ["prompt_tokens", "input_tokens", "input"]:
                if in_k in usage:
                    try:
                        breakdown["input_tokens"] = int(usage[in_k])
                        break
                    except Exception:
                        pass

            for out_k in ["completion_tokens", "output_tokens", "output"]:
                if out_k in usage:
                    try:
                        breakdown["output_tokens"] = int(usage[out_k])
                        break
                    except Exception:
                        pass

            for th_k in ["reasoning_tokens", "thinking_tokens", "thinking"]:
                if th_k in usage:
                    try:
                        breakdown["thinking_tokens"] = int(usage[th_k])
                        break
                    except Exception:
                        pass
            if breakdown["thinking_tokens"] is None and isinstance(usage.get("completion_tokens_details"), dict):
                try:
                    breakdown["thinking_tokens"] = int(usage["completion_tokens_details"].get("reasoning_tokens"))
                except Exception:
                    pass

            for c_k in ["cached_tokens", "cache_read_tokens", "cache_read_input_tokens"]:
                if c_k in usage:
                    try:
                        breakdown["cache_read_tokens"] = int(usage[c_k])
                        break
                    except Exception:
                        pass
            if breakdown["cache_read_tokens"] is None and isinstance(usage.get("prompt_tokens_details"), dict):
                try:
                    breakdown["cache_read_tokens"] = int(usage["prompt_tokens_details"].get("cached_tokens"))
                except Exception:
                    pass

        if "reported_tokens" in meta and meta["reported_tokens"] is not None and breakdown["total_tokens"] is None:
            try:
                breakdown["total_tokens"] = int(meta["reported_tokens"])
            except Exception:
                pass

    if isinstance(raw_output, str) and raw_output:
        trimmed = raw_output.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                data = json.loads(trimmed)
                if isinstance(data, dict):
                    usage = data.get("usage")
                    if isinstance(usage, dict):
                        if breakdown["total_tokens"] is None and "total_tokens" in usage:
                            breakdown["total_tokens"] = int(usage["total_tokens"])
                        if breakdown["input_tokens"] is None and ("prompt_tokens" in usage or "input_tokens" in usage):
                            breakdown["input_tokens"] = int(usage.get("prompt_tokens") or usage.get("input_tokens"))
                        if breakdown["output_tokens"] is None and ("completion_tokens" in usage or "output_tokens" in usage):
                            breakdown["output_tokens"] = int(usage.get("completion_tokens") or usage.get("output_tokens"))
                        if breakdown["thinking_tokens"] is None and ("reasoning_tokens" in usage or "thinking_tokens" in usage):
                            breakdown["thinking_tokens"] = int(usage.get("reasoning_tokens") or usage.get("thinking_tokens"))
                        if breakdown["cache_read_tokens"] is None and ("cached_tokens" in usage or "cache_read_tokens" in usage):
                            breakdown["cache_read_tokens"] = int(usage.get("cached_tokens") or usage.get("cache_read_tokens"))
                    if breakdown["total_tokens"] is None and "reported_tokens" in data and data["reported_tokens"] is not None:
                        breakdown["total_tokens"] = int(data["reported_tokens"])
            except Exception:
                pass

        if breakdown["total_tokens"] is None:
            match = CODEX_TOKENS_REGEX.search(raw_output)
            if match:
                try:
                    breakdown["total_tokens"] = int(match.group(1))
                except (ValueError, TypeError):
                    pass

    return breakdown


def extract_reported_tokens(raw_output: str, meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Estrae i token totali riportati in modo affidabile se presenti nel testo o metadati."""
    bd = extract_token_breakdown(raw_output, meta)
    return bd.get("total_tokens")



class TelemetryRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db_manager = db_manager or DatabaseManager()

    def record_event(
        self,
        agent_role: str,
        provider: str,
        model: str,
        phase: str = "chat",
        status: str = "SUCCESS",
        duration_ms: int = 0,
        prompt_length: int = 0,
        reported_tokens: Optional[int] = None,
        estimated_tokens: Optional[int] = None,
        run_id: Optional[str] = None,
        subtask_id: Optional[str] = None,
        session_id: Optional[str] = None,
        files_count: Optional[int] = None,
        escalation_reason: Optional[str] = None,
        error_category: Optional[str] = None,
        attempts_count: int = 1,
        fallback_used: Optional[str] = None,
        prompt_sha256: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        thinking_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        call_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Registra un evento di utilizzo agente. Completamente NON BLOCCANTE.
        Non solleva mai eccezioni verso il chiamante.
        """
        event_id = uuid.uuid4().hex
        created_at = utc_now_iso()

        # Calcolo stima se non fornita
        if estimated_tokens is None:
            out_len = int(metadata.get("output_length", 0)) if isinstance(metadata, dict) else 0
            estimated_tokens = reported_tokens if reported_tokens is not None else max(1, (prompt_length + out_len) // 4)

        safe_metadata_json = sanitize_telemetry_metadata(metadata)

        try:
            with self.db_manager.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_usage_events (
                        id, created_at, run_id, session_id, agent_role, provider, model, phase, status,
                        duration_ms, reported_tokens, prompt_length, files_count, escalation_reason,
                        error_category, metadata_json, subtask_id, estimated_tokens, attempts_count,
                        fallback_used, prompt_sha256, input_tokens, output_tokens, thinking_tokens,
                        cache_read_tokens, call_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        created_at,
                        run_id,
                        session_id,
                        str(agent_role).strip().lower(),
                        str(provider).strip().lower(),
                        str(model).strip(),
                        str(phase).strip().lower(),
                        str(status).strip().upper(),
                        max(0, int(duration_ms)),
                        int(reported_tokens) if reported_tokens is not None else None,
                        max(0, int(prompt_length)),
                        int(files_count) if files_count is not None else None,
                        str(escalation_reason).strip() if escalation_reason else None,
                        str(error_category).strip() if error_category else None,
                        safe_metadata_json,
                        str(subtask_id).strip() if subtask_id else None,
                        int(estimated_tokens) if estimated_tokens is not None else None,
                        max(1, int(attempts_count)),
                        str(fallback_used).strip() if fallback_used else None,
                        str(prompt_sha256).strip() if prompt_sha256 else None,
                        int(input_tokens) if input_tokens is not None else None,
                        int(output_tokens) if output_tokens is not None else None,
                        int(thinking_tokens) if thinking_tokens is not None else None,
                        int(cache_read_tokens) if cache_read_tokens is not None else None,
                        str(call_reason).strip() if call_reason else None
                    )
                )
            return event_id
        except Exception as e:
            logger.warning(f"Errore non bloccante registrazione telemetria: {e}")
            return None

    def get_usage_summary(self, days: int = 7) -> Dict[str, Any]:
        """Restituisce l'aggregazione di utilizzo per gli ultimi N giorni."""
        since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            with self.db_manager.connection() as conn:
                # 1. Aggregazione per agente e modello
                cursor = conn.execute(
                    """
                    SELECT
                        agent_role,
                        model,
                        provider,
                        COUNT(*) as total_calls,
                        SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as success_calls,
                        SUM(CASE WHEN status != 'SUCCESS' THEN 1 ELSE 0 END) as failed_calls,
                        ROUND(AVG(duration_ms), 1) as avg_duration_ms,
                        SUM(CASE WHEN reported_tokens IS NOT NULL THEN reported_tokens ELSE 0 END) as total_reported_tokens
                    FROM agent_usage_events
                    WHERE created_at >= ?
                    GROUP BY agent_role, model, provider
                    ORDER BY total_calls DESC;
                    """,
                    (since_iso,)
                )
                summary_rows = [dict(row) for row in cursor.fetchall()]

                # 2. Aggregazione per escalation
                esc_cursor = conn.execute(
                    """
                    SELECT
                        escalation_reason,
                        COUNT(*) as count
                    FROM agent_usage_events
                    WHERE created_at >= ? AND escalation_reason IS NOT NULL AND escalation_reason != ''
                    GROUP BY escalation_reason
                    ORDER BY count DESC;
                    """,
                    (since_iso,)
                )
                escalation_rows = [dict(row) for row in esc_cursor.fetchall()]

                # 3. Totali complessivi
                total_calls = sum(r["total_calls"] for r in summary_rows)
                total_tokens = sum(r["total_reported_tokens"] for r in summary_rows)

                return {
                    "days": days,
                    "since": since_iso,
                    "total_calls": total_calls,
                    "total_reported_tokens": total_tokens,
                    "agents": summary_rows,
                    "escalations": escalation_rows
                }
        except Exception as e:
            logger.error(f"Errore recupero sommario telemetria: {e}")
            return {
                "days": days,
                "since": since_iso,
                "total_calls": 0,
                "total_reported_tokens": 0,
                "agents": [],
                "escalations": []
            }

    def format_cli_report(self, days: int = 7) -> str:
        """Genera un report formattato per la CLI."""
        summary = self.get_usage_summary(days=days)
        lines = [
            "=" * 96,
            f"📊 REPORT UTILIZZO AGENTI - ULTIMI {days} GIORNI (dal {summary['since'][:10]})",
            "=" * 96,
            f"{'Agente':<12} {'Modello':<24} {'Chiamate':<10} {'Success':<9} {'Fail':<8} {'Durata Media':<18} {'Token Totali':<12}",
            "-" * 96
        ]

        if not summary["agents"]:
            lines.append("Nessun evento di utilizzo registrato nel periodo.")
        else:
            for r in summary["agents"]:
                avg_dur = f"{r['avg_duration_ms']} ms"
                tokens_str = f"{r['total_reported_tokens']:,}" if r['total_reported_tokens'] > 0 else "-"
                lines.append(
                    f"{r['agent_role']:<12} {r['model']:<24} {r['total_calls']:<10} {r['success_calls']:<9} {r['failed_calls']:<8} {avg_dur:<18} {tokens_str:<12}"
                )

        lines.append("-" * 96)
        lines.append(f"TOTALI: {summary['total_calls']} chiamate | {summary['total_reported_tokens']:,} token riportati")

        if summary["escalations"]:
            lines.append("\n📈 CONTEGGIO ESCALATION:")
            for esc in summary["escalations"]:
                lines.append(f"  • {esc['escalation_reason']}: {esc['count']}")

        lines.append("=" * 96)
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    days_arg = 7
    if len(sys.argv) > 1:
        try:
            days_arg = int(sys.argv[1])
        except ValueError:
            pass
    repo = TelemetryRepository()
    print(repo.format_cli_report(days=days_arg))
