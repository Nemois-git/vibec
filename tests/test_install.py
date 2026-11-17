# Copyright (c) 2026 Jeongcheol Kim
# Licensed under the MIT License; see LICENSE at the repository root.
# SPDX-License-Identifier: MIT
"""Tests for install.sh: fresh install, legacy "vibe" migration, dry run, and skip-existing.

Each test runs install.sh with a temporary HOME and a fake `claude` CLI, so the real
~/.claude and ~/.claude.json are never touched.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"

FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
path = os.path.join(os.environ["HOME"], ".claude.json")
data = json.load(open(path)) if os.path.exists(path) else {}
servers = data.setdefault("mcpServers", {})
if args[:2] == ["mcp", "remove"]:
    servers.pop(args[2], None)
elif args[:2] == ["mcp", "add"]:
    cmd = args[args.index("--") + 1:]
    servers[args[2]] = {"type": "stdio", "command": cmd[0], "args": cmd[1:]}
json.dump(data, open(path, "w"), indent=2)
'''

LEGACY_FILES = ["commands/vibe.md", "mcp/vibe_mcp.py", "mcp/vibe-delegate.sh"]


def snapshot(home):
    tracked = [home / ".claude", home / ".claude.json"]
    files = {}
    for base in tracked:
        if base.is_file():
            files[base.name] = base.read_bytes()
        elif base.is_dir():
            files.update({str(p.relative_to(home)): p.read_bytes() for p in base.rglob("*") if p.is_file()})
    return files


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="install-tests-"))
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.log = self.tmp / "claude.log"
        self.home.mkdir()
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_legacy(self, registered=True):
        dest = self.home / ".claude"
        for rel in LEGACY_FILES:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_text("legacy " + rel)
        (dest / "settings.json").write_text(json.dumps(
            {"permissions": {"allow": ["mcp__vibe__vibe_delegate"]}}))
        servers = {"nemograph": {"type": "stdio", "command": "x", "args": []}}
        if registered:
            servers["vibe"] = {"type": "stdio", "command": "python3",
                               "args": [str(dest / "mcp/vibe_mcp.py")]}
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": servers}))

    def servers(self):
        return json.loads((self.home / ".claude.json").read_text())["mcpServers"]

    def run_install(self, *args, with_claude=True):
        env = {
            "HOME": str(self.home),
            "PATH": (f"{self.bin}:" if with_claude else "") + "/usr/bin:/bin",
            "FAKE_CLAUDE_LOG": str(self.log),
        }
        return subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True)

    def test_fresh_install_installs_files_and_registers_delegate(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = self.home / ".claude"
        for rel in ["commands/delegate.md", "mcp/delegate_mcp.py", "mcp/delegate.sh"]:
            self.assertTrue((dest / rel).is_file(), rel)
        self.assertEqual(self.servers()["delegate"]["args"], [str(dest / "mcp/delegate_mcp.py")])

    def test_legacy_install_is_migrated_and_nothing_else_is_lost(self):
        self.make_legacy()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = self.home / ".claude"
        for rel in LEGACY_FILES:
            self.assertFalse((dest / rel).exists(), rel)
        backups = list((dest / "migrated").glob("vibe-*"))
        self.assertEqual(len(backups), 1)
        for rel in LEGACY_FILES:
            self.assertTrue((backups[0] / rel).is_file(), rel)
        servers = self.servers()
        self.assertNotIn("vibe", servers)
        self.assertIn("nemograph", servers)
        self.assertIn("delegate", servers)
        self.assertIn("legacy 'vibe' install migrated", result.stdout)
        self.assertIn("mcp__vibe__", result.stdout)

    def test_rerun_is_idempotent(self):
        self.make_legacy()
        self.assertEqual(self.run_install().returncode, 0)
        second = self.run_install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(list(self.servers()).count("delegate"), 1)
        self.assertNotIn("legacy 'vibe' install migrated", second.stdout)

    def test_dry_run_changes_nothing_and_calls_no_claude(self):
        self.make_legacy()
        before = snapshot(self.home)
        result = self.run_install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[dry-run]", result.stdout)
        self.assertIn("would migrate legacy", result.stdout)
        self.assertNotIn("migrated to", result.stdout)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.log.exists())

    def test_skip_existing_keeps_current_command_file(self):
        dest = self.home / ".claude"
        (dest / "commands").mkdir(parents=True)
        (dest / "commands/delegate.md").write_text("mine")
        result = self.run_install("--skip-existing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((dest / "commands/delegate.md").read_text(), "mine")
        self.assertIn("skip (already exists)", result.stdout)

    def test_without_claude_cli_migrates_files_and_prints_manual_steps(self):
        self.make_legacy()
        result = self.run_install(with_claude=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude CLI not found", result.stdout)
        self.assertFalse((self.home / ".claude/commands/vibe.md").exists())
        self.assertIn("vibe", self.servers())

    def test_unknown_option_is_rejected(self):
        self.assertEqual(self.run_install("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
