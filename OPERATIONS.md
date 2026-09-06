# 📘 Operational Runbook: Taktstock Multi-Agent

Comprehensive guide for administrators and developers covering deployment, maintenance, debugging, customization, and monitoring of the **Taktstock Multi-Agent Orchestrator**.

---

## 📑 Table of Contents

1. [Operational Architecture & E2E Flow](#1-operational-architecture--e2e-flow)
2. [Multi-Agent Brainstorming Mode (Deliberation & Approval)](#2-multi-agent-brainstorming-mode-deliberation--approval)
3. [Structured Log Management with jq](#3-structured-log-management-with-jq)
4. [Adding a New Agent or LLM Profile](#4-adding-a-new-agent-or-llm-profile)
5. [Adding or Customizing a Preset](#5-adding-or-customizing-a-preset)
6. [Debugging Failed Tasks & Resuming from Checkpoints](#6-debugging-failed-tasks--resuming-from-checkpoints)
7. [Worktree Management and Git Rollback](#7-worktree-management-and-git-rollback)
8. [Health Server & Resource Monitoring](#8-health-server--resource-monitoring)
9. [Centralized Configuration (config.json)](#9-centralized-configuration-configjson)

---

## 1. Operational Architecture & E2E Flow

Taktstock operates as a headless service on Linux hosts or containers:
1. **Trigger**: The operator initiates a request via Telegram (`/task ...` or `/brainstorm ...`) or HTTP API (`POST /api/run`).
2. **n8n Gateway / API**: The workflow or API receiver validates the command and forwards it to `orchestrator_core.py`.
3. **Structured Brainstorming (Optional)**: Interactive deliberation rounds with Sol, DeepSeek Pro, and GLM before touching code.
4. **Worktree Isolation**: A dedicated directory is provisioned in `~/taktstock/worktrees/wt_<timestamp>`.
5. **Decomposition & Planning**: The Director (`GPT-5.6 Sol`) decomposes the task into atomic subtasks.
6. **Execution & Checkpoints**: `agy` executes local changes, persisting checkpoints in `~/taktstock/state/checkpoint_<branch>.json`.
7. **Double Review & Fix**: Multi-model review (GLM 5.3 + DeepSeek Pro) and targeted self-healing with DeepSeek Flash when needed.
8. **Diff Review & Push**: Annotated HTML diff report generated in `~/taktstock/diffs/`, followed by branch commit and push.
9. **Cleanup**: Automatic disposal of the temporary worktree in a `try...finally` block.

---

## 2. Multi-Agent Brainstorming Mode (Deliberation & Approval)

Brainstorming mode enables structured technical discussion with the agent team (Sol, DeepSeek Pro, GLM) to refine requirements and architecture before authorizing code modifications.

### Telegram Commands:

| Command | Description |
|---|---|
| `/brainstorm <task>` | Initiates a new brainstorming session (Round 1) |
| `/opinion <feedback>` | Submits guidance or constraints for the next round |
| `/ask-reviewers` | Requests targeted architectural alternatives from Reviewers |
| `/plan-status` | Displays the status and summary of the active session |
| `/approve-plan` | **Approves the plan and immediately initiates development orchestration** |
| `/reject-plan <reason>` | Rejects the plan and cancels the session |

### Equivalent CLI Commands:

```bash
# 1. Start brainstorming
python3 server/orchestrator_core.py --brainstorm "Add Google OAuth2 login" --preset critical

# 2. Continue with feedback
python3 server/orchestrator_core.py --brainstorm-continue bs_20260822_123456 --feedback "Use SQLite for sessions"

# 3. Approve and start execution
python3 server/orchestrator_core.py --brainstorm-approve bs_20260822_123456

# 4. Reject session
python3 server/orchestrator_core.py --brainstorm-reject bs_20260822_123456 --reason "Too complex"

# 5. Check status
python3 server/orchestrator_core.py --brainstorm-status bs_20260822_123456
```

---

## 3. Structured Log Management with jq

All logs are emitted concurrently in standard text format (`orch_*.log`) and in **JSONLines** (`orch_*.jsonl`) under `~/taktstock/logs/`.

### Useful `jq` queries:

```bash
# 1. Follow live run logs in real time
tail -f ~/taktstock/logs/orch_*.jsonl | jq .

# 2. Filter errors and warnings only
cat ~/taktstock/logs/orch_*.jsonl | jq 'select(.level=="ERROR" or .level=="WARNING")'

# 3. Find all operations exceeding 1,000 tokens
cat ~/taktstock/logs/orch_*.jsonl | jq 'select(.tokens > 1000)'

# 4. Trace actions performed by a specific agent (e.g. agy)
cat ~/taktstock/logs/orch_*.jsonl | jq 'select(.agent=="agy")'

# 5. Display metrics for all completed runs
cat ~/taktstock/state/runs_history.jsonl | jq '{task: .task, branch: .branch, tokens: .tokens_used.total, status: .status}'
```

---

## 4. Adding a New Agent or LLM Profile

To configure a new model or agent profile (e.g. `gemini-pro` or `claude-opus`):

1. **Add the profile TOML configuration in `~/.codex/`**:
   ```toml
   # ~/.codex/gemini.config.toml
   model = "gemini-2.5-pro"
   model_provider = "google"
   ```
2. **Update `server/config.json`**:
   ```json
   "reviewers": {
     "gemini": {
       "enabled": true,
       "profile": "gemini",
       "model": "gemini-2.5-pro"
     }
   }
   ```
3. **Invoke via CLI**:
   ```bash
   python3 server/orchestrator_core.py "My task" --reviewers "gemini,ds-pro"
   ```

---

## 5. Adding or Customizing a Preset

Presets are declared in `server/config.json`. To create a custom preset (e.g. `security-heavy`):

```json
{
  "presets": {
    "security-heavy": {
      "director": "sol",
      "reviewers": ["ds-pro", "glm"],
      "allow_escalation": true,
      "description": "Comprehensive security and OWASP compliance review"
    }
  }
}
```

Invocation via Telegram:
```text
/task https://github.com/org/repo Auth migration --preset security-heavy
```

---

## 6. Debugging Failed Tasks & Resuming from Checkpoints

If a task halts due to host interruption or timeout:

1. **Inspect the latest persisted checkpoint**:
   ```bash
   cat ~/taktstock/state/checkpoint_<branch_name>.json | jq .
   ```
2. **Inspect the diff generated up to that step**:
   ```bash
   open ~/taktstock/diffs/diff_*.html  # on macOS
   # or on Linux server inspect the record:
   cat ~/taktstock/state/runs_history.jsonl | tail -n 1 | jq .
   ```
3. **Preserve the worktree on disk for manual inspection**:
   Run with `--keep-worktree` or export:
   ```bash
   export TAKTSTOCK_KEEP_WORKTREE="1"
   ```
   The intact filesystem will remain in `~/taktstock/worktrees/wt_<branch>`.

---

## 7. Worktree Management and Git Rollback

### List active worktrees:
```bash
git -C ~/taktstock/repos/<repo_name> worktree list
```

### Prune orphaned worktrees manually:
```bash
git -C ~/taktstock/repos/<repo_name> worktree prune
```

### Roll back a generated branch:
```bash
# If the remote branch should not be merged:
git push origin --delete feature/taktstock-123456789
```

---

## 8. Health Server & Resource Monitoring

Taktstock includes a lightweight HTTP monitoring server (`server/health_server.py`) executable via PM2 or systemd:

### Starting the Health service:
```bash
pm2 start ~/taktstock/server/health_server.py --name "taktstock-health" --interpreter python3
pm2 save
```

### Querying HTTP Health status:
```bash
curl http://localhost:8765/health | jq .
```

Example response:
```json
{
  "status": "healthy",
  "system": "Taktstock Multi-Agent Orchestrator",
  "current_account": "env_key_1",
  "accounts_count": 2,
  "accounts": [
    {
      "name": "env_key_1",
      "available": true,
      "cooldown_until": 0.0,
      "failure_count": 0,
      "success_count": 12
    }
  ],
  "active_worktrees_count": 0,
  "active_worktrees": [],
  "recent_runs_count": 4
}
```

---

## 9. Centralized Configuration (`config.json`)

Runtime settings are controlled in `~/taktstock/config.json`:

| Parameter | Default | Description |
|---|---|---|
| `director.default_model` | `gpt-5.6-sol` | Director model for planning and quick evaluation |
| `director.escalation_model` | `gpt-5.6-sol` | High-reasoning model for critical tasks or escalation |
| `executor.timeout` | `15m` | Maximum execution timeout for `agy` (supports `10m`, `30m`, `1h`) |
| `executor.max_tokens_per_task` | `50000` | Warning threshold for token consumption per subtask |
| `notifications.webhook_url` | `http://localhost:5678/...` | Webhook endpoint for live n8n/Telegram progress notifications |
| `storage.keep_worktree_on_error`| `false` | When `true`, preserves worktree directory upon task failure |
