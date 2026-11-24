#!/usr/bin/env bash
# Copyright (c) 2026 Jeongcheol Kim
# Licensed under the MIT License; see LICENSE at the repository root.
# SPDX-License-Identifier: MIT
# Fake vibe CLI for coverage driving: honours -p, --output-dir, --resume; SLEEP tasks block until SIGTERM.
# Records the argv it received to <output-dir>/argv.txt so tests can assert which flags reached it.
orig_args=("$@")
out_dir=""; prompt=""; resume=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) echo "vibe 0.0.0-fake"; exit 0 ;;
    --output-dir) out_dir="$2"; shift 2 ;;
    -p) prompt="$2"; shift 2 ;;
    --resume) resume="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$out_dir" ]] && printf '%s\n' "${orig_args[@]}" > "$out_dir/argv.txt"
sid="${resume:-fake-session-1}"
export_run() {
  printf '{"session_id":"%s","cost_usd":0.01,"usage":{"tokens":1},"outcome":"%s","exit_code":%s,"stop_reason":"end_turn","vibe_version":"0.0.0-fake"}\n' "$sid" "$1" "$2" > "$out_dir/export.json"
}
on_term() { kill "$child" 2>/dev/null; export_run terminated 3; exit 3; }
trap on_term TERM
if [[ "$prompt" == *SLEEP* ]]; then
  sleep 60 &
  child=$!
  wait "$child"
fi
echo "FAKE_ANSWER: $prompt"
export_run success 0
