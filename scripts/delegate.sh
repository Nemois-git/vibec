#!/usr/bin/env bash
# Copyright (c) 2026 Jeongcheol Kim
# Licensed under the MIT License; see LICENSE at the repository root.
# SPDX-License-Identifier: MIT
# Delegates one task to Mistral Vibe in headless programmatic mode (vibe -p).
#
# Config precedence: CLI flags > environment variables > .claude/delegate.env > built-in defaults.
# Each run appends one index line to <log>/runs.log and writes <log>/runs/<ts>/ containing
# stdout.txt, stderr.txt and export.json (vibe --output-dir: usage, cost, stop reason, warnings).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CONFIG_FILE="$PROJECT_DIR/.claude/delegate.env"

MODEL="" AGENT="" RESUME="" RUN_ID="" ALIAS="" MAX_TURNS="" MAX_PRICE="" MAX_TOKENS="" TIME_LIMIT="" OUTPUT="" LOG_DIR="" TASK_FILE="" DOCTOR=0
TASK=""

usage() {
  cat <<'EOF'
Usage: delegate.sh [options] -- "<task prompt>"
       delegate.sh [options] --task-file PATH
       delegate.sh --doctor

Options:
  --model ALIAS     Vibe model alias (--model); empty = vibe default routing
  --agent NAME      Vibe agent profile (--agent), e.g. from ~/.vibe/agents/
  --resume ID       Resume existing Vibe session ID (--resume ID); the task is
                    appended to that session as a follow-up message. The session
                    keeps its pinned model/agent; --model is ignored on resume.
  --run-id TS       Fix the run directory name (used by scripts/delegate_mcp.py for
                    background runs so the caller knows run_dir up front).
  --alias NAME      Human-friendly name for this run, recorded in started.json /
                    meta.json and in the MCP server's aliases.json registry, so
                    delegate_wait / delegate_interrupt / delegate_status can address the run
                    by NAME instead of the timestamp.
  --max-turns N     Max assistant turns (--max-turns)
  --max-price D     Max cumulative cost in USD (--max-price)
  --max-tokens N    Max cumulative tokens (--max-tokens)
  --time-limit S    Interrupt the run after S seconds (--time-limit)
  --output FMT      vibe -p output: text (default) | json | streaming
  --log-dir DIR     Run log dir (absolute, or relative to the project root)
  --config FILE     Config file (default: .claude/delegate.env)
  --doctor          Check the integration without running a task
  -h, --help        This help

Config keys (file .claude/delegate.env or environment):
  DELEGATE_MODEL, DELEGATE_AGENT,
  DELEGATE_MAX_TURNS, DELEGATE_MAX_PRICE, DELEGATE_MAX_TOKENS,
  DELEGATE_TIME_LIMIT, DELEGATE_OUTPUT, DELEGATE_LOG_DIR
  DELEGATE_META_FILE (internal: JSON run metadata path used by scripts/delegate_mcp.py)
EOF
}

# Prints the value of the first KEY=VALUE line in FILE; never evaluates the file.
cfg_get() {
  local file="$1" key="$2" line k v
  [[ -f "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    k="${line%%=*}"; v="${line#*=}"
    k="${k//[[:space:]]/}"
    if [[ "$k" == "$key" ]]; then printf '%s' "$v"; return 0; fi
  done < "$file"
  return 1
}

doctor() {
  local fails=0
  check() {
    printf '%s\t%s\t%s\n' "$1" "$2" "$3"
    [[ "$1" == "FAIL" ]] && fails=$((fails + 1))
    return 0
  }
  if command -v vibe >/dev/null 2>&1; then
    check PASS "vibe-cli" "$(vibe --version 2>&1)"
  else
    check FAIL "vibe-cli" "vibe not on PATH (install: uv tool install mistral-vibe)"
  fi
  if command -v claude >/dev/null 2>&1; then
    check PASS "claude-code" "$(claude --version 2>&1 | head -1)"
  else
    check WARN "claude-code" "claude not on PATH here; .claude/ still loads in any Claude Code session on this repo"
  fi
  if [[ -f "$CONFIG_FILE" ]]; then
    local n
    n="$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$CONFIG_FILE" || true)"
    check PASS "config" "$CONFIG_FILE ($n keys)"
  else
    check WARN "config" "$CONFIG_FILE missing; built-in defaults apply"
  fi
  if mkdir -p "$LOG_DIR_ABS/runs" 2>/dev/null && [[ -w "$LOG_DIR_ABS" ]]; then
    check PASS "log-dir" "$LOG_DIR_ABS writable"
  else
    check FAIL "log-dir" "$LOG_DIR_ABS not writable"
  fi
  if [[ -f "$PROJECT_DIR/.mcp.json" && -f "$SCRIPT_DIR/delegate_mcp.py" ]]; then
    check PASS "mcp" ".mcp.json + scripts/delegate_mcp.py present (tools: mcp__delegate__delegate_run, mcp__delegate__delegate_status, mcp__delegate__delegate_doctor)"
  else
    check FAIL "mcp" ".mcp.json or scripts/delegate_mcp.py missing"
  fi
  if command -v jq >/dev/null 2>&1; then
    check PASS "jq" "available for export.json pretty-printing"
  else
    check WARN "jq" "not found; exports are read as raw json"
  fi
  echo "doctor: $fails failure(s)"
  [[ "$fails" -eq 0 ]]
}

run_task() {
  local ts run_dir out err start dur rc preview alias_json args=()
  ts="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
  run_dir="$LOG_DIR_ABS/runs/$ts"
  mkdir -p "$run_dir"
  out="$run_dir/stdout.txt"; err="$run_dir/stderr.txt"
  preview="$(printf '%s' "$TASK$TASK_FILE" | tr '\t\n\r"\\' '    ' | cut -c1-160)"
  alias_json="$(printf '%s' "$ALIAS" | tr -d '"\\')"
  printf '{"run":"%s","alias":"%s","wrapper_pid":%s,"started_at":"%s","mode":"%s","model":"%s","agent":"%s","resume":"%s","task_preview":"%s"}\n' \
    "$ts" "$alias_json" "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" smart "${MODEL:-}" "${AGENT:-}" "${RESUME:-}" "$preview" \
    > "$run_dir/started.json"

  if [[ -n "$TASK_FILE" ]]; then args+=(--prompt-file "$TASK_FILE"); else args+=(-p "$TASK"); fi
  args+=(--workdir "$PROJECT_DIR" --trust --output "$OUTPUT" --output-dir "$run_dir")
  args+=(--smart-approve)
  [[ -n "$MODEL" ]] && args+=(--model "$MODEL")
  [[ -n "$AGENT" ]] && args+=(--agent "$AGENT")
  [[ -n "$RESUME" ]] && args+=(--resume "$RESUME")
  [[ -n "$MAX_TURNS" ]] && args+=(--max-turns "$MAX_TURNS")
  [[ -n "$MAX_PRICE" ]] && args+=(--max-price "$MAX_PRICE")
  [[ -n "$MAX_TOKENS" ]] && args+=(--max-tokens "$MAX_TOKENS")
  [[ -n "$TIME_LIMIT" ]] && args+=(--time-limit "$TIME_LIMIT")

  start="$(date +%s)"
  vibe "${args[@]}" >"$out" 2>"$err" &
  vibe_pid=$!
  trap 'kill -TERM "$vibe_pid" 2>/dev/null' TERM
  set +e
  wait "$vibe_pid"
  rc=$?
  set -e
  trap - TERM
  dur=$(( $(date +%s) - start ))

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$ts" "$rc" "$dur" smart "${MODEL:-default}" "${AGENT:--}" "$MAX_TURNS" "$MAX_PRICE" "$preview" \
    >> "$LOG_DIR_ABS/runs.log"

  if [[ -n "${DELEGATE_META_FILE:-}" ]]; then
    printf '{"run":"%s","alias":"%s","rc":%s,"duration_s":%s,"run_dir":"%s","mode":"%s","model":"%s","agent":"%s"}\n' \
      "$ts" "$alias_json" "$rc" "$dur" "$run_dir" smart "${MODEL:-}" "${AGENT:-}" > "$DELEGATE_META_FILE"
  fi

  cat "$out"
  echo "[delegate] rc=$rc duration=${dur}s mode=smart model=${MODEL:-default} run=$ts logs=$run_dir" >&2
  exit "$rc"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode|--mode=*)
      echo "delegate: --mode was removed; delegated runs always use --smart-approve" >&2
      exit 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    --resume) RESUME="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --alias) ALIAS="$2"; shift 2 ;;
    --max-turns) MAX_TURNS="$2"; shift 2 ;;
    --max-price) MAX_PRICE="$2"; shift 2 ;;
    --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
    --time-limit) TIME_LIMIT="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --task-file) TASK_FILE="$2"; shift 2 ;;
    --doctor) DOCTOR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
TASK="$*"

if [[ -z "$MODEL" ]]; then MODEL="${DELEGATE_MODEL:-$(cfg_get "$CONFIG_FILE" DELEGATE_MODEL || true)}"; fi
if [[ -z "$AGENT" ]]; then AGENT="${DELEGATE_AGENT:-$(cfg_get "$CONFIG_FILE" DELEGATE_AGENT || true)}"; fi
if [[ -z "$MAX_TURNS" ]]; then MAX_TURNS="${DELEGATE_MAX_TURNS:-$(cfg_get "$CONFIG_FILE" DELEGATE_MAX_TURNS || true)}"; fi
if [[ -z "$MAX_PRICE" ]]; then MAX_PRICE="${DELEGATE_MAX_PRICE:-$(cfg_get "$CONFIG_FILE" DELEGATE_MAX_PRICE || true)}"; fi
if [[ -z "$MAX_TOKENS" ]]; then MAX_TOKENS="${DELEGATE_MAX_TOKENS:-$(cfg_get "$CONFIG_FILE" DELEGATE_MAX_TOKENS || true)}"; fi
if [[ -z "$TIME_LIMIT" ]]; then TIME_LIMIT="${DELEGATE_TIME_LIMIT:-$(cfg_get "$CONFIG_FILE" DELEGATE_TIME_LIMIT || true)}"; fi
if [[ -z "$OUTPUT" ]]; then OUTPUT="${DELEGATE_OUTPUT:-$(cfg_get "$CONFIG_FILE" DELEGATE_OUTPUT || true)}"; fi
if [[ -z "$LOG_DIR" ]]; then LOG_DIR="${DELEGATE_LOG_DIR:-$(cfg_get "$CONFIG_FILE" DELEGATE_LOG_DIR || true)}"; fi

: "${OUTPUT:=text}"
: "${MAX_TURNS:=200}"
: "${MAX_PRICE:=10}"
: "${TIME_LIMIT:=3600}"
: "${LOG_DIR:=.delegate}"

case "$LOG_DIR" in
  /*) LOG_DIR_ABS="$LOG_DIR" ;;
  *)  LOG_DIR_ABS="$PROJECT_DIR/$LOG_DIR" ;;
esac

if [[ "$DOCTOR" -eq 1 ]]; then
  doctor
  exit $?
fi

if [[ -z "$TASK" && -z "$TASK_FILE" ]]; then
  usage >&2
  exit 2
fi

run_task
