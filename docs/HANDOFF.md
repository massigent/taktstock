# Operational Log & Project Handoff: Taktstock Multi-Agent

**Last Updated**: 2026-08-25T10:37:00+02:00
**Repository**: `/opt/taktstock` (Server) / `taktstock` (Workspace)

---

## 1. Project Purpose & Agent Roles

Taktstock is a multi-agent orchestration platform for responsible software development, architectural review, and operational automation via HTTP API and Telegram/n8n.

### Current Agent Roles:
- **Sol / Director (`gpt-5.6-sol`)**: Planning, strategic coordination, review, and synthesis. Executes by default with `model_reasoning_effort="low"`; scales to `high` only on `critical` preset or explicit escalation. Operational via Host Sidecar.
- **Luna (GPT-5.6 Terra)**: Specialist for live n8n modifications via MCP and analytical tasks (receives no reasoning effort override). Authenticated workspace active and operational via Host Sidecar.
- **Bonus / Fallback (`gpt-5.6-sol`)**: Secondary account for automatic rotation and failover.
- **AGY (Antigravity CLI Executor)**: Specialized executive agent for codebase inspection, test running, and code modification inside isolated worktrees.
- **Specialist Reviewers & Fixers**: Pro (`deepseek-v4-pro`), Flash (`deepseek-v4-flash`), GLM (`glm-5.3`).

---

## 2. Sidecar Architecture & Live Deployment (User Units + Linger)

> **Architecture Status (P0 / P0.2 / P1 / P1D / P2)**: **FULLY DEPLOYED AND OPERATIONAL**.
> The system is managed entirely via **systemd user units** (`systemctl --user`) with user lingering (`loginctl enable-linger $USER`, `Linger=yes`), requiring neither root privileges nor writes to `/etc`.

### Production Components:
- **Codex Runner Host Sidecar (`~/.config/systemd/user/taktstock-codex.service`)**:
  - Runs as unprivileged user (UID 1000).
  - Runtime directory on user tmpfs: `/run/user/1000/taktstock-codex` (mode `0750`).
  - Atomic runtime token: `/run/user/1000/taktstock-codex/token` (mode `0400`) provisioned by `provision_runtime_token.py` from `~/.config/taktstock-codex/sidecar.env`.
  - Unix Domain Socket: `/run/user/1000/taktstock-codex/codex.sock` (mode `0600`).
  - Authenticated `ready` and `execute` protocol with environment sanitization.
- **AGY Runner Host Sidecar (`~/.config/systemd/user/taktstock-agy.service`)**:
  - Runs as unprivileged user (UID 1000).
  - Runtime directory on user tmpfs: `/run/user/1000/taktstock-agy` (mode `0750`).
  - Independent runtime token: `/run/user/1000/taktstock-agy/token` (mode `0400`) from `~/.config/taktstock-agy/sidecar.env` (`TAKTSTOCK_AGY_SIDECAR_TOKEN`).
  - Isolated environment: `AGY_HOME=~/.config/taktstock-agy/home`, `AGY_BIN=~/.local/bin/agy`.
  - Unix Domain Socket: `/run/user/1000/taktstock-agy/agy.sock` (mode `0600`).
  - Authenticated `ready` and `execute` protocol with strict allowlist environment sanitization, 512 KB output cap, single-job mutual exclusion (BUSY), and mandatory `--sandbox` flags (`--mode plan` for read-only, `--mode accept-edits` for workspace-write).
- **Container Supervision (`~/.config/systemd/user/taktstock-server.service`)**:
  - Auto-start at boot via `Linger=yes` and dependencies `After=network.target taktstock-codex.service taktstock-agy.service`.
  - Boot resilience configuration: `StartLimitIntervalSec=0` and `Restart=always` (`RestartSec=5s`) to prevent rate-limiting during asynchronous system Docker daemon startup.
  - Readiness Gate: `wait_for_sidecar.py` verifies socket existence, token validity, and authenticated handshake (`action: ready`) before launching containers.
  - Unified Read-Only Mounts:
    - `/run/user/1000/taktstock-codex:/run/taktstock-codex:ro`
    - `/run/user/1000/taktstock-agy:/run/taktstock-agy:ro`
- **Docker Compose (`docker-compose.yml`)**:
  - `restart: "no"`, `TAKTSTOCK_HOST_CODEX_SIDECAR=1`, `TAKTSTOCK_HOST_AGY_SIDECAR=1`, read-only mounts to `/run/taktstock-codex` and `/run/taktstock-agy`.
- **Orchestrator, Brainstorm & Gateway**:
  - `AgentGateway`: automatic routing to `agy` with deterministic sandbox (`read-only` in `chat` phase, `workspace-write` in orchestrator tasks).
  - `MultiAgentRunner.call_executor_agy()`: delegates entirely to `AgentGateway` fail-closed with zero local CLI fallback.

---

## 3. Secret Provisioning & Ready Protocol

### 1. Single Master Source & Atomic Provisioning
- Persistent master sources: `~/.config/taktstock-codex/sidecar.env` and `~/.config/taktstock-agy/sidecar.env` (permissions `0600`).
- Runtime tokens on user tmpfs: `/run/user/1000/taktstock-codex/token` and `/run/user/1000/taktstock-agy/token` (permissions `0400`, created with `O_EXCL`, `O_NOFOLLOW`, anti-symlink `lstat` verification, and `fsync`).
- IPC Protocol: authenticated handshake `{"auth_token": "...", "action": "ready"}`.
- Environment sanitization: strict allowlist for AGY and complete removal of `TAKTSTOCK_*` variables, tokens, and credentials before child execution.

---

## 4. Status of Production Enhancements (P0 / P1 / P1D / R1 / P2)

- **P0 - Sidecar Architecture Alignment (Codex + AGY), Token Provisioner & Readiness Gate**:
  - **STATUS**: **COMPLETED & DEPLOYED IN PRODUCTION**.
- **P0.2 - Docker Boot Resilience & StartLimitIntervalSec**:
  - **STATUS**: **COMPLETED & DEPLOYED IN PRODUCTION**.
- **P1 - Buzz Chat Persistence, Multi-Process Concurrency & Validated Compaction**:
  - **STATUS**: **COMPLETED & DEPLOYED IN PRODUCTION**.
  - Multi-process `SessionFileLock` (`fcntl.flock`) on `.locks/`, optimistic `revision` CAS, strict summary schema validation, quarantine handling via `chat_summary_quarantine`, multi-day conversation tail via `get_chat_tail()`.
- **P1D - Atomic Mode 0600 File Permissions**:
  - **STATUS**: **COMPLETED & DEPLOYED IN PRODUCTION**.
  - `atomic_write_json()` creates and overwrites session files and mapping metadata with mode `0600` regardless of umask.
- **R1 - Historical Session Recovery (`bs_20260823_171003.json`)**:
  - **STATUS**: **COMPLETED & VERIFIED IN PRODUCTION**.
  - 19 raw messages preserved intact (SHA256 verified: `2222a8ae8fb738364db0e458b800e656b3f8b8f5b08d097e38c6831897b661ce`), `revision: 1`, `channel: "unknown"`, `compacted_up_to_index: 0`, mode `0600`.
- **P2 - SQLite Shadow Persistence, Privacy-Safe Telemetry, Context Budget & Async Queue**:
  - **STATUS**: **COMPLETED, ACTIVE & VERIFIED IN PRODUCTION**.
  - **Production Verification & Health**: **All tests passing**; user units `taktstock-codex.service`, `taktstock-agy.service`, and `taktstock-server.service` active; `taktstock-server` container healthy; HTTP health endpoint 200 (`/health`).
  - **All P2 Feature Flags Active (`server/config.json`)**:
    - `storage.sqlite_shadow_write`: `true`
    - `storage.sqlite_shadow_read`: `true`
    - `storage.sqlite_run_shadow_write`: `true`
    - `storage.async_runs`: `true`
  - **The 4 Rollout Phases Successfully Validated in Production**:
    1. **Phase 1 (SQLite Shadow Write)**: Specular asynchronous WAL persistence for sessions and messages. Precedence resolver verified (`explicit_arg > os.environ > config.json["storage"] > fail_closed`), 1:1 record parity, zero log errors.
    2. **Phase 2 (SQLite Shadow Read)**: Transparent shadow reads with semantic verification and fail-safe fallback to primary JSON for legacy sessions.
    3. **Phase 3 (SQLite Run Shadow Write)**: Complete run lifecycle tracking (`MultiAgentRunner`) with progressive status updates up to terminal state `completed` (progress 100%) in the SQLite `runs` table.
    4. **Phase 4 (Async Run Queue & n8n/Telegram Polling)**: Decoupled `/api/run` returning immediate `HTTP 202 Accepted` (`{"status": "QUEUED", "run_id": "<uuid>"}`) with background `QueueWorker`. Live n8n workflow polls `GET /api/runs/<run_id>` (10s interval, max 180 cycles / 30 min). Validated with Telegram smoke tests (`P2_ASYNC_N8N_SMOKE_20260825`): no premature success notifications on ACK, exactly one completion alert dispatched upon finish.
  - **Live n8n Workflow**:
    - Name: `Taktstock Multi-Agent Orchestrator` (ID `zOzkSLhPySImEH5I`), published and active in the n8n container.
    - Hybrid synchronous 200 / asynchronous 202 support with timeout and error routing.
    - `Send Telegram ACK` node configured with `parse_mode: None` to avoid markdown parsing errors on dynamic command syntax.
  - **Privacy-Safe Append-Only Gateway Telemetry**: `TelemetryRepository` and `telemetry_reporter.py` CLI; isolated records of latency (ms), token consumption from envelopes, status, roles, and escalation. Strict rule against storing raw prompts, completions, tokens, authentication data, or absolute paths.
  - **Context Budget & Token Efficiency**: 32,000 character hard cap on aggregate agent payloads, 3,000 character compactor summary bound, 12,000 character backward compaction window, 20-message tail with strict exclusion of compactor warnings and sidecar operational errors.

---

## 5. Live Monitoring & E2E Verification Commands

```bash
# 1. Systemd User Units Status
systemctl --user status taktstock-codex.service taktstock-agy.service taktstock-server.service

# 2. HTTP Health Endpoint Check
curl -s http://127.0.0.1:8765/health

# 3. In-Container E2E IPC Verification
docker exec taktstock-server python3 -c '
from infrastructure.agent_gateway import AgentGateway
print("Codex:", AgentGateway.execute_agent_call(agent_role="sol", prompt="ping", worktree_path="/app/workspaces/chat_default", sandbox_mode="read-only", phase="chat")[0])
print("AGY:  ", AgentGateway.execute_agent_call(agent_role="agy", prompt="ping", worktree_path="/app/workspaces/chat_default", sandbox_mode="read-only", phase="chat")[0])
'
```

---

## 6. Rollback & Version Information

- **User Units**: `~/.config/systemd/user/taktstock-codex.service`, `taktstock-agy.service`, `taktstock-server.service`
- **Runtime Utilities**: `server/infrastructure/provision_runtime_token.py` and `server/infrastructure/wait_for_sidecar.py`
- **Session Records**: `state/brainstorms/bs_20260823_171003.json` (mode `0600`, revision `1`, 19 messages)
