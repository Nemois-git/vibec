# vibec

Delegate implementation and review work from **Claude Code** to **Mistral Vibe** (`mistral-vibe` CLI) over **MCP**.

Claude Code stays the orchestrator for long-horizon work. Self-contained subtasks -- simple file edits, implementation, review passes, test/fix loops -- are delegated through the `vibe` MCP server, which runs headless (`vibe -p`) inside the repo with Vibe's own tools and model, then returns a structured result. Every run is logged, so status and cost are auditable afterwards.

This repo is MCP-based on purpose: a Claude Code *subagent* always inherits the session's model (e.g. Sonnet), so a "vibe" subagent would still run on Sonnet. An MCP *tool* is deterministic: the Claude side only calls `mcp__delegate__delegate_run`, and the actual work runs on Mistral via `vibe -p`.

## How it works

```
Claude Code (main agent -- orchestration stays here)
  |  MCP tool call: mcp__delegate__delegate_run(task, mode?, model?, budgets?)
  v
scripts/delegate_mcp.py  (MCP stdio server, registered via .mcp.json)
  |  executes
  v
scripts/delegate.sh  (single source of config/args/logging)
  |  builds: vibe -p "<task>" --workdir <repo> --trust --<mode flag>
  |          [--model A] [--max-turns T] [--max-price P] [--max-tokens K]
  |          [--time-limit S] --output text --output-dir runs/<ts>/
  v
vibe -p (headless: own context, own model, own tool loop; edits/tests run in the repo)
  |
  v
.delegate/runs.log + .delegate/runs/<ts>/
(stdout.txt, stderr.txt, export.json, session journal)
  |
  +-- structured result back to Claude Code:
      answer, exit_code, run, run_dir, session_id, cost_usd, usage, outcome
```

## Requirements

- Claude Code (CLI, Desktop, or VS Code extension -- all load project `.mcp.json`).
- Mistral Vibe CLI (`vibe`, developed against 3.1.2): `uv tool install mistral-vibe`
- `python3` (stdlib only; the MCP server has no pip dependencies)
- A Mistral API key in `~/.vibe/.env` (`MISTRAL_API_KEY=...`), or run `vibe --setup` once.
- `jq` (optional) -- for ad-hoc pretty-printing of `export.json`.

Note for **Claude Code Desktop on macOS**: it is a GUI app with a minimal PATH, so `~/.local/bin` (the `uv tool` location) may not be visible. If `vibe` is not found there, symlink it into a GUI-visible dir, e.g. `sudo ln -s ~/.local/bin/vibe /opt/homebrew/bin/vibe`.

## Install

**Project level (default): nothing to do.** Clone the repo and open it in Claude Code. `.mcp.json` registers the `delegate` MCP server, `.claude/settings.json` auto-enables project MCP servers (`enableAllProjectMcpServers`) and pre-approves the three `mcp__delegate__*` tools, and `.claude/commands/delegate.md` adds the `/delegate` slash command.

**User level (optional, every project):**

```bash
./install.sh            # installs /delegate and the MCP server into ~/.claude and registers "delegate" (user scope)
./install.sh --dry-run  # prints what would change; touches nothing
```

Re-running `install.sh` updates the installed copy. It also migrates an earlier install under the name `vibe`: the old command and server files move to `~/.claude/migrated/vibe-<timestamp>/`, the user-scope `vibe` server is removed, and `delegate` is registered in its place. `install.sh` warns about `mcp__vibe__*` permission rules in `~/.claude/settings.json` but does not edit them; replace them with `mcp__delegate__delegate_run`, `mcp__delegate__delegate_wait`, `mcp__delegate__delegate_status`, `mcp__delegate__delegate_interrupt`, and `mcp__delegate__delegate_doctor`. `--skip-existing` keeps files that already exist.

Projects that used the earlier version rename `.claude/vibe-delegate.env` to `.claude/delegate.env` and the `VIBE_DELEGATE_*` keys to `DELEGATE_*`. Run logs move from `.vibe-delegate/` to `.delegate/`; old logs are not migrated.

The CLI path named in tool descriptions and instructions is the `delegate_mcp.py` that is running, so it fits either scope: `scripts/delegate_mcp.py` (project, via `.mcp.json`) or `~/.claude/mcp/delegate_mcp.py` (user, via `claude mcp add`).

**Verify the setup** (either in chat as a tool call, or in a shell):

```text
mcp__delegate__delegate_doctor
# or
bash scripts/delegate.sh --doctor
```

`--doctor` checks: `vibe` on PATH + version, `claude` on PATH, MCP files present, config file parseable, log dir writable, `jq`. One `PASS`/`WARN`/`FAIL` line per check; exit code is non-zero on any `FAIL`.

## Quick start

```text
/delegate "Add input validation to every CLI flag in scripts/ and make bash -n pass"
mcp__delegate__delegate_status            # recent runs + newest export.json
mcp__delegate__delegate_doctor            # health check
```

Or call the tool directly with arguments:

```text
mcp__delegate__delegate_run(task="Summarize README.md", mode="smart", model="my-fast-model", max_price=2)
```

Suggested line for an org-level system prompt (so delegation is deterministic):

> If the delegate MCP server is connected, call mcp__delegate__delegate_run for simple file edits, implementation, and review work. Orchestration, planning, and final integration decisions stay with you -- never delegate those.

## Background runs (detached; collect with wait)

Every MCP tool call blocks until it returns, so long delegations should be started detached to keep the orchestrator free. Start detached, keep working, then collect the result (wake-on-finish):

```text
mcp__delegate__delegate_run(task="...", background=true, alias="auth-refactor")
# returns immediately: alias, run (timestamp), run_dir, pid -- registered in .delegate/aliases.json
mcp__delegate__delegate_status                                   # shows running + finished runs + aliases
mcp__delegate__delegate_wait(alias="auth-refactor")              # block until done; returns answer + export fields
mcp__delegate__delegate_interrupt(alias="auth-refactor")         # graceful stop mid-run (SIGTERM, exit 3)
mcp__delegate__delegate_run(task="follow-up...", session_id="<id>")  # resume the same run with a small follow-up (--resume); new tasks get a new delegation
```

Runs are addressable by the returned `pid`/`run` timestamp or by a caller-chosen `alias` (stored in `.delegate/aliases.json`); `delegate_interrupt` and `delegate_wait` find a run's processes through the alias registry, the run's `dispatch.pid`, or `pgrep -f` on its command line.

## Direct CLI (same file as the MCP server, no MCP needed)

The installed server file is also a standalone CLI: `python3 <delegate_mcp.py> <command>` prints the same JSON result object the corresponding MCP tool returns. Every MCP tool call blocks until it returns; the CLI equivalents of the reading tools (wait = wake-on-finish, status = monitor) are the ones to use outside MCP. If you attach an external monitor or async watcher (harness tooling that polls runs), drive it through this CLI -- MCP calls block, so they are not suitable for watch loops:

```bash
# user-level install (install.sh):
PY="$HOME/.claude/mcp/delegate_mcp.py"
# project scope, from the repo root:
# PY="$PWD/scripts/delegate_mcp.py"

python3 "$PY" delegate "Summarize README.md" --mode smart --max-price 2
python3 "$PY" delegate "Refactor the arg parsing..." --background --alias auth-refactor   # returns run/run_dir/pid immediately
python3 "$PY" status                                     # running + finished runs + aliases
python3 "$PY" wait auth-refactor --timeout 600           # wake-on-finish: blocks until .delegate/runs/<run>/meta.json appears
python3 "$PY" interrupt auth-refactor                    # graceful stop; resume the same run later via session_id (small follow-up)
python3 "$PY" doctor                                     # health check
```

Run it with the target project as the working directory (or `CLAUDE_PROJECT_DIR` set). Run log paths: `.delegate/runs/<run>/{started.json,meta.json,export.json,stdout.txt,stderr.txt}`, `.delegate/runs.log`, `.delegate/aliases.json`.

## Tools

| Tool | Purpose | Key arguments |
|---|---|---|
| `mcp__delegate__delegate_run` | Delegate one self-contained task to the Vibe agent; blocks until done, or returns immediately with `background=true` | `task` (required), `background`, `alias`, `session_id`, `mode`, `model`, `agent`, `max_turns`, `max_price`, `max_tokens`, `time_limit`, `output` |
| `mcp__delegate__delegate_wait` | Block until a background run finishes, then return the full result | `run` or `alias`, `timeout_s` (default 600) |
| `mcp__delegate__delegate_status` | Running + recent finished runs, newest `export.json`, and the alias registry | `count` (default 10) |
| `mcp__delegate__delegate_interrupt` | SIGTERM a running delegation (export written, exit 3); resume the same run afterwards via `session_id` (small follow-up only) | `run` or `alias`, `wait_s` (default 30) |
| `mcp__delegate__delegate_doctor` | Integration health check | -- |

`session_id` is for resuming a run or making a small follow-up on it, not for handing new tasks to the same agent. Each new task starts a new delegation without `session_id`.

`delegate_run` returns `structuredContent` with: `answer` (Vibe's final report), `exit_code`, `duration_s`, `run`, `run_dir`, `mode`, `model`, `session_id`, `cost_usd`, `usage`, `outcome`, `stop_reason`, `vibe_version`, `stderr_tail`.

## Configuration

All defaults live in `.claude/delegate.env` (shared by the team). Precedence:

```
tool arguments / CLI flags  >  environment variables  >  .claude/delegate.env  >  built-in defaults
```

| Key | Default | Meaning |
|---|---|---|
| `DELEGATE_MODEL` | (empty) | Model alias passed to `vibe --model`. Define aliases in your own `~/.vibe/config.toml` (`[[models]]`); empty = Vibe's default routing. The repo never overrides your providers. |
| `DELEGATE_MODE` | `smart` | Approve mode: `smart` or `auto`, see below. |
| `DELEGATE_AGENT` | (empty) | Optional Vibe agent profile (`--agent`), e.g. a bounded profile in `~/.vibe/agents/`. |
| `DELEGATE_MAX_TURNS` | `200` | Max assistant turns of the run (root + subagents). |
| `DELEGATE_MAX_PRICE` | `10` | Max cumulative cost in USD. Note: `0` does **not** mean unlimited -- it stops the run before the next call once anything has cost money. Leave empty to omit the flag. |
| `DELEGATE_MAX_TOKENS` | (empty) | Max cumulative prompt+completion tokens. |
| `DELEGATE_TIME_LIMIT` | `3600` | Wall-clock interrupt after this many seconds. |
| `DELEGATE_OUTPUT` | `text` | `vibe -p` output: `text` (final answer), `json`, or `streaming`. |
| `DELEGATE_LOG_DIR` | `.delegate` | Run log dir, relative to the project root unless absolute. |

The config file is parsed line-by-line (`KEY=VALUE`, `#` comments); it is never sourced or evaluated.

## Approve modes (headless semantics)

Headless mode (`vibe -p`) never prompts. The mode decides which tool calls run; denied calls are printed on stderr and recorded in `export.json`.

| Mode | Flag passed to vibe | Behavior |
|---|---|---|
| `smart` (default) | `--smart-approve` | A classifier auto-runs the tool calls it judges safe; risky ones are denied and listed. Classifier model configurable via `[utility_models] smart_approve` in `~/.vibe/config.toml`. |
| `auto` | `--auto-approve` (alias `--yolo`) | Approves every tool call. Allowlists are bypassed under auto-approve, so use it only for trusted tasks. |

## Monitoring and status

Every run appends one TSV line to `.delegate/runs.log`:

```
timestamp  exit_code  duration_s  mode  model  agent  max_turns  max_price  task_preview
```

and writes `.delegate/runs/<timestamp>/` containing:

- `stdout.txt` -- Vibe's final answer
- `stderr.txt` -- warnings and denied tool calls
- `export.json` -- written by `vibe --output-dir`: `vibe_version`, `usage` (input/output/cached/total tokens), `cost_usd`, `stop_reason`, `outcome`, `exit_code`, `session_id`, `limits`, `config`, `steps`, `journal_dir`
- `session/` -- a copy of the session journal

```text
mcp__delegate__delegate_status [count]   # table of recent runs + newest export summary
vibe -c                          # in the project dir: resume the latest Vibe session
vibe --resume <id>               # <id> = session_id from export.json
```

Logs stay on the machine (`.delegate/` is gitignored) and never enter version control.

## Security notes

- Headless runs never prompt. `smart` auto-approves only what its classifier judges safe; `auto` is a deliberate opt-in per run.
- The repo's `.mcp.json` / `.claude/` files are trusted code once you open the repo in Claude Code -- review them like any other repo config. `enableAllProjectMcpServers` in `.claude/settings.json` auto-enables project MCP servers; drop it if you prefer approving each server manually.
- The delegate config is parsed, never executed; the wrapper runs `vibe` with `--workdir` pinned to the repo and `--trust` scoped to that single invocation.
- Budget guards (`--max-turns/--max-price/--max-tokens/--time-limit`) bound every run, root and subagents included.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `vibe` tool not found / `vibe: command not found` | `uv tool install mistral-vibe`; on Desktop (macOS) also symlink into `/opt/homebrew/bin`; then `mcp__delegate__delegate_doctor` |
| `delegate` MCP server not listed | Open the repo root; check `.mcp.json`; remove `enableAllProjectMcpServers` if you want a manual approval prompt instead |
| Tool call needs approval every time | Keep `.claude/settings.json` permissions for `mcp__delegate__*` |
| Auth errors from vibe | Put `MISTRAL_API_KEY` in `~/.vibe/.env` or run `vibe --setup` |
| `Tool call denied: ...` in the run's `stderr.txt` | The smart classifier judged the call risky. Re-run with `mode=auto` only for a trusted task, or narrow the task |
| Empty answer | A text run that ends without an answer says so on stderr -- read `stderr.txt` in the run dir |
| Cost/turn overruns | Lower `DELEGATE_MAX_PRICE` / `DELEGATE_MAX_TURNS` in `.claude/delegate.env` |

## File map

```
.mcp.json                     # registers the "delegate" MCP server (python3 scripts/delegate_mcp.py)
.claude/
+-- commands/delegate.md          # /delegate <task> -- delegates via mcp__delegate__delegate_run
+-- delegate.env         # shared defaults (model, approve mode, budgets, log dir)
+-- settings.json             # enableAllProjectMcpServers + pre-approved mcp__delegate__* tools
scripts/
+-- delegate_mcp.py               # MCP stdio server: delegate_run / delegate_status / delegate_doctor
+-- delegate.sh          # wrapper: config resolution, headless vibe -p, logging, --doctor
install.sh                    # optional user-level install into ~/.claude
docs/delegate.md         # detailed reference (MCP protocol notes, schemas, extension points)
```

See [docs/delegate.md](docs/delegate.md) for the full reference.

## License

MIT. See [LICENSE](LICENSE). Each source file carries the same copyright and SPDX header.

## Trademarks and affiliation

This project is independent. It is not affiliated with, endorsed by, or sponsored by Anthropic or Mistral AI.
"Claude", "Claude Code" and "Anthropic" belong to Anthropic, PBC. "Mistral", "Mistral AI" and "Mistral Vibe" belong to Mistral AI.
They appear here only to describe what this project works with. Claude Code and Mistral Vibe are not included in this repository;
install them separately and follow their own terms.
