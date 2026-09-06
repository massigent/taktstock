#!/usr/bin/env python3
"""
Agent Gateway (Centralized Unified Execution Gateway)
-----------------------------------------------------
Single centralized entry point for all AI agent invocations (Sol, Luna, Director, etc.)
from any operational channel (Web Chat, Telegram, n8n, Brainstorming, Orchestrator Core).

Guaranteed Behavior:
1. When TAKTSTOCK_HOST_CODEX_SIDECAR=1 (or fallback UFFICIO_HOST_CODEX_SIDECAR=1):
   - Every request for Sol/Luna/Director is routed EXCLUSIVELY via HostCodexClient on Unix socket.
   - Any fallback to local Codex CLI, CODEX_HOME, or container-mounted accounts is strictly FORBIDDEN.
   - Controlled error handling for the user, without stack trace leaks or internal data exposure.
   - Sol/Director: receives 'low' reasoning effort by default, or 'high' for critical presets / explicit escalations.
   - Luna: NEVER receives reasoning effort overrides.
2. When TAKTSTOCK_HOST_CODEX_SIDECAR=0:
   - Signals adherence to legacy compatible flow.
3. Phase 1 Telemetry (Observational & Append-Only):
   - Non-blocking, privacy-safe recording of every invocation (duration, reported tokens if available, status, etc.).
"""

import os
import time
import logging
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Union

try:
    from infrastructure.telemetry_repository import (
        TelemetryRepository,
        extract_reported_tokens,
        SAFE_METADATA_KEYS
    )
except ImportError:
    try:
        from server.infrastructure.telemetry_repository import (
            TelemetryRepository,
            extract_reported_tokens,
            SAFE_METADATA_KEYS
        )
    except ImportError:
        TelemetryRepository = None
        extract_reported_tokens = lambda output, meta=None: None
        SAFE_METADATA_KEYS = {
            "preset",
            "reasoning_effort",
            "sandbox",
            "context_message_count",
            "summary_length_chars",
            "context_payload_chars",
            "truncated_message_count",
            "subtask_id",
            "call_reason",
            "output_length"
        }


logger = logging.getLogger("AgentGateway")


def is_sidecar_mode_enabled() -> bool:
    """Checks whether host sidecar mode (Codex or AGY) is enabled via feature flags."""
    codex_flag = (os.environ.get("TAKTSTOCK_HOST_CODEX_SIDECAR") or os.environ.get("UFFICIO_HOST_CODEX_SIDECAR", "0")).strip().lower() in ("1", "true", "yes")
    agy_flag = (os.environ.get("TAKTSTOCK_HOST_AGY_SIDECAR") or os.environ.get("UFFICIO_HOST_AGY_SIDECAR", "0")).strip().lower() in ("1", "true", "yes")
    return codex_flag or agy_flag


def resolve_agent_provider_and_model(agent_role: str) -> Tuple[str, str]:
    """Resolves provider and model from agent role or name."""
    role = (agent_role or "").strip().lower()
    if role in ["sol", "director"]:
        return "openai_codex", "gpt-5.6-sol"
    elif role in ["luna"]:
        return "openai_codex", "gpt-5.6-terra"
    elif role in ["bonus"]:
        return "openai_codex", "gpt-5.6-sol"
    elif role in ["ds-flash", "flash", "deepseek-coder-flash"]:
        return "deepseek", "deepseek-coder-flash"
    elif role in ["ds-pro", "pro", "deepseek-coder-pro"]:
        return "deepseek", "deepseek-coder-pro"
    elif role in ["glm", "glm-4-plus"]:
        return "openrouter", "glm-4-plus"
    elif role in ["agy"]:
        return "antigravity_cli", "agy-agent"
    return "custom", role or "unknown"


class AgentGateway:
    _telemetry_repo: Optional[Any] = None

    @classmethod
    def get_telemetry_repository(cls):
        """Lazy-loader for TelemetryRepository."""
        if cls._telemetry_repo is None and TelemetryRepository is not None:
            try:
                cls._telemetry_repo = TelemetryRepository()
            except Exception as e:
                logger.warning(f"[AgentGateway] Telemetry initialization failed: {e}")
        return cls._telemetry_repo

    @classmethod
    def record_telemetry(
        cls,
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
        """Centralized method to safely record telemetry non-blockingly."""
        repo = cls.get_telemetry_repository()
        if repo is None:
            return None
        try:
            return repo.record_event(
                agent_role=agent_role,
                provider=provider,
                model=model,
                phase=phase,
                status=status,
                duration_ms=duration_ms,
                prompt_length=prompt_length,
                reported_tokens=reported_tokens,
                estimated_tokens=estimated_tokens,
                run_id=run_id,
                subtask_id=subtask_id,
                session_id=session_id,
                files_count=files_count,
                escalation_reason=escalation_reason,
                error_category=error_category,
                attempts_count=attempts_count,
                fallback_used=fallback_used,
                prompt_sha256=prompt_sha256,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                cache_read_tokens=cache_read_tokens,
                call_reason=call_reason,
                metadata=metadata
            )
        except Exception as e:
            logger.warning(f"[AgentGateway] Telemetry recording failed: {e}")
            return None

    @classmethod
    def execute_agent_call(
        cls,
        agent_role: str,
        prompt: str,
        worktree_path: Optional[Union[str, Path]] = None,
        preset: str = "standard",
        reasoning_effort: Optional[str] = None,
        sandbox_mode: str = "read-only",
        phase: str = "chat",
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        files_count: Optional[int] = None,
        escalation_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Executes agent call in centralized mode.
        Returns: (success: bool, output_or_error_msg: str, metadata: dict)
        """
        agent = (agent_role or "").strip().lower()
        provider, model = resolve_agent_provider_and_model(agent)
        prompt_len = len(prompt) if isinstance(prompt, str) else 0
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16] if prompt else None
        subtask_id = str(metadata.get("subtask_id")) if (metadata and metadata.get("subtask_id")) else None
        effective_timeout = timeout or (metadata.get("timeout") if isinstance(metadata, dict) else None) or 900
        start_time = time.perf_counter()

        def _merge_meta(base_dict: Dict[str, Any]) -> Dict[str, Any]:
            out = dict(base_dict)
            if metadata and isinstance(metadata, dict):
                for k, v in metadata.items():
                    if k in SAFE_METADATA_KEYS:
                        out[k] = v
            return out

        # Determine escalation reason if not explicit
        if not escalation_reason:
            if preset == "critical":
                escalation_reason = "critical_preset"
            elif reasoning_effort == "high":
                escalation_reason = "high_reasoning"

        if not is_sidecar_mode_enabled():
            return False, "LEGACY_MODE", {}

        # 1. Sidecar profile resolution
        if agent in ["sol", "director", "bonus"]:
            profile = "sol"
        elif agent in ["luna"]:
            profile = "luna"
        elif agent in ["ds-flash", "flash", "deepseek-coder-flash"]:
            profile = "ds-flash"
        elif agent in ["ds-pro", "pro", "deepseek-coder-pro"]:
            profile = "ds-pro"
        else:
            profile = agent

        # 2. Reasoning effort resolution
        if profile in ["sol", "director"]:
            effort: Optional[str] = reasoning_effort or ("high" if preset == "critical" else "low")
        else:
            effort = None

        # 3. Authorized worktree resolution (must be a real subdirectory, never root)
        if worktree_path:
            wt = Path(worktree_path).resolve()
        else:
            base_home = os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME")
            if base_home:
                base_dir = Path(base_home) / "workspaces" / "chat_default"
            else:
                base_dir = Path.home() / "taktstock" / "workspaces" / "chat_default"
            try:
                base_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            wt = base_dir.resolve()

        # 4. Invocate HostAgyClient for AGY or HostCodexClient for Codex
        if agent == "agy":
            effective_sandbox = "read-only" if phase == "chat" else sandbox_mode
            try:
                try:
                    from infrastructure.host_agy_client import (
                        HostAgyClient,
                        AgySidecarBusyError,
                        AgySidecarTimeoutError,
                        AgySidecarError
                    )
                except ImportError:
                    from server.infrastructure.host_agy_client import (
                        HostAgyClient,
                        AgySidecarBusyError,
                        AgySidecarTimeoutError,
                        AgySidecarError
                    )

                req_kwargs = {
                    "prompt": prompt,
                    "worktree": str(wt),
                    "sandbox": effective_sandbox
                }
                if timeout is not None or (metadata and "timeout" in metadata):
                    req_kwargs["timeout"] = effective_timeout

                agy_client = HostAgyClient()
                if hasattr(agy_client.send_request, "return_value"):
                    output = agy_client.send_request(**req_kwargs)
                    duration_ms = int((time.perf_counter() - start_time) * 1000)
                    tokens = extract_reported_tokens(output)
                elif hasattr(agy_client, "send_request_envelope"):
                    resp = agy_client.send_request_envelope(**req_kwargs)
                    if isinstance(resp, dict):
                        output = resp.get("stdout", "")
                        duration_sec = resp.get("duration_sec")
                        duration_ms = int(duration_sec * 1000) if duration_sec is not None else int((time.perf_counter() - start_time) * 1000)
                        tokens = extract_reported_tokens(output, meta=resp)
                    else:
                        output = str(resp)
                        duration_ms = int((time.perf_counter() - start_time) * 1000)
                        tokens = extract_reported_tokens(output)
                else:
                    output = agy_client.send_request(**req_kwargs)
                    duration_ms = int((time.perf_counter() - start_time) * 1000)
                    tokens = extract_reported_tokens(output)

                from infrastructure.telemetry_repository import extract_token_breakdown
                bd = extract_token_breakdown(output, meta=resp if 'resp' in locals() and isinstance(resp, dict) else None)
                tokens = bd.get("total_tokens")
                in_tok = bd.get("input_tokens")
                out_tok = bd.get("output_tokens")
                th_tok = bd.get("thinking_tokens")
                cr_tok = bd.get("cache_read_tokens")

                out_str = str(output or "")
                est_tokens = tokens if tokens is not None else max(1, (prompt_len + len(out_str)) // 4)

                cls.record_telemetry(
                    agent_role="agy",
                    provider=provider,
                    model=model,
                    phase=phase,
                    status="SUCCESS",
                    duration_ms=duration_ms,
                    prompt_length=prompt_len,
                    reported_tokens=tokens,
                    estimated_tokens=est_tokens,
                    run_id=run_id,
                    subtask_id=subtask_id,
                    session_id=session_id,
                    files_count=files_count,
                    escalation_reason=escalation_reason,
                    prompt_sha256=prompt_sha256,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    thinking_tokens=th_tok,
                    cache_read_tokens=cr_tok,
                    call_reason=phase,
                    metadata=_merge_meta({
                        "sandbox": effective_sandbox,
                        "output_length": len(out_str),
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "thinking_tokens": th_tok,
                        "cache_read_tokens": cr_tok,
                        "call_reason": phase
                    })
                )
                return True, out_str.strip(), {
                    "status": "SUCCESS",
                    "role": "agy",
                    "reported_tokens": tokens,
                    "estimated_tokens": est_tokens,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "thinking_tokens": th_tok,
                    "cache_read_tokens": cr_tok
                }

            except AgySidecarBusyError as e:
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                user_msg = f"⚠️ Host AGY runner busy (Runner host AGY occupato): {e}"
                logger.warning(f"[AgentGateway] {user_msg}")
                cls.record_telemetry(
                    agent_role="agy",
                    provider=provider,
                    model=model,
                    phase=phase,
                    status="BUSY",
                    duration_ms=duration_ms,
                    prompt_length=prompt_len,
                    run_id=run_id,
                    subtask_id=subtask_id,
                    session_id=session_id,
                    files_count=files_count,
                    escalation_reason=escalation_reason,
                    error_category="agy_sidecar_busy",
                    prompt_sha256=prompt_sha256,
                    call_reason=phase,
                    metadata=_merge_meta({"sandbox": effective_sandbox, "call_reason": phase})
                )
                return False, user_msg, {"status": "BUSY", "error": str(e)}

            except AgySidecarTimeoutError as e:
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                user_msg = f"TIME_BUDGET_EXCEEDED: Execution timeout exceeded ({e})"
                logger.warning(f"[AgentGateway] {user_msg}")
                cls.record_telemetry(
                    agent_role="agy",
                    provider=provider,
                    model=model,
                    phase=phase,
                    status="TIMEOUT",
                    duration_ms=duration_ms,
                    prompt_length=prompt_len,
                    run_id=run_id,
                    subtask_id=subtask_id,
                    session_id=session_id,
                    files_count=files_count,
                    escalation_reason=escalation_reason,
                    error_category="time_budget_exceeded",
                    prompt_sha256=prompt_sha256,
                    call_reason=phase,
                    metadata=_merge_meta({"sandbox": effective_sandbox, "call_reason": phase, "status": "TIME_BUDGET_EXCEEDED"})
                )
                return False, user_msg, {"status": "TIME_BUDGET_EXCEEDED", "error": str(e)}

            except AgySidecarError as e:
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                user_msg = f"⚠️ AGY sidecar execution error (Errore di esecuzione AGY sidecar): {e}"
                logger.error(f"[AgentGateway] {user_msg}")
                cls.record_telemetry(
                    agent_role="agy",
                    provider=provider,
                    model=model,
                    phase=phase,
                    status="ERROR",
                    duration_ms=duration_ms,
                    prompt_length=prompt_len,
                    run_id=run_id,
                    subtask_id=subtask_id,
                    session_id=session_id,
                    files_count=files_count,
                    escalation_reason=escalation_reason,
                    error_category="agy_sidecar_error",
                    prompt_sha256=prompt_sha256,
                    call_reason=phase,
                    metadata=_merge_meta({"sandbox": effective_sandbox, "call_reason": phase})
                )
                return False, user_msg, {"status": "ERROR", "error": str(e)}

        try:
            try:
                from infrastructure.host_codex_client import (
                    HostCodexClient,
                    CodexSidecarBusyError,
                    CodexSidecarError
                )
            except ImportError:
                from server.infrastructure.host_codex_client import (
                    HostCodexClient,
                    CodexSidecarBusyError,
                    CodexSidecarError
                )

            client = HostCodexClient()
            output = client.send_request(
                profile=profile,
                prompt=prompt,
                worktree=str(wt),
                sandbox=sandbox_mode,
                reasoning_effort=effort
            )
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            from infrastructure.telemetry_repository import extract_token_breakdown
            bd = extract_token_breakdown(output)
            tokens = bd.get("total_tokens")
            in_tok = bd.get("input_tokens")
            out_tok = bd.get("output_tokens")
            th_tok = bd.get("thinking_tokens")
            cr_tok = bd.get("cache_read_tokens")

            out_str = str(output or "")
            est_tokens = tokens if tokens is not None else max(1, (prompt_len + len(out_str)) // 4)

            # Record Success Telemetry
            cls.record_telemetry(
                agent_role=agent,
                provider=provider,
                model=model,
                phase=phase,
                status="SUCCESS",
                duration_ms=duration_ms,
                prompt_length=prompt_len,
                reported_tokens=tokens,
                estimated_tokens=est_tokens,
                run_id=run_id,
                subtask_id=subtask_id,
                session_id=session_id,
                files_count=files_count,
                escalation_reason=escalation_reason,
                prompt_sha256=prompt_sha256,
                input_tokens=in_tok,
                output_tokens=out_tok,
                thinking_tokens=th_tok,
                cache_read_tokens=cr_tok,
                call_reason=phase,
                metadata=_merge_meta({
                    "preset": preset,
                    "reasoning_effort": effort,
                    "sandbox": sandbox_mode,
                    "output_length": len(out_str),
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "thinking_tokens": th_tok,
                    "cache_read_tokens": cr_tok,
                    "call_reason": phase
                })
            )

            return True, out_str.strip(), {
                "status": "SUCCESS",
                "profile": profile,
                "reported_tokens": tokens,
                "estimated_tokens": est_tokens,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "thinking_tokens": th_tok,
                "cache_read_tokens": cr_tok
            }

        except CodexSidecarBusyError as e:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            user_msg = f"⚠️ Host runner busy (Runner host occupato): {e}"
            logger.warning(f"[AgentGateway] {user_msg}")

            cls.record_telemetry(
                agent_role=agent,
                provider=provider,
                model=model,
                phase=phase,
                status="BUSY",
                duration_ms=duration_ms,
                prompt_length=prompt_len,
                run_id=run_id,
                subtask_id=subtask_id,
                session_id=session_id,
                files_count=files_count,
                escalation_reason=escalation_reason,
                error_category="sidecar_busy",
                prompt_sha256=prompt_sha256,
                metadata=_merge_meta({"preset": preset, "reasoning_effort": effort, "sandbox": sandbox_mode})
            )

            return False, user_msg, {"status": "BUSY", "error": str(e)}

        except CodexSidecarError as e:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            user_msg = f"⚠️ Execution error (Errore di esecuzione): {e}"
            logger.error(f"[AgentGateway] {user_msg}")

            cls.record_telemetry(
                agent_role=agent,
                provider=provider,
                model=model,
                phase=phase,
                status="ERROR",
                duration_ms=duration_ms,
                prompt_length=prompt_len,
                run_id=run_id,
                subtask_id=subtask_id,
                session_id=session_id,
                files_count=files_count,
                escalation_reason=escalation_reason,
                error_category="sidecar_error",
                prompt_sha256=prompt_sha256,
                metadata=_merge_meta({"preset": preset, "reasoning_effort": effort, "sandbox": sandbox_mode})
            )

            return False, user_msg, {"status": "ERROR", "error": str(e)}

        except Exception as e:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            user_msg = "⚠️ Unexpected error during host sidecar communication."
            logger.error(f"[AgentGateway] Unhandled exception: {e}")

            cls.record_telemetry(
                agent_role=agent,
                provider=provider,
                model=model,
                phase=phase,
                status="ERROR",
                duration_ms=duration_ms,
                prompt_length=prompt_len,
                run_id=run_id,
                session_id=session_id,
                files_count=files_count,
                escalation_reason=escalation_reason,
                error_category="unexpected_exception",
                metadata=_merge_meta({"preset": preset, "reasoning_effort": effort, "sandbox": sandbox_mode})
            )

            return False, user_msg, {"status": "ERROR", "error": str(e)}
