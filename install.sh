#!/usr/bin/env bash
# Copyright (c) 2026 Jeongcheol Kim
# Licensed under the MIT License; see LICENSE at the repository root.
# SPDX-License-Identifier: MIT
# Optional user-level install for Claude Code (every project, not just this repo).
# Project-level use needs no install: Claude Code loads .mcp.json and .claude/ from the repo.
#
# Re-running updates ~/.claude in place. An install under the previous name ("vibe") is detected
# and migrated: its files move to a backup folder and its user-scope MCP registration is replaced.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude"
CLAUDE_JSON="$HOME/.claude.json"
SERVER_NAME="delegate"
LEGACY_NAME="vibe"
SKIP_EXISTING=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --skip-existing) SKIP_EXISTING=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) echo "Usage: install.sh [--skip-existing] [--dry-run]"; exit 0 ;;
    *) echo "install.sh: unknown option '$arg' (expected --skip-existing or --dry-run)" >&2; exit 2 ;;
  esac
done

# Files written by the previous name, relative to $DEST.
LEGACY_FILES=(
  "commands/vibe.md"
  "mcp/vibe_mcp.py"
  "mcp/vibe-delegate.sh"
)

# "<source relative to repo root> <destination relative to $DEST>"
INSTALL_PAIRS=(
  ".claude/commands/delegate.md commands/delegate.md"
  "scripts/delegate_mcp.py mcp/delegate_mcp.py"
  "scripts/delegate.sh mcp/delegate.sh"
)

say() { printf '%s\n' "$*"; }

# Reads ~/.claude.json directly: user-scope servers are its top-level mcpServers.
registered_in_user_scope() {
  [[ -f "$CLAUDE_JSON" ]] || return 1
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if sys.argv[2] in (d.get("mcpServers") or {}) else 1)' \
    "$CLAUDE_JSON" "$1" 2>/dev/null
}

remove_user_server() {
  if ! command -v claude >/dev/null 2>&1; then
    say "claude CLI not found: remove the user-scope '$1' server yourself: claude mcp remove $1 --scope user"
    return 1
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would remove user-scope MCP server '$1'"
    return 0
  fi
  claude mcp remove "$1" --scope user
}

install_file() {
  local src="$1" dst="$2"
  if [[ "$SKIP_EXISTING" -eq 1 && -e "$dst" ]]; then
    say "skip (already exists): $dst"
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would install: $dst"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  say "installed: $dst"
}

migrate_legacy() {
  local rel found=0 backup="$DEST/migrated/$LEGACY_NAME-$(date +%Y%m%d-%H%M%S)"
  for rel in "${LEGACY_FILES[@]}"; do
    [[ -e "$DEST/$rel" ]] || continue
    found=1
    if [[ "$DRY_RUN" -eq 1 ]]; then
      say "[dry-run] would move legacy file to backup: $DEST/$rel"
    else
      mkdir -p "$backup/$(dirname "$rel")"
      mv "$DEST/$rel" "$backup/$rel"
      say "migrated legacy file to backup: $backup/$rel"
    fi
  done
  if registered_in_user_scope "$LEGACY_NAME"; then
    found=1
    say "found legacy user-scope MCP server '$LEGACY_NAME' in $CLAUDE_JSON"
    remove_user_server "$LEGACY_NAME" || true
  fi
  if [[ "$found" -eq 1 && "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would migrate legacy '$LEGACY_NAME' install to '$SERVER_NAME'"
  elif [[ "$found" -eq 1 ]]; then
    say "legacy '$LEGACY_NAME' install migrated to '$SERVER_NAME'"
  fi
  return 0
}

register_server() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would register MCP server '$SERVER_NAME' at user scope"
    return 0
  fi
  if registered_in_user_scope "$SERVER_NAME"; then
    remove_user_server "$SERVER_NAME" || true
  fi
  if command -v claude >/dev/null 2>&1; then
    claude mcp add "$SERVER_NAME" --scope user -- python3 "$DEST/mcp/delegate_mcp.py"
    say "registered: '$SERVER_NAME' (user scope) -> $DEST/mcp/delegate_mcp.py"
  else
    say "claude CLI not found. Register manually: claude mcp add $SERVER_NAME --scope user -- python3 \"$DEST/mcp/delegate_mcp.py\""
  fi
}

warn_stale_permissions() {
  local f
  for f in "$DEST/settings.json" "$DEST/settings.local.json"; do
    [[ -f "$f" ]] || continue
    if grep -q "mcp__${LEGACY_NAME}__" "$f"; then
      say "warning: $f still allows mcp__${LEGACY_NAME}__* tools (not edited automatically)."
      say "         Replace them with mcp__${SERVER_NAME}__delegate_run, _wait, _status, _interrupt, _doctor."
    fi
  done
}

say "install.sh: destination $DEST$( [[ "$DRY_RUN" -eq 1 ]] && echo ' (dry run, no changes)')"
migrate_legacy
for pair in "${INSTALL_PAIRS[@]}"; do
  src="$ROOT/${pair%% *}"
  dst="$DEST/${pair#* }"
  install_file "$src" "$dst"
done
register_server
warn_stale_permissions

say ""
say "Verify afterwards with the MCP tool: mcp__${SERVER_NAME}__delegate_doctor"
