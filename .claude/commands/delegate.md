---
description: Delegate a task to the Vibe agent (mcp__delegate__delegate_run), then verify and report
argument-hint: [--mode smart|auto] [--model ALIAS] [--max-turns N] [--max-price D] [--background] <task description>
---

Call the MCP tool `mcp__delegate__delegate_run` for this task:

$ARGUMENTS

Parse leading --flags into the tool arguments (mode, model, max_turns, max_price, ...; --background sets background=true). The remaining text is the `task`: make it self-contained (goal, file paths, constraints, acceptance criteria), because Vibe cannot see this conversation.

For a long task, use background=true, keep working, and collect the result later with mcp__delegate__delegate_wait (alias or run).

If the result has isError true, do not report success. Read exit_code, outcome and stderr_tail from the result. To finish or fix that same run, resume it with mcp__delegate__delegate_run(session_id=..., task=...) (small follow-up only); otherwise retry with a new delegation (no session_id) and a narrower task. Do not reuse a session for unrelated tasks.

Otherwise verify the changes yourself: `git status`, `git diff --stat`, and the acceptance criteria. Report files changed, the verification result, and the run timestamp (inspect later with mcp__delegate__delegate_status).
