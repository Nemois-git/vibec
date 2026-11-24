#!/usr/bin/env bash
# Copyright (c) 2026 Jeongcheol Kim
# Licensed under the MIT License; see LICENSE at the repository root.
# SPDX-License-Identifier: MIT
# CLI smoke tests for the delegate MCP server (scripts/delegate_mcp.py, run directly).
#
# Tests 5-7 make real, tiny vibe API calls; they need the vibe CLI installed
# and authenticatable. Run from anywhere: bash tests/delegate_smoke.sh
#
# Note: the CLI exits non-zero when a run ends abnormally (isError), so every
# output is captured to a file and judged by its JSON content, not exit code.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRV="$REPO/scripts/delegate_mcp.py"
OUT="$(mktemp)"
PASS=0
FAIL=0

check() { # check <name> <exit_code>
  if [[ "$2" -eq 0 ]]; then
    echo "PASS  $1"; PASS=$((PASS + 1))
  else
    echo "FAIL  $1"; FAIL=$((FAIL + 1))
  fi
}

# 1. Script compiles.
/usr/bin/python3 -m py_compile "$SRV" >/dev/null 2>&1
check "script compiles" $?

# 2. --help works and lists the subcommands.
help="$("$SRV" --help 2>&1)"
printf '%s' "$help" | grep -q delegate
check "--help lists delegate" $?
printf '%s' "$help" | grep -q wait
check "--help lists wait" $?

# 3. Doctor runs and reports the vibe CLI check.
"$SRV" doctor > "$OUT" 2>/dev/null
printf '%s' "$(cat "$OUT")" | grep -q "vibe-cli"
check "doctor reports vibe-cli" $?

# 4. status returns valid JSON with a runs field in structuredContent.
"$SRV" status > "$OUT" 2>/dev/null
/usr/bin/python3 -c 'import json; d=json.load(open("'"$OUT"'")); assert "runs" in d["structuredContent"]'
check "status returns valid JSON" $?

# 5. Synchronous delegate: tiny real run, answer echoed back.
alias_sync="smoke-sync-$(date +%s)"
"$SRV" delegate "Reply with exactly: SMOKE_SYNC_OK" --max-price 0.5 --alias "$alias_sync" > "$OUT" 2>/dev/null
check "sync delegate exits 0" $?
/usr/bin/python3 -c 'import json; assert "SMOKE_SYNC_OK" in json.load(open("'"$OUT"'"))["structuredContent"]["answer"]'
check "sync delegate answer is SMOKE_SYNC_OK" $?

# 6. Background dispatch and wait-by-alias.
alias_bg="smoke-bg-$(date +%s)"
"$SRV" delegate "Reply with exactly: SMOKE_BG_OK" --max-price 0.5 --background --alias "$alias_bg" > "$OUT" 2>/dev/null
check "background delegate exits 0" $?
"$SRV" wait "$alias_bg" --timeout 300 > "$OUT" 2>/dev/null
/usr/bin/python3 -c 'import json; d=json.load(open("'"$OUT"'"))["structuredContent"]; assert d.get("status")=="finished" and "SMOKE_BG_OK" in d["answer"]'
check "wait(by alias) returns SMOKE_BG_OK finished" $?

# 7. Interrupt a long background run; wait still reports it as finished.
alias_int="smoke-int-$(date +%s)"
"$SRV" delegate "Count slowly from 1 to 1000000, one number per line. Never finish early." --max-price 2.0 --background --alias "$alias_int" > "$OUT" 2>/dev/null
check "long background delegate exits 0" $?
sleep 4
"$SRV" interrupt "$alias_int" --wait 30 > "$OUT" 2>/dev/null
check "interrupt by alias executes" $?
"$SRV" wait "$alias_int" --timeout 300 > "$OUT" 2>/dev/null
/usr/bin/python3 -c 'import json; d=json.load(open("'"$OUT"'"))["structuredContent"]; assert d.get("status")=="finished" and d.get("outcome")=="terminated"'
check "interrupted run reports finished + terminated" $?

rm -f "$OUT"
echo
echo "cli_smoke: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
