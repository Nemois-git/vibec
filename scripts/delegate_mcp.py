#!/usr/bin/env python3
# Copyright (c) 2026 Jeongcheol Kim
# Licensed under the MIT License; see LICENSE at the repository root.
# SPDX-License-Identifier: MIT
"""MCP stdio server that exposes Mistral Vibe to Claude Code as tools.

Tools:
  delegate_run   run a self-contained task headless via the installed
                  delegate.sh wrapper (sits next to this file)
                  and return Vibe's answer plus run metadata (session_id, cost,
                  usage, outcome, run dir) from the run's export.json.
                  With background=true, starts the run detached and returns
                  immediately (run, run_dir, pid) so the orchestrator is not blocked.
  delegate_wait       block until a background run finishes, then return the same
                  structured result as a synchronous delegate_run
  delegate_status     recent runs (finished + currently running) from .delegate
  delegate_interrupt  SIGTERM a running delegation (vibe writes export.json, exit 3)
  delegate_doctor     integration health check (delegates to the wrapper's --doctor)

Transport: newline-delimited JSON-RPC 2.0 over stdio (MCP stdio transport).
Stdlib only, so the only runtime requirements are python3 and the vibe CLI.

Config precedence mirrors the wrapper: tool arguments > environment
variables > .claude/delegate.env > built-in defaults.
"""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _resolve_project_dir() -> Path:
    # CLAUDE_PROJECT_DIR wins (set by Claude Code when launching the MCP server).
    # CLI use without it: a repo checkout in the cwd, else the dir containing this file.
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd / "scripts" / "delegate.sh").is_file():
        return cwd
    if (SCRIPT_DIR.parent / "scripts" / "delegate.sh").is_file():
        return SCRIPT_DIR.parent
    return SCRIPT_DIR


PROJECT_DIR = _resolve_project_dir()
CONFIG_FILE = PROJECT_DIR / ".claude" / "delegate.env"

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "delegate-mcp", "version": "2.0.0"}

# Absolute path of the running server file, so CLI examples name the install this server runs from
# (user-level ~/.claude/mcp or project scripts/) instead of a path fixed at authoring time.
CLI_COMMAND = "python3 " + shlex.quote(str(Path(__file__).resolve()))

SERVER_INSTRUCTIONS = """\
This server delegates coding work to Mistral Vibe, a separate agent. You stay the orchestrator:
decide what to delegate, then verify what comes back.

Every MCP call blocks until it returns. For long tasks, start with delegate_run(background=true),
keep working, and collect the result later with delegate_wait.

Tools (WHEN picks the tool, HOW calls it):
  delegate_run  WHEN: a self-contained implementation or review task can be done by Vibe.
                 DO NOT: use it for work that needs this conversation's context, or for a single
                 edit you can make yourself.
                 HOW: pass `task`. Returns Vibe's answer and run metadata. background=true returns
                 at once with run, run_dir, pid and alias.
  delegate_wait      WHEN: collecting a background run. HOW: pass run or alias. Blocks until the run
                 finishes or timeout_s (default 600) passes; a run still going returns status "running".
  delegate_status    WHEN: checking what is running or finished. Never blocks. HOW: optional count (default 10).
  delegate_interrupt WHEN: a running delegation must stop. HOW: pass run or alias. Sends SIGTERM and waits
                 wait_s (default 30). To resume that same run with a small follow-up, use delegate_run(session_id=...).
  delegate_doctor    WHEN: verifying the setup after install, or as a CI gate. HOW: no args.

Writing the task: Vibe sees only `task`. Include the goal, file paths, constraints and acceptance
criteria. Example:
  In scripts/x.py rename parse() to parse_input() and update its two call sites in tests/test_x.py.
  Done when python3 -m unittest passes.

Failures: when a result has isError true, do not report success. Read exit_code, outcome and
stderr_tail from structuredContent. To finish or fix that same run, resume it with
delegate_run(session_id=..., task=<small follow-up>). Otherwise retry with a new delegation
(no session_id) and a smaller, more specific task.

Sessions: session_id is for resuming one run or making a small change to it. It is not a
worker to reuse: each new task is a new delegation without session_id, so Vibe starts with a
clean context.

Outside MCP (shell scripts, CI, external monitors), use the CLI. It prints the same JSON result on stdout:
  {CLI} wait RUN_OR_ALIAS [--timeout S]
  {CLI} status [--count N]
Monitors and async watchers must use the CLI, because MCP calls block and are not suitable for
watch loops. Run the CLI from the target project's directory, or with CLAUDE_PROJECT_DIR set.

Tool names are shown bare here; a client may prefix them, for example mcp__delegate__delegate_run.
"""


def find_wrapper() -> Path:
    env_w = os.environ.get("DELEGATE_WRAPPER")
    if env_w:
        return Path(env_w)
    for cand in (PROJECT_DIR / "scripts" / "delegate.sh", SCRIPT_DIR / "delegate.sh"):
        if cand.is_file():
            return cand
    return PROJECT_DIR / "scripts" / "delegate.sh"


WRAPPER = find_wrapper()

_CONFIG = None


def config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = {}
        if CONFIG_FILE.is_file():
            for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                _CONFIG[k.strip()] = v.strip()
    return _CONFIG


def resolve(key: str, default: str = "") -> str:
    return os.environ.get(key) or config().get(key) or default


def log_dir() -> Path:
    d = Path(resolve("DELEGATE_LOG_DIR", ".delegate"))
    return d if d.is_absolute() else PROJECT_DIR / d


# Popen handles for detached wrappers: holding them lets poll() reap exits and avoids GC warnings.
BACKGROUND_PROCS = []


def reap() -> None:
    # Reap dead background wrappers so they do not linger as zombies
    # (a zombie still answers os.kill(pid, 0), which would look "alive").
    BACKGROUND_PROCS[:] = [p for p in BACKGROUND_PROCS if p.poll() is None]
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def pid_alive(pid: int) -> bool:
    reap()
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# Pids of a run: the wrapper (from dispatch.pid, alive before it writes started.json), its vibe
# child (pgrep), and the alias registry pid. pgrep gets "--" so a pattern is never read as a flag.
def run_pids(run: str) -> list:
    pids = set()
    # '-' excluded so run "<ts>" does not match sibling run "<ts>-<pid>"
    for pat in (rf"--run-id {run}([^0-9-]|$)", rf"runs/{run}([^0-9-]|$)"):
        p = subprocess.run(["pgrep", "-f", "--", pat], capture_output=True, text=True)
        pids.update(int(x) for x in p.stdout.split())
    dispatch = log_dir() / "runs" / run / "dispatch.pid"
    if dispatch.is_file():
        try:
            dispatch_pid = int(dispatch.read_text(encoding="utf-8").strip())
        except ValueError:
            dispatch_pid = None
        if dispatch_pid and pid_alive(dispatch_pid):
            pids.add(dispatch_pid)
    # registry pid is alive from Popen onward, immune to the exec race
    for entry in load_aliases().values():
        if entry.get("run") == run and entry.get("pid") and pid_alive(int(entry["pid"])):
            pids.add(int(entry["pid"]))
    return sorted(pids)


def aliases_file() -> Path:
    return log_dir() / "aliases.json"


def load_aliases() -> dict:
    f = aliases_file()
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_aliases(a: dict) -> None:
    f = aliases_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(a, indent=2) + "\n", encoding="utf-8")


def register_alias(alias, run, run_dir, pid=None, status="running", session_id=None, rc=None) -> None:
    if not alias:
        return
    a = load_aliases()
    a[alias] = {
        "run": run, "run_dir": str(run_dir), "pid": pid, "status": status,
        "session_id": session_id, "rc": rc,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_aliases(a)


def resolve_run(x: str) -> str:
    if not x:
        return x
    if (log_dir() / "runs" / x).is_dir():
        return x
    a = load_aliases()
    if x in a:
        return a[x]["run"]
    return x


DELEGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "Self-contained task prompt for Vibe: goal, file paths, constraints, acceptance criteria. Vibe cannot see the Claude Code conversation."},
        "background": {"type": "boolean", "description": "If true, return immediately with run/run_dir/pid/alias and collect the result later with delegate_wait or delegate_status. Default false (blocks until the run ends)."},
        "alias": {"type": "string", "description": "Human-friendly name for this run. Registered in .delegate/aliases.json with run/pid/status; delegate_wait, delegate_interrupt and delegate_status accept the alias in place of the run timestamp."},
        "model": {"type": "string", "description": "Vibe model alias (--model) from your ~/.vibe/config.toml [[models]]. Empty = Vibe default routing. Set it for new runs; leave it empty when resuming with session_id."},
        "agent": {"type": "string", "description": "Vibe agent profile (--agent), e.g. a bounded profile from ~/.vibe/agents/."},
        "session_id": {"type": "string", "description": "Resume this existing Vibe session (--resume <id>) to finish or make a small follow-up on that same run. Do not reuse a session for unrelated tasks; start a new delegation without session_id instead. Get it from a previous delegate_run result, delegate_status, or the run's export.json."},
        "max_turns": {"type": "integer", "description": "Max assistant turns (--max-turns). Default 200."},
        "max_price": {"type": "number", "description": "Max cumulative cost in USD (--max-price). Default 10."},
        "max_tokens": {"type": "integer", "description": "Max cumulative tokens (--max-tokens)."},
        "time_limit": {"type": "integer", "description": "Wall-clock seconds before Vibe interrupts the run (--time-limit). Default 3600."},
        "output": {"type": "string", "enum": ["text", "json", "streaming"], "description": "vibe -p output format. Default: text (final answer)."},
    },
    "required": ["task"],
}

TOOLS = [
    {
        "name": "delegate_run",
        "description": "WHEN: a self-contained implementation or review task can be done by the Vibe agent instead of you. DO NOT use it for work that needs this conversation's context, or for a single edit you can make yourself. HOW: pass `task` (plus optional model, agent, limits). This call blocks until the run ends and returns Vibe's answer and run metadata; background=true returns at once with run, run_dir, pid and alias, collected later with delegate_wait. CLI: {CLI} delegate \"<task>\" [--background] [--alias NAME].",
        "inputSchema": DELEGATE_SCHEMA,
    },
    {
        "name": "delegate_wait",
        "description": "WHEN: collecting the result of a delegate_run(background=true) run. HOW: pass run or alias. Blocks until the run finishes or timeout_s (default 600) passes, then returns the same result as delegate_run; a run still going at timeout returns status \"running\". Async or external waiters: use the CLI form, since this call blocks. CLI: {CLI} wait RUN_OR_ALIAS [--timeout S].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string", "description": "Run timestamp (directory name under .delegate/runs/) or an alias given to delegate_run."},
                "alias": {"type": "string", "description": "Alias given to delegate_run; resolved to its run timestamp."},
                "timeout_s": {"type": "integer", "description": "Max seconds to wait. Default 600."},
            },
            "required": [],
        },
    },
    {
        "name": "delegate_status",
        "description": "WHEN: checking which runs are running or finished. Never blocks. HOW: optional count (default 10). Returns running runs, recent finished runs, the newest run's export (session_id, cost, usage, outcome) and the alias registry. CLI: {CLI} status [--count N].",
        "inputSchema": {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "How many recent finished runs to show. Default 10."}},
        },
    },
    {
        "name": "delegate_interrupt",
        "description": "WHEN: a running delegation must be stopped. HOW: pass run or alias. Sends SIGTERM and waits wait_s seconds (default 30). Returns the signaled pids and still_alive; isError when nothing was signaled or processes remain. Vibe still writes its export. To resume that same run with a small follow-up, use delegate_run(session_id=..., task=...). CLI: {CLI} interrupt RUN_OR_ALIAS [--wait S].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string", "description": "Run timestamp (directory name under .delegate/runs/) of the running delegation, or its alias."},
                "alias": {"type": "string", "description": "Alias given to delegate_run; resolved to its run timestamp."},
                "wait_s": {"type": "integer", "description": "Seconds to wait for the processes to exit. Default 30."},
            },
            "required": [],
        },
    },
    {
        "name": "delegate_doctor",
        "description": "WHEN: verifying the setup works, after install or as a CI gate. HOW: no args. Returns PASS, WARN or FAIL per check; isError on any FAIL. CLI: {CLI} doctor.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def read_export(run_dir) -> dict:
    ep = Path(run_dir) / "export.json"
    if ep.is_file():
        try:
            return json.loads(ep.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def build_cmd(args: dict, task: str, run_id: str = None) -> list:
    cmd = ["bash", str(WRAPPER)]
    for key, flag in (
        ("model", "--model"), ("agent", "--agent"), ("session_id", "--resume"), ("alias", "--alias"),
        ("max_turns", "--max-turns"), ("max_price", "--max-price"),
        ("max_tokens", "--max-tokens"), ("time_limit", "--time-limit"), ("output", "--output"),
    ):
        if args.get(key) not in (None, ""):
            cmd += [flag, str(args[key])]
    if run_id:
        cmd += ["--run-id", run_id]
    cmd += ["--", task]
    return cmd


def result_from_run(run_dir: Path, meta: dict, answer: str, errs: str, rc: int, started: float) -> dict:
    export = read_export(run_dir)
    structured = {
        "answer": answer,
        "exit_code": rc,
        "duration_s": meta.get("duration_s", round(time.time() - started)),
        "run": meta.get("run") or run_dir.name,
        "alias": meta.get("alias") or None,
        "run_dir": str(run_dir),
        "status": "finished",
        "mode": meta.get("mode"),
        "model": meta.get("model") or None,
        "session_id": export.get("session_id"),
        "cost_usd": export.get("cost_usd"),
        "usage": export.get("usage"),
        "outcome": export.get("outcome"),
        "stop_reason": export.get("stop_reason"),
        "vibe_version": export.get("vibe_version"),
        "stderr_tail": (errs or "")[-2000:],
    }
    meta_text = {k: v for k, v in structured.items() if k not in ("answer", "stderr_tail")}
    text = (answer if answer.strip() else errs) + "\n\n--- vibe run ---\n" + json.dumps(meta_text, indent=2)
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured, "isError": rc != 0}


def start_background(args: dict, task: str) -> dict:
    base = time.strftime("%Y%m%d-%H%M%S")
    runs_root = log_dir() / "runs"
    ts = base
    n = 0
    while (runs_root / ts).exists():
        n += 1
        ts = f"{base}-{os.getpid()}-{n}"
    run_dir = runs_root / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "meta.json"
    env = dict(os.environ)
    env["DELEGATE_META_FILE"] = str(meta_path)
    cmd = build_cmd(args, task, run_id=ts)
    logf = open(run_dir / "server.log", "w")
    # stdin=DEVNULL: the child must never read from this server's protocol pipe
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_DIR), env=env, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    (run_dir / "dispatch.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    BACKGROUND_PROCS.append(proc)
    logf.close()
    alias = args.get("alias")
    if alias:
        register_alias(alias, ts, run_dir, pid=proc.pid, status="running")
    structured = {
        "status": "running",
        "run": ts,
        "alias": alias or None,
        "run_dir": str(run_dir),
        "pid": proc.pid,
        "note": "collect with delegate_wait(run=...) (blocks until done), or check delegate_status; stop with delegate_interrupt(run=...); resume that same run with a small follow-up via delegate_run(session_id=..., task=...)",
    }
    text = f"started background run {ts} (pid {proc.pid})\nrun_dir: {run_dir}\nwait: delegate_wait(run=\"{ts}\")"
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured}


# Seconds a stopped process group gets to exit after SIGTERM before SIGKILL.
STOP_GRACE_S = 15


# SIGTERM to the whole group lets the wrapper's trap stop vibe and still write
# export.json and meta.json; SIGKILL is the fallback so a wedged group cannot hang the server.
def stop_process_group(proc: subprocess.Popen) -> tuple:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return proc.communicate(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        return proc.communicate()


def call_delegate(args: dict) -> dict:
    task = args.get("task")
    if not task:
        return err("delegate_run: 'task' is required")
    if args.get("mode") not in (None, "", "smart"):
        return err("delegate_run: 'mode' was removed; delegated runs always use --smart-approve")
    if args.get("background"):
        return start_background(args, task)

    cmd = build_cmd(args, task)
    fd, meta_path = tempfile.mkstemp(prefix="delegate-meta-", suffix=".json")
    os.close(fd)
    env = dict(os.environ)
    env["DELEGATE_META_FILE"] = meta_path

    started = time.time()
    try:
        timeout = float(args.get("time_limit") or resolve("DELEGATE_TIME_LIMIT", "3600")) + 120
    except (TypeError, ValueError):
        timeout = None

    try:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT_DIR), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, errs = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            out, errs = stop_process_group(proc)
            rc = 124
            errs = "delegate: MCP-side timeout (vibe time limit + 120s exceeded)\n" + (errs or "")
    finally:
        meta = {}
        try:
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            os.unlink(meta_path)
        except OSError:
            pass

    run_dir = Path(meta.get("run_dir")) if meta.get("run_dir") else None
    if run_dir is None:
        run_dir = log_dir() / "runs" / "unknown"
    if args.get("alias"):
        register_alias(args["alias"], meta.get("run") or run_dir.name, run_dir, status="finished",
                       session_id=read_export(run_dir).get("session_id"), rc=rc)
    return result_from_run(run_dir, meta, out, errs, rc, started)


def call_wait(args: dict) -> dict:
    raw = args.get("run") or args.get("alias")
    if not raw:
        return err("delegate_wait: 'run' (timestamp or alias) is required")
    run = resolve_run(raw)
    try:
        timeout_s = float(args.get("timeout_s", 600))
    except (TypeError, ValueError):
        timeout_s = 600.0
    run_dir = log_dir() / "runs" / run
    meta_path = run_dir / "meta.json"
    started = time.time()
    while time.time() - started < timeout_s:
        if meta_path.is_file() or not run_pids(run):
            break
        time.sleep(1)
    out_file = run_dir / "stdout.txt"
    answer = out_file.read_text(encoding="utf-8") if out_file.is_file() else ""
    err_file = run_dir / "stderr.txt"
    errs = err_file.read_text(encoding="utf-8") if err_file.is_file() else ""
    meta = None
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = None
    if meta is not None:
        res = result_from_run(run_dir, meta, answer, errs, meta.get("rc", 0), started)
        sc = res["structuredContent"]
        if raw in load_aliases():
            register_alias(raw, run, run_dir, status="finished", session_id=sc.get("session_id"), rc=sc.get("exit_code"))
        return res
    export = read_export(run_dir)
    if not export and run_pids(run):
        text = f"run {run}: still running after {timeout_s:.0f}s. run_dir: {run_dir}"
        return {"content": [{"type": "text", "text": text}], "structuredContent": {"status": "running", "run": run, "run_dir": str(run_dir)}, "isError": False}
    meta = {"run": run, "duration_s": round(time.time() - started), "mode": None, "model": ""}
    sj = run_dir / "started.json"
    if sj.is_file():
        try:
            meta["mode"] = json.loads(sj.read_text(encoding="utf-8")).get("mode")
        except Exception:
            pass
    res = result_from_run(run_dir, meta, answer, errs, export.get("exit_code", 0), started)
    if not meta_path.is_file():
        res["structuredContent"]["status"] = "finished (no meta.json; synthesized from export.json)"
    sc = res["structuredContent"]
    if raw in load_aliases():
        register_alias(raw, run, run_dir, status=sc.get("status", "finished"), session_id=sc.get("session_id"), rc=sc.get("exit_code"))
    return res


def call_status(args: dict) -> dict:
    try:
        count = int(args.get("count", 10))
    except (TypeError, ValueError):
        count = 10
    runs_log = log_dir() / "runs.log"
    lines = []
    if runs_log.is_file():
        lines = [l for l in runs_log.read_text(encoding="utf-8").splitlines() if l.strip()][-count:]
    runs = []
    for l in lines:
        p = l.split("\t")
        runs.append({
            "run": p[0] if len(p) > 0 else "",
            "exit_code": p[1] if len(p) > 1 else "",
            "duration_s": p[2] if len(p) > 2 else "",
            "mode": p[3] if len(p) > 3 else "",
            "model": p[4] if len(p) > 4 else "",
            "agent": p[5] if len(p) > 5 else "",
            "max_turns": p[6] if len(p) > 6 else "",
            "max_price": p[7] if len(p) > 7 else "",
            "task_preview": p[8] if len(p) > 8 else "",
        })
    running = []
    runs_root = log_dir() / "runs"
    if runs_root.is_dir():
        for d in sorted(runs_root.iterdir()):
            sj, mj = d / "started.json", d / "meta.json"
            if sj.is_file() and not mj.is_file():
                try:
                    s = json.loads(sj.read_text(encoding="utf-8"))
                    running.append({
                        "run": s.get("run"), "pid": s.get("wrapper_pid"), "started_at": s.get("started_at"),
                        "mode": s.get("mode"), "task_preview": s.get("task_preview"),
                    })
                except Exception:
                    pass
    newest = None
    if runs:
        rd = log_dir() / "runs" / runs[-1]["run"]
        e = read_export(rd)
        if e:
            newest = {
                "run_dir": str(rd),
                "session_id": e.get("session_id"),
                "cost_usd": e.get("cost_usd"),
                "usage": e.get("usage"),
                "outcome": e.get("outcome"),
                "stop_reason": e.get("stop_reason"),
                "exit_code": e.get("exit_code"),
                "vibe_version": e.get("vibe_version"),
            }
    text = "running now:\n" + (json.dumps(running, indent=2) if running else "(none)")
    text += "\n\nrecent finished runs (newest last):\n" + ("\n".join(lines) if lines else "(no runs yet)")
    if newest:
        text += "\n\nnewest finished run export.json:\n" + json.dumps(newest, indent=2)
        text += "\n\nresume that same run (small follow-up only): delegate_run(session_id=" + str(newest.get("session_id")) + ", task=...)"
    aliases = load_aliases()
    text += "\n\naliases:\n" + (json.dumps(aliases, indent=2) if aliases else "(none)")
    return {"content": [{"type": "text", "text": text}], "structuredContent": {"running": running, "runs": runs, "newest": newest, "aliases": aliases}}


def call_interrupt(args: dict) -> dict:
    raw = args.get("run") or args.get("alias")
    if not raw:
        return err("delegate_interrupt: 'run' (timestamp or alias) is required")
    run = resolve_run(raw)
    try:
        wait_s = float(args.get("wait_s", 30))
    except (TypeError, ValueError):
        wait_s = 30.0
    run_dir = log_dir() / "runs" / run
    meta_path = run_dir / "meta.json"
    pids = run_pids(run)
    signaled = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signaled.append(pid)
        except ProcessLookupError:
            pass
    deadline = time.time() + wait_s
    while time.time() < deadline:
        alive = [pid for pid in pids if pid_alive(pid)]
        if not alive:
            break
        time.sleep(0.5)
    alive = [pid for pid in pids if pid_alive(pid)]
    meta_written = meta_path.is_file()
    text = (
        f"run {run}: signaled {signaled or '[]'}"
        + (f", still alive after {wait_s:.0f}s: {alive}" if alive else ", processes exited")
        + (", meta.json written (run logged, exit code 3)" if meta_written else ", no meta.json (wrapper signaled before the run started)")
        + "\nresume that same run (small follow-up only): delegate_run(session_id=<session_id>, task=<follow-up>)"
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"run": run, "signaled": signaled, "still_alive": alive, "meta_written": meta_written},
        "isError": bool(alive) or not signaled,
    }


def call_doctor(args: dict) -> dict:
    proc = subprocess.run(["bash", str(WRAPPER), "--doctor"], cwd=str(PROJECT_DIR), capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    return {"content": [{"type": "text", "text": out}], "structuredContent": {"exit_code": proc.returncode}, "isError": proc.returncode != 0}


HANDLERS = {
    "delegate_run": call_delegate,
    "delegate_wait": call_wait,
    "delegate_status": call_status,
    "delegate_interrupt": call_interrupt,
    "delegate_doctor": call_doctor,
}


def respond(mid, result=None, code=None, message=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if code is not None:
        msg["error"] = {"code": code, "message": message or "error"}
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle_call(params: dict) -> dict:
    name = params.get("name", "")
    handler = HANDLERS.get(name)
    if handler is None:
        return err(f"unknown tool: {name}")
    try:
        return handler(params.get("arguments") or {})
    except Exception as e:
        return err(f"{name} failed: {e}")


def cli(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="delegate_mcp.py",
        description="Standalone CLI for the delegate MCP server: same tools, same JSON result objects on stdout. Run with cwd inside the target project or CLAUDE_PROJECT_DIR set.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("delegate", help="run one task via vibe -p (same as MCP delegate_run)", allow_abbrev=False)
    d.add_argument("task", help="self-contained task prompt")
    d.add_argument("--background", action="store_true")
    d.add_argument("--alias")
    d.add_argument("--session-id")
    d.add_argument("--model")
    d.add_argument("--agent")
    d.add_argument("--max-turns", type=int)
    d.add_argument("--max-price", type=float)
    d.add_argument("--max-tokens", type=int)
    d.add_argument("--time-limit", type=int)
    d.add_argument("--output", choices=["text", "json", "streaming"])

    w = sub.add_parser("wait", help="block until a background run finishes (wake-on-finish)")
    w.add_argument("run", help="run timestamp or alias")
    w.add_argument("--timeout", type=float, default=600, dest="timeout_s")

    s = sub.add_parser("status", help="recent + running runs and the alias registry")
    s.add_argument("--count", type=int, default=10)

    i = sub.add_parser("interrupt", help="SIGTERM a running delegation")
    i.add_argument("run", help="run timestamp or alias")
    i.add_argument("--wait", type=float, default=30, dest="wait_s")

    sub.add_parser("doctor", help="integration check")

    ns = p.parse_args(argv)
    if ns.command == "delegate":
        args = {"task": ns.task}
        for key in ("background", "alias", "model", "agent", "max_turns", "max_price", "max_tokens", "time_limit", "output"):
            v = getattr(ns, key)
            if v is not None and v is not False:
                args[key] = v
        if ns.session_id:
            args["session_id"] = ns.session_id
        result = call_delegate(args)
    elif ns.command == "wait":
        result = call_wait({"run": ns.run, "timeout_s": ns.timeout_s})
    elif ns.command == "status":
        result = call_status({"count": ns.count})
    elif ns.command == "interrupt":
        result = call_interrupt({"run": ns.run, "wait_s": ns.wait_s})
    else:
        result = call_doctor({})
    print(json.dumps(result, indent=2))
    return 1 if result.get("isError") else 0


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method is None:
            continue
        if method == "initialize":
            params = msg.get("params") or {}
            respond(mid, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS.replace("{CLI}", CLI_COMMAND),
            })
        elif method == "ping":
            respond(mid, {})
        elif method == "tools/list":
            respond(mid, {"tools": [dict(t, description=t["description"].replace("{CLI}", CLI_COMMAND)) for t in TOOLS]})
        elif method == "tools/call":
            respond(mid, handle_call(msg.get("params") or {}))
        elif method.startswith("notifications/"):
            pass
        elif mid is not None:
            respond(mid, code=-32601, message=f"method not found: {method}")


if __name__ == "__main__":
    main()
