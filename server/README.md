# Taktstock Multi-Agent Orchestrator (Server Edition)

Multi-agent architecture for headless software engineering and governance, orchestrated via **n8n** and HTTP API.

---

## 🏗️ Architecture

```
               ┌───────────────────────┐
               │    TELEGRAM / WEB     │ (Launch tasks from any device)
               └───────────┬───────────┘
                           │
               ┌───────────▼───────────┐
               │          n8n          │ (Orchestrator, triggers & notifications)
               └───────────┬───────────┘
                           │
               ┌───────────▼───────────┐
               │   DIRECTOR (Codex)    │ (GPT-5.6 Terra / Sol)
               │ Brainstorm & Planning │
               └───────────┬───────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
┌────────▼────────┐ ┌──────▼────────┐ ┌──────▼────────┐
│    EXECUTOR     │ │  REVIEWER 1   │ │  REVIEWER 2   │
│    agy (90%)    │ │  Claude Code  │ │ Codex DeepSeek│
│ Antigravity CLI │ │  + GLM 5.3    │ │   V4 Pro      │
└────────┬────────┘ └──────┬────────┘ └──────┬────────┘
         │                 │                 │
         └────────┬────────┴─────────────────┘
                  │
         ┌────────▼────────┐
         │     FIXER       │
         │ DeepSeek Flash  │
         └────────┬────────┘
                  │
         ┌────────▼────────┐
         │   DIFF REVIEW   │ (Annotated HTML report + Telegram summary)
         └────────┬────────┘
                  │
         ┌────────▼────────┐
         │    GIT PUSH     │ (Automated commit and branch ready for PR)
         └─────────────────┘
```

---

## 🌟 Advanced Features

### 1. 🌿 Git Worktree Isolation
- **Isolated and parallel execution**: Every task runs in its own isolated `git worktree` (`~/taktstock/worktrees/wt_...`) linked to the central repository clone (`~/taktstock/repos/...`).
- Zero conflicts across parallel branches and files, with automatic cleanup upon completion.

### 2. 📋 Visual Diff Review & Human Approval Flow
- **Annotated HTML report**: Automatically generates a self-contained, dark-themed HTML report displaying colorized line-by-line diffs (`~/taktstock/diffs/diff_...html`).
- **Telegram summary**: Dispatches immediate metrics (`+X / -Y` lines across N files) and diff extracts prior to final commit.
- **`--require-approval` flag**: Blocks branch push pending explicit human confirmation for production-critical changes.

### 3. 🔄 Account Hot-Switching & Failover (429 Rate Limit)
- Automatic management of multiple Codex/OpenAI accounts (`OPENAI_API_KEYS="sk-1,sk-2"` or `~/.codex/accounts/` directories).
- If an account hits a rate limit (HTTP 429 or quota exhaustion), the system rotates instantly to the next account without breaking execution.

### 4. 🎨 Design Mode (Playwright + Screenshot + DOM)
- Visual capture of frontend interfaces (Astro, Appsmith, React, Tailwind):
  ```bash
  python3 orchestrator_core.py "Realign navbar buttons" --design-url "http://localhost:3000" --design-selector ".navbar"
  ```
- Captures high-resolution screenshots, extracts computed CSS styles and DOM tree structures, injecting visual context directly into multimodal prompts for `agy` and the Director.

### 5. 🧠 Structured Brainstorming Mode (Deliberation & Approval)
- **Deliberative planning**: Facilitates structured discussion rounds with Sol (Director), DeepSeek Pro, and GLM before writing any code.
- **Multi-round workflow**:
  - `Round 1`: Sol strategic analysis $\rightarrow$ DeepSeek Pro + GLM evaluations $\rightarrow$ synthesis and questions for the operator.
  - `Round N`: Continuous refinement based on operator guidance and constraints.
- **Explicit approval**: Execution starts only upon explicit operator confirmation (`/approve` or `--brainstorm-approve`).
- **Interactive dashboard**: Accessible via browser at `http://<server-ip>:8765/brainstorm/<id>` with web controls to submit feedback or approve plans.

---

## 🚀 Quick Setup on Ubuntu (One-Line Setup)

1. **Clone or copy the repository to your host**:
   ```bash
   git clone https://github.com/massigent/taktstock ~/taktstock
   cd ~/taktstock/server
   chmod +x orchestrator_core.py
   ```

2. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **(Optional) Install Playwright for Design Mode**:
   ```bash
   pip install playwright && playwright install chromium
   ```

4. **Configure environment variables** in `.env` (or PM2 environment):
   ```bash
   cp ../.env.example ../.env
   # Edit with your credentials:
   # OPENAI_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY, TAKTSTOCK_AUTH_TOKEN
   ```

5. **Authenticate `agy` once on the host**:
   ```bash
   agy
   ```

6. **Start the health and API server**:
   ```bash
   python3 health_server.py
   # Or with PM2:
   pm2 start health_server.py --name "taktstock-server" --interpreter python3
   ```

---

## 📱 n8n Workflow Configuration

1. Open your n8n web dashboard at `http://<YOUR-SERVER-IP>:5678`.
2. Click **Add Workflow** -> **Import from File** and select [`Taktstock Multi-Agent Orchestrator.json`](./Taktstock%20Multi-Agent%20Orchestrator.json).
3. Configure the **Telegram Trigger** credentials (obtain bot credentials from [@BotFather](https://t.me/BotFather)).
4. Configure the **Taktstock API Auth** header credential (`X-Taktstock-Token`).
5. Activate the workflow (**Active: ON**).

---

## 💻 Usage via Telegram / CLI

Send a message to your Telegram Bot:

```text
/task https://github.com/your-org/your-project Add JWT authentication with refresh tokens and unit tests
```

Or with Design Mode for frontend changes:
```text
/task https://github.com/your-org/frontend-astro Fix footer padding --design-url http://localhost:4321
```

Taktstock performs workspace isolation, planning, execution via `agy`, automated multi-model review, self-healing fixes, and visual diff generation.
