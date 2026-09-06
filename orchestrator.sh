#!/usr/bin/env bash
set -euo pipefail

# ════════════════════════════════════════════════════════════
#  Taktstock Multi-Agent Orchestrator (Headless Mode)
#
#  Director:  Sol (strategic & selective routing)
#  Reviewers: GLM 5.3 (security) | DeepSeek V4 Pro (architecture)
#  Executor:  agy (constrained local execution)
#  Fixer:     DeepSeek V4 Flash (minimal fixes)
#
#  Usage:
#    ./orchestrator.sh "task description"
#    ./orchestrator.sh --dry-run "task description"
#    ./orchestrator.sh --health
# ════════════════════════════════════════════════════════════

ORCH_HOME="${ORCH_HOME:-$HOME/taktstock}"
PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
STATE_FILE="$ORCH_HOME/state/state.json"
LOG_DIR="$ORCH_HOME/logs"
TS=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/orch_${TS}.log"
TOTAL_TOKENS_FILE="$ORCH_HOME/state/tokens.json"

# Codex profiles (see guide for setup)
DIR_PROFILE="${DIR_PROFILE:-sol}"              # Sol: default director
SOL_PROFILE="${SOL_PROFILE:-sol}"              # Alias for escalation compatibility
DS_PRO_PROFILE="${DS_PRO_PROFILE:-ds-pro}"     # DeepSeek V4 Pro, effort high
DS_FLASH_PROFILE="${DS_FLASH_PROFILE:-ds-flash}"  # DeepSeek V4 Flash, effort low

# GLM via OpenRouter / Anthropic compatible proxy
CLAUDE_GLM_URL="${CLAUDE_GLM_URL:-https://openrouter.ai/api/v1}"
CLAUDE_GLM_MODEL="${CLAUDE_GLM_MODEL:-z-ai/glm-5.3}"
CLAUDE_GLM_TOKEN="${OPENROUTER_API_KEY:-${CLAUDE_GLM_TOKEN:-}}"

# agy settings
AGY_TIMEOUT="${AGY_TIMEOUT:-15m}"
AGY_MODEL="${AGY_MODEL:-}"  # empty = agy default

# ── Director System Prompt ──────────────────────────────────
read -r -d '' DIR_PROMPT <<'PROMPT' || true
You are Sol, operational director of a multi-agent software development system.
OBJECTIVE: Best outcome with lowest reasonable token consumption. Reason deeply when risk, complexity, or uncertainty warrant it.

AVAILABLE AGENTS:
- agy: executor for filesystem, CLI, tests, and local code. Executes only assigned task.
- claude-glm: independent reviewer for security, authorization, secrets, external inputs, and privacy.
- deepseek-pro: independent reviewer for architecture, data, edge cases, concurrency, and failure modes.
- deepseek-flash: economical fixer for minimal and verifiable fixes; does not make architectural decisions.

RULES:
1. Output VALID JSON ONLY. Zero prose, zero markdown, zero explanations.
2. Minimal tokens: short field names, omit optional fields.
3. Decompose into minimal but complete and testable units.
4. Default executor: agy. Default fixer: deepseek-flash.
5. Call deepseek-pro only if a second architectural opinion can alter the decision; call claude-glm only for concrete security surface. Both only for significant cross-cutting risks.
6. For each subtask generate the exact and complete prompt the agent must execute.
7. If unsure about a critical decision, add "escalate":true.
8. Do not delegate planning to agy and do not request reviews out of habit.

DECOMPOSITION FORMAT:
{"phase":"decompose","tasks":[{"id":"T1","d":"description","a":"agy","p":"exact prompt for agent","r":true,"pr":"high"}]}
  r = true if requires dual review (claude-glm + deepseek-pro)
  pr = priority: high|med|low

VALIDATION FORMAT:
{"phase":"validate","status":"done|retry","retry":["T1"],"notes":""}

SINGLE TASK CHECK FORMAT:
{"ok":true,"notes":"","escalate":false}
PROMPT

# ── AGY Execution Contract ──────────────────────────────────
read -r -d '' AGY_PROMPT <<'PROMPT' || true
You are AGY, specialized operative. Execute, do not direct.
1. Work only within project and on assigned task; read instructions and relevant files first.
2. Do not alter scope, architecture, dependencies, deployment, credentials, or live n8n workflows.
3. Do not use destructive actions, do not commit/push, and do not perform opportunistic refactoring.
4. Test the cheapest hypothesis first, apply minimal changes, and verify with proportional test/lint/build.
5. Do not declare success without real verification. If you find blockers or ambiguities, stop and report them.
6. Do not expose secrets.
Output concise JSON: status, changed, verification, risks, next_action. Report observable facts only.
PROMPT

# ════════════════════════════════════════════════════════════
#  INIT & HEALTH CHECK
# ════════════════════════════════════════════════════════════

init() {
  mkdir -p "$ORCH_HOME/state" "$LOG_DIR"
  if [[ ! -f "$STATE_FILE" ]]; then
    echo '{"phase":"init","tasks":[],"brainstorm":""}' | jq '.' > "$STATE_FILE" 2>/dev/null || \
      echo '{"phase":"init","tasks":[],"brainstorm":""}' > "$STATE_FILE"
  fi
  echo '{"agy":0,"total":0}' > "$TOTAL_TOKENS_FILE" 2>/dev/null || true
}

health_check() {
  echo "Health Check"
  echo "============"
  local ok=true

  for cmd in codex agy claude jq python3; do
    if command -v "$cmd" &>/dev/null; then
      echo "  [OK] $cmd"
    else
      echo "  [MISSING] $cmd"
      ok=false
    fi
  done

  echo ""
  echo "Codex profiles:"
  for p in "$DIR_PROFILE" "$SOL_PROFILE" "$DS_PRO_PROFILE" "$DS_FLASH_PROFILE"; do
    if [[ -f "$HOME/.codex/${p}.config.toml" ]]; then
      echo "  [OK] $p"
    else
      echo "  [MISSING] ~/.codex/${p}.config.toml"
      ok=false
    fi
  done

  echo ""
  echo "Environment:"
  [[ -n "${DEEPSEEK_API_KEY:-}" ]] && echo "  [OK] DEEPSEEK_API_KEY" || echo "  [MISSING] DEEPSEEK_API_KEY"
  [[ -n "${OPENROUTER_API_KEY:-${CLAUDE_GLM_TOKEN:-}}" ]] && echo "  [OK] OPENROUTER_API_KEY / GLM" || echo "  [MISSING] OPENROUTER_API_KEY"
  [[ -n "${OPENAI_API_KEY:-}" ]] && echo "  [OK] OPENAI_API_KEY" || echo "  [WARN] OPENAI_API_KEY (maybe OAuth is used)"

  echo ""
  if $ok; then echo "All ready."; else echo "Complete MISSING items before running this script."; fi
}

# ════════════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════════════

log() {
  local agent="$1" msg="$2"
  echo "[$(date +%H:%M:%S)][${agent}] ${msg}" | tee -a "$LOG_FILE"
}

# ════════════════════════════════════════════════════════════
#  JSON UTILITIES
# ════════════════════════════════════════════════════════════

extract_json() {
  local text="$1"
  echo "$text" | python3 -c "
import sys, json, re
text = sys.stdin.read()
start = text.find('{')
end = text.rfind('}')
if start != -1 and end != -1 and end > start:
    candidate = text[start:end+1]
    try:
        obj = json.loads(candidate)
        print(json.dumps(obj, ensure_ascii=False))
        sys.exit(0)
    except Exception:
        pass
for line in text.split(chr(10)):
    s = line.find('{')
    e = line.rfind('}')
    if s != -1 and e != -1 and e > s:
        try:
            obj = json.loads(line[s:e+1])
            print(json.dumps(obj, ensure_ascii=False))
            sys.exit(0)
        except Exception:
            continue
print('{}')
" 2>/dev/null
}

track_tokens() {
  local agent="$1" usage_json="$2"
  if [[ -z "$usage_json" || "$usage_json" == "null" ]]; then return; fi
  local tokens
  tokens=$(echo "$usage_json" | jq -r '.total_tokens // 0' 2>/dev/null || echo "0")
  local current
  current=$(jq -r --arg a "$agent" '.[$a] // 0' "$TOTAL_TOKENS_FILE" 2>/dev/null || echo "0")
  local new_total
  new_total=$(jq -r '.total // 0' "$TOTAL_TOKENS_FILE" 2>/dev/null || echo "0")
  new_total=$((new_total + tokens))
  current=$((current + tokens))
  jq --arg a "$agent" --argjson t "$current" --argjson tot "$new_total" \
    '.[$a] = $t | .total = $tot' "$TOTAL_TOKENS_FILE" > "${TOTAL_TOKENS_FILE}.tmp" 2>/dev/null && \
    mv "${TOTAL_TOKENS_FILE}.tmp" "$TOTAL_TOKENS_FILE" || true
}

# ════════════════════════════════════════════════════════════
#  AGENT CALL FUNCTIONS
# ════════════════════════════════════════════════════════════

call_director() {
  local prompt="$1"
  local profile="${2:-$DIR_PROFILE}"
  log "DIRECTOR" "Profile: $profile"
  cd "$PROJECT_DIR"
  local result
  if [[ "$profile" == "$SOL_PROFILE" || "$profile" == "director" || "$profile" == "sol" ]]; then
    local effort="low"
    if [[ "${PRESET:-standard}" == "critical" ]]; then
      effort="high"
    fi
    result=$(codex exec --profile "$profile" -c "model_reasoning_effort=\"$effort\"" "$prompt" 2>&1) || true
  else
    result=$(codex exec --profile "$profile" "$prompt" 2>&1) || true
  fi

  local json escalate
  json=$(extract_json "$result")
  escalate=$(echo "$json" | jq -r '.escalate // false' 2>/dev/null || echo "false")

  if [[ "$escalate" == "true" && "$profile" != "$SOL_PROFILE" ]]; then
    log "DIRECTOR" "Escalation to Sol requested by director (effort: high)"
    result=$(codex exec --profile "$SOL_PROFILE" -c 'model_reasoning_effort="high"' "$prompt" 2>&1) || true
  fi
  echo "$result"
}

call_executor() {
  local prompt="$1"
  log "AGY" "Task execution (model: ${AGY_MODEL:-default})"
  cd "$PROJECT_DIR"
  local -a cmd=(agy -p "${AGY_PROMPT}

TASK ASSIGNED BY SOL:
${prompt}")
  if [[ -n "${AGY_MODEL:-}" ]]; then
    cmd+=(--model "$AGY_MODEL")
  fi
  cmd+=(--output-format json --print-timeout "$AGY_TIMEOUT")
  local result
  result=$("${cmd[@]}" 2>&1) || true

  local status response usage
  status=$(echo "$result" | jq -r '.status // "ERROR"' 2>/dev/null || echo "ERROR")
  response=$(echo "$result" | jq -r '.response // ""' 2>/dev/null || echo "")
  usage=$(echo "$result" | jq -r '.usage // empty' 2>/dev/null || echo "")

  if [[ "$status" != "SUCCESS" ]]; then
    log "AGY" "WARNING: status=$status"
  fi
  track_tokens "agy" "$usage"
  echo "$response"
}

call_reviewer_claude() {
  local prompt="$1"
  log "CLAUDE-GLM" "Review critical task (GLM)"
  cd "$PROJECT_DIR"
  local token="${CLAUDE_GLM_TOKEN:-${OPENROUTER_API_KEY:-}}"
  if [[ -z "$token" ]]; then
    log "CLAUDE-GLM" "WARNING: No token set (CLAUDE_GLM_TOKEN or OPENROUTER_API_KEY)"
  fi
  local result
  result=$(ANTHROPIC_BASE_URL="$CLAUDE_GLM_URL" \
    ANTHROPIC_AUTH_TOKEN="$token" \
    ANTHROPIC_MODEL="$CLAUDE_GLM_MODEL" \
    claude -p "$prompt" --output-format json 2>&1) || true
  echo "$result"
}

call_reviewer_ds() {
  local prompt="$1"
  log "DS-PRO" "Complex analysis review (DeepSeek V4 Pro)"
  cd "$PROJECT_DIR"
  local result
  result=$(codex exec --profile "$DS_PRO_PROFILE" "$prompt" 2>&1) || true
  echo "$result"
}

call_fixer() {
  local prompt="$1"
  log "DS-FLASH" "Fast fix/test (DeepSeek V4 Flash)"
  cd "$PROJECT_DIR"
  local result
  result=$(codex exec --profile "$DS_FLASH_PROFILE" "$prompt" 2>&1) || true
  echo "$result"
}

# ════════════════════════════════════════════════════════════
#  STATE MANAGEMENT
# ════════════════════════════════════════════════════════════

save_state() { echo "$1" | jq '.' > "$STATE_FILE" 2>/dev/null || echo "$1" > "$STATE_FILE"; }
load_state() { cat "$STATE_FILE"; }

update_task() {
  local task_id="$1" status="$2" output="${3:-}"
  local state
  state=$(load_state)
  state=$(echo "$state" | jq --arg id "$task_id" --arg s "$status" --arg o "$output" \
    '.tasks |= map(if .id == $id then .status = $s | .output = $o else . end)')
  save_state "$state"
}

# ════════════════════════════════════════════════════════════
#  PHASES
# ════════════════════════════════════════════════════════════

brainstorm() {
  local task="$1"
  log "BRAINSTORM" "Starting analysis"

  local prompt
  prompt="${DIR_PROMPT}

TASK: ${task}

Phase: brainstorm. Analyze task, identify risks, propose strategy.
Do not decompose yet.
Output JSON: {\"phase\":\"brainstorm\",\"analysis\":\"\",\"risks\":[],\"strategy\":\"\"}"

  local analysis
  analysis=$(call_director "$prompt")

  echo ""
  echo "================================================================"
  echo "  DIRECTOR ANALYSIS (GPT-5.6 Terra)"
  echo "================================================================"
  echo "$analysis"
  echo "================================================================"
  echo ""
  echo "Brainstorming: add observations or refine the plan"
  echo "(type and press Enter, or 'skip' to proceed)"
  read -r user_input

  local final
  if [[ "$user_input" != "skip" && -n "$user_input" ]]; then
    local refine_prompt
    refine_prompt="${DIR_PROMPT}

TASK: ${task}
Previous analysis: ${analysis}
USER FEEDBACK: ${user_input}

Incorporate feedback. Update strategy.
Output JSON: {\"phase\":\"brainstorm\",\"analysis\":\"\",\"risks\":[],\"strategy\":\"\",\"user_feedback\":\"\"}"

    final=$(call_director "$refine_prompt")
    echo ""
    echo "================================================================"
    echo "  REFINED PLAN"
    echo "================================================================"
    echo "$final"
    echo "================================================================"
  else
    final="$analysis"
  fi

  save_state "$(load_state | jq --arg b "$final" --arg p "decompose" '.brainstorm = $b | .phase = $p')"

  echo ""
  read -p "Proceed with decomposition? (y/n) " confirm
  [[ "$confirm" == "y" ]] || { log "ABORT" "User aborted"; exit 0; }
  echo "$final"
}

decompose() {
  local task="$1" brainstorm_result="$2"
  log "DIRECTOR" "Decomposing task into subtasks"

  local prompt
  prompt="${DIR_PROMPT}

TASK: ${task}
BRAINSTORM CONTEXT: ${brainstorm_result}

Decompose into subtasks. Rules:
- Most tasks must go to agy (main executor, 90% of work)
- deepseek-flash only for quick fixes and tests
- claude-glm and deepseek-pro only when Sol identifies a concrete risk in their respective areas
- For each task generate the exact prompt the agent will execute

Output: {\"phase\":\"decompose\",\"tasks\":[{\"id\":\"T1\",\"d\":\"desc\",\"a\":\"agy\",\"p\":\"prompt\",\"r\":true,\"pr\":\"high\"}]}"

  local result
  result=$(call_director "$prompt")

  local tasks_json
  tasks_json=$(extract_json "$result")
  local tasks
  tasks=$(echo "$tasks_json" | jq '.tasks // empty' 2>/dev/null)

  if [[ -z "$tasks" || "$tasks" == "null" ]]; then
    log "ERROR" "Decomposition failed. Director output:"
    echo "$result"
    return 1
  fi

  tasks=$(echo "$tasks" | jq 'map(. + {status: "pending", output: null})')
  save_state "$(load_state | jq --argjson t "$tasks" --arg p "execute" '.tasks = $t | .phase = $p')"

  local count
  count=$(echo "$tasks" | jq 'length')
  log "DIRECTOR" "${count} tasks decomposed"

  echo ""
  echo "================================================================"
  echo "  DECOMPOSED TASKS"
  echo "================================================================"
  echo "$tasks" | jq -r '.[] | "  \(.id) [\(.a)] \(.pr): \(.d)"'
  echo "================================================================"
  echo ""
  read -p "Execute all tasks? (y/n) " confirm
  [[ "$confirm" == "y" ]] || { log "ABORT" "User aborted"; exit 0; }
}

execute_all() {
  local state
  state=$(load_state)
  local task_count
  task_count=$(echo "$state" | jq '.tasks | length')

  for i in $(seq 0 $((task_count - 1))); do
    local task_id agent prompt desc review priority
    task_id=$(echo "$state" | jq -r ".tasks[$i].id")
    agent=$(echo "$state" | jq -r ".tasks[$i].a")
    prompt=$(echo "$state" | jq -r ".tasks[$i].p")
    desc=$(echo "$state" | jq -r ".tasks[$i].d")
    review=$(echo "$state" | jq -r ".tasks[$i].r // false")
    priority=$(echo "$state" | jq -r ".tasks[$i].pr // \"med\"")

    log "EXECUTE" "Task ${task_id} (${agent}/${priority}): ${desc}"
    update_task "$task_id" "executing"

    local output
    case "$agent" in
      agy)
        output=$(call_executor "$prompt")
        ;;
      deepseek-flash)
        output=$(call_fixer "$prompt")
        ;;
      deepseek-pro)
        output=$(call_reviewer_ds "$prompt")
        ;;
      claude-glm)
        output=$(call_reviewer_claude "$prompt")
        ;;
      *)
        log "ERROR" "Unknown agent: $agent"
        update_task "$task_id" "failed"
        continue
        ;;
    esac

    echo "$output" > "$LOG_DIR/${task_id}_output.txt"
    update_task "$task_id" "executed" "$output"

    # Director quick-check loop (Max 3 attempts)
    local check_passed=false
    local attempt=1
    local max_attempts=3
    while [[ $attempt -le $max_attempts && "$check_passed" == "false" ]]; do
      log "DIRECTOR" "Quick check task ${task_id} (attempt ${attempt}/${max_attempts})"
      local check_prompt
      check_prompt="${DIR_PROMPT}

Quick check: was task \"${desc}\" completed correctly?
Agent output (first 1000 chars): ${output:0:1000}

Respond: {\"ok\":true,\"notes\":\"\",\"escalate\":false}"

      local check_result
      check_result=$(call_director "$check_prompt")
      local check_json
      check_json=$(extract_json "$check_result")
      local ok
      ok=$(echo "$check_json" | jq -r '.ok // false' 2>/dev/null || echo "false")

      if [[ "$ok" == "true" ]]; then
        check_passed=true
        log "DIRECTOR" "Task ${task_id} passed check on attempt ${attempt}"
        update_task "$task_id" "checked_ok" "$output"
      else
        local notes
        notes=$(echo "$check_json" | jq -r '.notes // "Fix needed"' 2>/dev/null)
        log "DIRECTOR" "Task ${task_id} failed check (attempt ${attempt}): ${notes}"
        if [[ $attempt -lt $max_attempts ]]; then
          log "FIX" "DeepSeek Flash intervening on ${task_id}"
          local fix_output
          fix_output=$(call_fixer "Task: ${desc}
Problem detected: ${notes}
Previous output: ${output:0:1500}
Fix in project and verify.")
          echo "$fix_output" > "$LOG_DIR/${task_id}_fix_attempt_${attempt}.txt"
          output="$fix_output"
          update_task "$task_id" "fixing" "$output"
        else
          log "WARN" "Task ${task_id} failed check after ${max_attempts} attempts."
          update_task "$task_id" "check_failed" "$output"
        fi
      fi
      attempt=$((attempt + 1))
    done

    # Double review for important/critical tasks
    if [[ "$review" == "true" ]]; then
      review_task "$task_id" "$desc" "$prompt" "$output"
    fi

    state=$(load_state)
  done
}

review_task() {
  local task_id="$1" desc="$2" original_prompt="$3" output="$4"
  log "REVIEW" "Dual review for critical task: ${task_id}"

  echo ""
  echo "================================================================"
  echo "  DUAL REVIEW: ${task_id} - ${desc}"
  echo "================================================================"

  local rev_attempt=1
  local max_rev_attempts=3
  local approved=false

  while [[ $rev_attempt -le $max_rev_attempts && "$approved" == "false" ]]; do
    # Reviewer 1: Claude Code + GLM
    log "CLAUDE-GLM" "Review in progress (attempt ${rev_attempt}/${max_rev_attempts})..."
    local claude_review
    claude_review=$(call_reviewer_claude "Review this work.
Task: ${desc}
Original prompt: ${original_prompt}
Output: ${output:0:2000}

Verify: correctness, completeness, security, best practices.
Output JSON: {\"verdict\":\"pass|fix\",\"issues\":[],\"fix_prompt\":\"\"}")
    local claude_verdict
    claude_verdict=$(extract_json "$claude_review" | jq -r '.verdict // "pass"' 2>/dev/null || echo "pass")
    echo "  [Claude+GLM]: verdict=${claude_verdict}"

    # Reviewer 2: DeepSeek V4 Pro
    log "DS-PRO" "Review in progress (attempt ${rev_attempt}/${max_rev_attempts})..."
    local ds_review
    ds_review=$(call_reviewer_ds "Review this work.
Task: ${desc}
Output: ${output:0:2000}

Deep technical analysis: edge cases, performance, security, architecture.
Output JSON: {\"verdict\":\"pass|fix\",\"issues\":[],\"fix_prompt\":\"\"}")
    local ds_verdict
    ds_verdict=$(extract_json "$ds_review" | jq -r '.verdict // "pass"' 2>/dev/null || echo "pass")
    echo "  [DeepSeek Pro]: verdict=${ds_verdict}"

    # Fix if at least one reviewer finds issues
    if [[ "$claude_verdict" == "fix" || "$ds_verdict" == "fix" ]]; then
      if [[ $rev_attempt -lt $max_rev_attempts ]]; then
        log "FIX" "Issues found by reviewers. DeepSeek Flash fixing..."
        local claude_issues ds_issues claude_fix ds_fix
        claude_issues=$(extract_json "$claude_review" | jq -r '.issues[]? // empty' 2>/dev/null)
        ds_issues=$(extract_json "$ds_review" | jq -r '.issues[]? // empty' 2>/dev/null)
        claude_fix=$(extract_json "$claude_review" | jq -r '.fix_prompt // ""' 2>/dev/null)
        ds_fix=$(extract_json "$ds_review" | jq -r '.fix_prompt // ""' 2>/dev/null)

        local fix_output
        fix_output=$(call_fixer "Task: ${desc}
Resolve these issues in project:
Claude+GLM Issues: ${claude_issues}
DeepSeek Pro Issues: ${ds_issues}
Claude Suggested Fix: ${claude_fix}
DS Suggested Fix: ${ds_fix}")
        echo "$fix_output" > "$LOG_DIR/${task_id}_review_fix_${rev_attempt}.txt"
        output="$fix_output"
        update_task "$task_id" "reviewed_fixing" "$fix_output"
      else
        log "WARN" "${task_id} still has findings after ${max_rev_attempts} review cycles."
        update_task "$task_id" "review_warning" "$output"
      fi
    else
      approved=true
      update_task "$task_id" "reviewed_ok" "$output"
      log "REVIEW" "${task_id} approved by both reviewers"
    fi
    rev_attempt=$((rev_attempt + 1))
  done
  echo "================================================================"
}

validate() {
  log "DIRECTOR" "Final validation"

  local state
  state=$(load_state)
  local tasks_summary
  tasks_summary=$(echo "$state" | jq -r '.tasks[] | "\(.id): \(.status)"')

  local prompt
  prompt="${DIR_PROMPT}

Task status:
${tasks_summary}

Verify whether all tasks are complete. If issues exist, indicate which to retry.
Output: {\"phase\":\"validate\",\"status\":\"done|retry\",\"retry\":[],\"notes\":\"\"}"

  local result
  result=$(call_director "$prompt")

  local result_json
  result_json=$(extract_json "$result")
  local status
  status=$(echo "$result_json" | jq -r '.status // "done"' 2>/dev/null || echo "done")

  echo ""
  echo "================================================================"
  echo "  FINAL VALIDATION"
  echo "================================================================"
  echo "Status: ${status}"
  echo "$result_json" | jq -r '.notes // ""' 2>/dev/null
  echo "================================================================"

  if [[ "$status" == "done" ]]; then
    log "DIRECTOR" "All tasks completed!"
    return 0
  else
    local retry_tasks
    retry_tasks=$(echo "$result_json" | jq -r '.retry[]? // empty' 2>/dev/null)
    log "DIRECTOR" "Retry needed for: ${retry_tasks}"
    return 1
  fi
}

show_token_summary() {
  echo ""
  echo "================================================================"
  echo "  TOKEN USAGE SUMMARY"
  echo "================================================================"
  if [[ -f "$TOTAL_TOKENS_FILE" ]]; then
    jq '.' "$TOTAL_TOKENS_FILE" 2>/dev/null || cat "$TOTAL_TOKENS_FILE"
  fi
  echo "================================================================"
}

# ════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════

usage() {
  echo "Usage: $0 [options] \"task description\""
  echo ""
  echo "Options:"
  echo "  --dry-run    Only brainstorm + decompose, no execution"
  echo "  --health     Check prerequisites"
  echo "  --help       Show this help"
  echo ""
  echo "Environment:"
  echo "  PROJECT_DIR       Project directory (default: pwd)"
  echo "  DEEPSEEK_API_KEY  DeepSeek API Key"
  echo "  CLAUDE_GLM_TOKEN  Z.ai token for GLM 5.3"
  echo "  OPENAI_API_KEY    OpenAI API Key (or use OAuth)"
  echo "  AGY_MODEL         agy model (default: auto)"
  echo "  AGY_TIMEOUT       agy timeout (default: 15m)"
}

main() {
  local dry_run=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=true; shift ;;
      --health) init; health_check; exit 0 ;;
      --help|-h) usage; exit 0 ;;
      *) break ;;
    esac
  done

  local task="${1:-}"

  echo "=============================================================="
  echo "  Taktstock Multi-Agent Orchestrator (Headless Mode)"
  echo "  Director:  Sol (strategic routing)"
  echo "  Executor:  agy (90% of work)"
  echo "  Reviewers: Claude+GLM 5.3 | DeepSeek V4 Pro"
  echo "  Fixer:     DeepSeek V4 Flash"
  echo "=============================================================="
  echo ""

  init

  if [[ -z "$task" ]]; then
    echo "Describe the task to automate:"
    read -r task
  fi
  [[ -z "$task" ]] && { echo "No task provided. Exiting."; exit 1; }

  log "START" "Task: ${task}"
  save_state "$(jq -n --arg t "$task" '{phase:"init",task:$t,tasks:[],brainstorm:""}')"

  # Phase 1: Brainstorm
  local brainstorm_result
  brainstorm_result=$(brainstorm "$task")

  # Phase 2: Decompose
  decompose "$task" "$brainstorm_result"

  if $dry_run; then
    log "DRY-RUN" "Only decomposition, no execution"
    echo ""
    echo "Dry-run completed. State saved in $STATE_FILE"
    exit 0
  fi

  # Phase 3: Execute
  execute_all

  # Phase 4: Validate
  if validate; then
    log "DONE" "Orchestration completed!"
    show_token_summary
    echo ""
    echo "Full log: $LOG_FILE"
  else
    log "RETRY" "Some tasks require attention"
    show_token_summary
    echo ""
    echo "Check logs: $LOG_FILE"
    echo "State: $STATE_FILE"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
