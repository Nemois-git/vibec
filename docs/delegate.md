# delegate -- detailed reference

Full reference for the Claude Code -> Mistral Vibe delegation packaged in this repo. Overview: [README.md](../README.md).

## Why MCP and not a subagent

A Claude Code custom subagent always runs on the session's model (`model: inherit` in its frontmatter), so a "vibe" subagent would execute on Sonnet, not Mistral. An MCP tool avoids that: the Claude side only performs a deterministic tool call, and the actual work runs in a `vibe -p` subprocess on whatever model Vibe is configured with. Hence this repo registers `vibe` as an MCP server (`.mcp.json`), not as `.claude/agents/*`.

## Components

| File | Role |
|---|---|
| `.mcp.json` | Registers the `delegate` MCP server: `python3 scripts/delegate_mcp.py` (relative to the project root; `${CLAUDE_PROJECT_DIR}` is not expanded for project servers, which caused a connection failure). Loaded automatically by Claude Code (CLI, Desktop, VS Code) when the repo is open. |
| `scripts/delegate_mcp.py` | MCP stdio server (Python stdlib only). Exposes `delegate_run`, `delegate_status`, `delegate_doctor`. Newline-delimited JSON-RPC 2.0 over stdio. |
| `scripts/delegate.sh` | The single execution engine for both the MCP server and direct shell use: config resolution, `vibe -p` invocation, run logging, `--doctor`. |
| `.claude/commands/delegate.md` | `/delegate [flags] <task>` slash command; expands to instructions that call `mcp__delegate__delegate_run`. |
| `.claude/delegate.env` | Shared team defaults (see below). |
| `.claude/settings.json` | `enableAllProjectMcpServers: true` plus pre-approved `mcp__delegate__*` tool permissions. |
| `install.sh` | Optional user-level install into `~/.claude` + the `claude mcp add` command. Overwrites existing files by default, so re-running it after `git pull` updates the installed copy; `--skip-existing` keeps existing files. A previous `vibe` install is migrated automatically: its files move to `~/.claude/migrated/` and its user-scope server is replaced by `delegate`. |

## MCP server internals (`scripts/delegate_mcp.py`)

- **Transport**: stdio, one JSON-RPC 2.0 message per line. Handles `initialize` (echoes the client's `protocolVersion`, advertises `capabilities.tools`), `ping`, `tools/list`, `tools/call`, ignores `notifications/*`, and answers unknown methods with `-32601` when they carry an id.
- **Path resolution**: `PROJECT_DIR` = `$CLAUDE_PROJECT_DIR` (set by Claude Code); otherwise the current directory if it contains `scripts/delegate.sh`; otherwise the parent of the server's directory; otherwise the server's directory. The wrapper is resolved from `$DELEGATE_WRAPPER`, else `<project>/scripts/delegate.sh`, else next to the server script (user-level install layout).
- **Execution**: `delegate_run` runs `bash scripts/delegate.sh -- "<task>"` as a subprocess with `DELEGATE_META_FILE` pointing at a temp file. The wrapper writes one JSON line there (`run`, `rc`, `duration_s`, `run_dir`, `mode`, `model`, `agent`), which is how the server learns the run dir deterministically. The MCP-side subprocess timeout is `time_limit + 120s`.
- **Result**: the tool result's `structuredContent` merges the wrapper meta with the run's `export.json`: `answer`, `exit_code`, `duration_s`, `run`, `run_dir`, `mode`, `model`, `session_id`, `cost_usd`, `usage`, `outcome`, `stop_reason`, `vibe_version`, `stderr_tail`. `isError` is set when the run's exit code is non-zero.

### Tool schemas

`delegate_run` (required: `task`): optional `background` (bool -- start detached and return immediately with run/run_dir/pid), `alias` (caller-chosen name registered in `.delegate/aliases.json`, so later tools can address the run by name), `session_id` (resume an existing session; the task is appended via `--resume`), `model`, `agent`, `max_turns`, `max_price`, `max_tokens`, `time_limit`, `output` (text|json|streaming). Missing options fall back to env > `.claude/delegate.env` > defaults, exactly like the shell wrapper. `mode` is gone: a value other than `smart` is refused, because every run passes `--smart-approve`.

`delegate_wait` (required: `run` or `alias`, optional `timeout_s` default 600): polls `runs/<run>/meta.json` until the background run finishes, then returns the same structured result as a synchronous `delegate_run` (answer from `stdout.txt`, plus `export.json` fields). Returns `status: "running"` on timeout.

`delegate_status` (optional: `count`, default 10): currently running runs (from `runs/*/started.json` without `meta.json`) plus the last `count` finished runs (from `runs.log`), the newest finished run's `export.json` summary with a `delegate_run(session_id=...)` hint, and the alias registry.

`delegate_interrupt` (required: `run` or `alias`, optional `wait_s` default 30): resolves the run, then finds the wrapper (`--run-id <run>`) and vibe (`--output-dir .../runs/<run>`) processes via the alias registry PID plus `pgrep -f` fallback, and sends SIGTERM, so vibe writes `export.json` and exits 3. Signals PIDs directly (never the process group) so a synchronous run can never take down the MCP server.

`delegate_doctor`: runs `scripts/delegate.sh --doctor` and returns its `PASS`/`WARN`/`FAIL` lines; `isError` when any check fails, so it works as a CI gate.

### Background (detached) execution

In background mode the server generates the run timestamp itself, passes `--run-id <ts>` to the wrapper, pre-creates `runs/<ts>/`, points `DELEGATE_META_FILE` at `runs/<ts>/meta.json`, and starts the wrapper with `subprocess.Popen(..., start_new_session=True, stdin=DEVNULL)` with stdout/stderr redirected to `runs/<ts>/server.log`. The wrapper writes `started.json` at launch (pid, started_at, mode, task preview) and `meta.json` at completion, so `delegate_status` can distinguish running from finished runs even across MCP server restarts.

Runs are addressable three ways: the returned `pid`, the run timestamp (`runs/<ts>`), or a caller-chosen `alias` stored in `.delegate/aliases.json` (alias -> run/run_dir/pid/status/session_id/rc). `delegate_wait`/`delegate_interrupt` accept either form; PID lookup uses the registry first (immune to the fork/exec race right after spawn) and falls back to `pgrep -f` on the run-specific cmdline. The server reaps dead wrapper children (`waitpid WNOHANG`) so a finished run is never mistaken for a live one.

### Direct CLI (no MCP)

The installed server file is both the MCP stdio server and a standalone CLI with the same commands; each prints the same JSON result object as the MCP tool. MCP calls block until they return -- the reading side (wait = wake-on-finish, status = monitor) is what you use as CLI outside MCP. If you attach an external monitor or async watcher (harness tooling that polls runs), drive it through the CLI -- MCP calls block, so they are not suitable for watch loops:

```bash
# user-level install (install.sh):
PY="$HOME/.claude/mcp/delegate_mcp.py"
# project scope, from the repo root:
# PY="$PWD/scripts/delegate_mcp.py"
python3 "$PY" wait RUN_OR_ALIAS [--timeout S]     # wake-on-finish: blocks until .delegate/runs/<run>/meta.json appears
python3 "$PY" status [--count N]                  # monitor: running + finished runs + aliases
# the file also accepts delegate/interrupt/doctor with the same arguments as the MCP tools
```

Run with the target project as cwd (or `CLAUDE_PROJECT_DIR` set). The MCP `initialize` result also carries these instructions in its `instructions` field.

## Wrapper internals (`scripts/delegate.sh`)

```
delegate.sh [options] -- "<task prompt>"
delegate.sh [options] --task-file PATH
delegate.sh --doctor
```

| Flag | Maps to | Notes |
|---|---|---|
| `--model ALIAS` | `--model ALIAS` | Alias from the user's `~/.vibe/config.toml` `[[models]]`. |
| `--agent NAME` | `--agent NAME` | Agent profile, e.g. a bounded profile in `~/.vibe/agents/NAME.toml`. |
| `--max-turns N` | `--max-turns N` | Assistant-turn budget (root + subagents). |
| `--max-price D` | `--max-price D` | Cumulative USD cap. `0` stops the run once anything has cost money. |
| `--max-tokens N` | `--max-tokens N` | Cumulative token cap. |
| `--time-limit S` | `--time-limit S` | Wall-clock interrupt; vibe exits with code 3 and still writes the export. |
| `--output text\|json\|streaming` | `--output FMT` | `text` = final answer on stdout. |
| `--log-dir DIR` | -- | Absolute, or relative to the project root. |
| `--config FILE` | -- | Alternate config file. |
| `--task-file PATH` | `--prompt-file PATH` | Reads the task from a file (for long prompts). |
| `--run-id TS` | -- | Fixes the run directory name (used by the MCP server for background runs, so the caller knows `run_dir` up front). |
| `--alias NAME` | -- | Records the name in `started.json`/`meta.json` and the MCP alias registry, so `delegate_wait`/`delegate_interrupt`/`delegate_status` can use NAME instead of the timestamp. |
| `--resume ID` | `--resume ID` | Resumes an existing session for a small follow-up on that same run; the task is appended as a message. New tasks need a new delegation without `session_id`. The session keeps its pinned model/agent. |
| `--doctor` | -- | Integration check, no task run. |

The wrapper always adds: `-p "<task>"` (or `--prompt-file`), `--workdir "$PROJECT_DIR"`, `--trust`, `--output "$OUTPUT"`, `--output-dir "$RUN_DIR"`.

### Configuration resolution

Precedence: **tool args / CLI flags > environment variables > `.claude/delegate.env` > built-in default.**

The config file is parsed as plain `KEY=VALUE` lines (`#` comments, CRLF tolerated); it is never sourced or evaluated. Recognized keys: `DELEGATE_MODEL`, `DELEGATE_AGENT`, `DELEGATE_MAX_TURNS`, `DELEGATE_MAX_PRICE`, `DELEGATE_MAX_TOKENS`, `DELEGATE_TIME_LIMIT`, `DELEGATE_OUTPUT`, `DELEGATE_LOG_DIR`. Each has the same-named environment variable as its override. Built-in defaults: empty model/agent, `200` turns, `10` USD, `3600` s, `text`, `.delegate`. Approval is not configurable: every run passes `--smart-approve`.

Per-user overrides without touching the shared file:

```bash
DELEGATE_MODEL=my-fast-model DELEGATE_MAX_PRICE=2 bash scripts/delegate.sh -- "..."
```

## Run logs and monitoring schema

Each run appends one TSV line to `$LOG_DIR/runs.log`:

```
20260101-123045  0  42  smart  default  -  200  10  Add input validation to ...
|                |  |  |      |         |  |    |  +-- task preview (first 160 chars, tabs/newlines flattened)
|                |  |  |      |         |  |    +-- max_price
|                |  |  |      |         |  +-- max_turns
|                |  |  |      |         +-- agent ("-" when unset)
|                |  |  |      +-- model ("default" when unset)
|                |  |  +-- approve mode (always smart)
|                |  +-- duration_s
|                +-- exit code
+-- timestamp
```

and creates `$LOG_DIR/runs/<timestamp>/` with `stdout.txt`, `stderr.txt`, `export.json` (keys: `vibe_version`, `usage`, `cost_usd`, `stop_reason`, `outcome`, `exit_code`, `session_id`, `limits`, `config`, `steps`, `journal_dir`, `started_at`, `ended_at`, `error`, `schema_version`), and `session/` (journal copy).

`--doctor` output: one `PASS|WARN|FAIL <name> <detail>` line per check -- `vibe-cli` (PATH + `vibe --version`), `claude-code`, `mcp` (`.mcp.json` + `scripts/delegate_mcp.py` present), `config` (file + key count), `log-dir` (writable), `jq`; then `doctor: N failure(s)`.

## Extension points

- **Bounded Vibe profiles**: create an agent profile in `~/.vibe/agents/NAME.toml` with `[tools.bash] allowlist = [...]` and pass it via `agent` / `DELEGATE_AGENT` / `--agent`. This is the only way to widen what a delegated run may do: every run passes `--smart-approve`, which enforces allowlists instead of bypassing them.
- **Custom system prompt for delegated runs**: `vibe -p` supports `--system-prompt-file PATH`; add it to the wrapper's `args` if your team standardizes one.
- **Tool narrowing**: `--enabled-tools` / `--disabled-tools` (repeatable) restrict what the delegated run may use.
- **Isolated risky runs**: `vibe --worktree` runs the task in a separate git worktree under `$VIBE_HOME/worktrees`; the wrapper does not use it by default.
- **Model aliases**: define them per user in `~/.vibe/config.toml` (`[[models]]` with `alias = "..."`), reference via `model` / `DELEGATE_MODEL`. The repo intentionally ships no `[[providers]]` so it never overrides a user's auth setup.
- **User-level MCP install**: `install.sh` copies `delegate_mcp.py` + `delegate.sh` to `~/.claude/mcp/`; register with `claude mcp add delegate --scope user -- python3 "$HOME/.claude/mcp/delegate_mcp.py"`. The server then uses `$CLAUDE_PROJECT_DIR` as the project and the copied wrapper.

## Examples

```bash
# MCP handshake smoke test (no API call)
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 scripts/delegate_mcp.py

# 1. Smoke test through the wrapper (no tool calls, price cap)
bash scripts/delegate.sh --max-price 0.5 -- "Reply with exactly: OK"

# 2. Delegate a refactor through the wrapper directly
bash scripts/delegate.sh --max-turns 100 --max-price 5 -- \
  "Refactor scripts/ to use a shared parse_args helper. Acceptance: bash -n passes on all scripts. Do not touch .claude/."

# 3. Long task from a file
bash scripts/delegate.sh --task-file /tmp/migration-plan.md

# 4. Inspect runs
bash scripts/delegate.sh --doctor
tail -5 .delegate/runs.log
jq '{vibe_version, usage, cost_usd, stop_reason, outcome, exit_code, session_id, limits}' \
  .delegate/runs/$(ls .delegate/runs | tail -1)/export.json
```

## FAQ

**Q: Why a wrapper script at all if there is an MCP server?**
A: One execution path for both shell/CI use and MCP use: config precedence, the approval policy, budget guards, and logging live in exactly one place (`delegate.sh`). The MCP server is a thin JSON-RPC adapter that shells out to it and enriches the result with `export.json`.

**Q: Can Claude Code monitor a run while it is in progress?**
A: Yes -- start it with `background=true`, watch it in `delegate_status` (running section from `started.json`), or use `output: "streaming"` and tail `runs/<ts>/server.log`. You can also resume the session interactively with `vibe -c` / `vibe --resume <session_id>`.

**Q: How do I steer a run that is still in progress?**
A: `delegate_interrupt(run=...)` sends SIGTERM (vibe writes `export.json` and exits 3), then `delegate_run(session_id=..., task=...)` resumes the same run's session with a small follow-up appended via `--resume`. New tasks need a new delegation without `session_id`. Never edit the session journal by hand -- `--resume` appends the message for you, and the resumed session keeps its pinned model/agent.

**Q: What happens when a budget guard trips?**
A: Vibe stops the run, writes `export.json` with the stop reason, and the wrapper propagates the exit code; `runs.log` records the non-zero rc and the MCP tool sets `isError`.

**Q: Is the delegate config file executed?**
A: No. Both the wrapper and the MCP server parse it line-by-line as `KEY=VALUE` text. Only the two scripts run, and they are part of the repo you already trust.
