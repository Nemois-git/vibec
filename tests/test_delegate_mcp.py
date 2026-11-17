# Copyright (c) 2026 Jeongcheol Kim
# Licensed under the MIT License; see LICENSE at the repository root.
# SPDX-License-Identifier: MIT
"""Hermetic tests for scripts/delegate_mcp.py.

A fake `vibe` CLI (tests/fixtures/fake_vibe.sh) replaces Mistral Vibe, so no
API calls happen. Run from anywhere: python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
SERVER_PATH = REPO / "scripts" / "delegate_mcp.py"
FAKE_VIBE = TESTS_DIR / "fixtures" / "fake_vibe.sh"

ROOT = Path(tempfile.mkdtemp(prefix="delegate-mcp-tests-"))
BIN = ROOT / "bin"
PROJ = ROOT / "proj"
FAKE_VIBE_CMD = f"{BIN / 'vibe'} -p"
SERVER = None


def load_server(path=SERVER_PATH, name="delegate_mcp"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_project():
    BIN.mkdir(parents=True)
    shutil.copy(FAKE_VIBE, BIN / "vibe")
    (BIN / "vibe").chmod(0o755)
    (PROJ / "scripts").mkdir(parents=True)
    (PROJ / ".claude").mkdir()
    shutil.copy(REPO / "scripts" / "delegate.sh", PROJ / "scripts" / "delegate.sh")
    shutil.copy(SERVER_PATH, PROJ / "scripts" / "delegate_mcp.py")
    shutil.copy(REPO / ".mcp.json", PROJ / ".mcp.json")
    (PROJ / ".claude" / "delegate.env").write_text(
        "# test config\nDELEGATE_MODE=smart\nthis line has no equals sign\n"
    )


def setUpModule():
    global SERVER
    build_project()
    os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"
    os.environ["CLAUDE_PROJECT_DIR"] = str(PROJ)
    os.environ.pop("DELEGATE_LOG_DIR", None)
    os.environ.pop("DELEGATE_WRAPPER", None)
    SERVER = load_server()


def tearDownModule():
    subprocess.run(["pkill", "-f", "--", FAKE_VIBE_CMD], capture_output=True)
    shutil.rmtree(ROOT, ignore_errors=True)


def fake_vibe_pids(marker):
    out = subprocess.run(["pgrep", "-f", "--", f"{FAKE_VIBE_CMD} {marker}"], capture_output=True, text=True).stdout
    return out.split()


def wait_until(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rpc(*messages):
    lines = [json.dumps(m) if isinstance(m, dict) else m for m in messages]
    stdin, out = io.StringIO("\n".join(lines) + "\n"), io.StringIO()
    saved_in, saved_argv = sys.stdin, sys.argv
    sys.stdin, sys.argv = stdin, ["delegate_mcp.py"]
    try:
        with contextlib.redirect_stdout(out):
            SERVER.main()
    finally:
        sys.stdin, sys.argv = saved_in, saved_argv
    return [json.loads(line) for line in out.getvalue().splitlines()]


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = SERVER.cli(list(argv))
    return code, json.loads(out.getvalue())


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.log_dir = Path(tempfile.mkdtemp(prefix="logs-", dir=ROOT))
        os.environ["DELEGATE_LOG_DIR"] = str(self.log_dir)
        self.addCleanup(os.environ.pop, "DELEGATE_LOG_DIR", None)
        self.addCleanup(subprocess.run, ["pkill", "-f", "--", f"{FAKE_VIBE_CMD} SLEEP"], capture_output=True)

    def run_dir(self, run):
        return self.log_dir / "runs" / run


class ProtocolTests(ServerTestCase):
    def test_initialize_reports_server_info_and_instructions(self):
        (reply,) = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "delegate-mcp")
        self.assertIn("delegate_run", reply["result"]["instructions"])

    def test_ping_and_tools_list(self):
        ping, listing = rpc({"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(ping["result"], {})
        names = {t["name"] for t in listing["result"]["tools"]}
        self.assertEqual(names, {"delegate_run", "delegate_wait", "delegate_status", "delegate_interrupt", "delegate_doctor"})

    def test_tools_call_status_returns_runs_field(self):
        (reply,) = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "delegate_status", "arguments": {"count": 3}}})
        self.assertIn("runs", reply["result"]["structuredContent"])

    def test_unknown_tool_and_missing_task_return_error_results(self):
        unknown, no_task = rpc(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "delegate_run", "arguments": {}}},
        )
        self.assertTrue(unknown["result"]["isError"])
        self.assertTrue(no_task["result"]["isError"])

    def test_unknown_method_gets_jsonrpc_error(self):
        (reply,) = rpc({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_notifications_bad_json_and_blank_lines_get_no_reply(self):
        replies = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, "not json", "")
        self.assertEqual(replies, [])

    def test_doctor_tool_call_passes_in_fixture_project(self):
        (reply,) = rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "delegate_doctor"}})
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("vibe-cli", reply["result"]["content"][0]["text"])


class HandlerTests(ServerTestCase):
    def test_handler_exception_is_wrapped_as_error_result(self):
        with mock.patch.dict(SERVER.HANDLERS, {"boom": lambda args: 1 / 0}):
            result = SERVER.handle_call({"name": "boom"})
        self.assertTrue(result["isError"])
        self.assertIn("boom failed", result["content"][0]["text"])

    def test_unknown_tool_name_is_error(self):
        self.assertTrue(SERVER.handle_call({"name": "zzz"})["isError"])

    def test_wait_and_interrupt_require_a_run(self):
        self.assertTrue(SERVER.call_wait({})["isError"])
        self.assertTrue(SERVER.call_interrupt({})["isError"])

    def test_delegate_requires_task(self):
        self.assertTrue(SERVER.call_delegate({})["isError"])


class SyncDelegateTests(ServerTestCase):
    def test_flags_reach_wrapper_and_answer_is_returned(self):
        result = SERVER.call_delegate({
            "task": "Reply SYNC_OK", "mode": "smart", "max_price": 0.5, "max_turns": 3,
            "max_tokens": 100, "time_limit": 60, "output": "text", "agent": "a1", "model": "m1",
        })
        sc = result["structuredContent"]
        self.assertFalse(result["isError"])
        self.assertIn("FAKE_ANSWER: Reply SYNC_OK", sc["answer"])
        self.assertEqual(sc["session_id"], "fake-session-1")
        started = read_json(self.run_dir(sc["run"]) / "started.json")
        self.assertEqual((started["mode"], started["model"], started["agent"]), ("smart", "m1", "a1"))

    def test_alias_is_registered_as_finished(self):
        SERVER.call_delegate({"task": "Reply ALIAS", "alias": "sync-alias"})
        self.assertEqual(SERVER.load_aliases()["sync-alias"]["status"], "finished")

    def test_resume_passes_session_id_to_wrapper(self):
        result = SERVER.call_delegate({"task": "follow up", "session_id": "fake-session-1"})
        started = read_json(self.run_dir(result["structuredContent"]["run"]) / "started.json")
        self.assertEqual(started["resume"], "fake-session-1")

    def test_non_numeric_time_limit_is_tolerated(self):
        result = SERVER.call_delegate({"task": "Reply LIMIT_OK", "time_limit": "abc"})
        self.assertEqual(result["structuredContent"]["exit_code"], 0)

    def test_mcp_side_timeout_stops_vibe_and_keeps_run_dir(self):
        # time_limit -115 gives a 5 s MCP-side budget against a task that sleeps.
        result = SERVER.call_delegate({"task": "SLEEP past limit", "time_limit": -115})
        sc = result["structuredContent"]
        self.assertEqual(sc["exit_code"], 124)
        self.assertTrue(result["isError"])
        self.assertNotEqual(sc["run"], "unknown")
        self.assertTrue(self.run_dir(sc["run"]).is_dir())
        self.assertTrue(wait_until(lambda: not fake_vibe_pids("SLEEP past limit")),
                        "vibe child survived the MCP-side timeout")


class BackgroundTests(ServerTestCase):
    def test_background_without_alias_waits_for_real_result(self):
        for i in range(3):
            run = SERVER.call_delegate({"task": f"Reply BG_{i}", "background": True})["structuredContent"]["run"]
            result = SERVER.call_wait({"run": run, "timeout_s": 60})
            self.assertEqual(result["structuredContent"]["status"], "finished")
            self.assertIn(f"FAKE_ANSWER: Reply BG_{i}", result["structuredContent"]["answer"])

    def test_background_with_alias_waits_by_alias(self):
        SERVER.call_delegate({"task": "Reply BG_ALIAS", "background": True, "alias": "bg-alias"})
        result = SERVER.call_wait({"alias": "bg-alias", "timeout_s": 60})
        self.assertIn("FAKE_ANSWER: Reply BG_ALIAS", result["structuredContent"]["answer"])

    def test_dispatch_writes_wrapper_pid(self):
        sc = SERVER.call_delegate({"task": "Reply PIDFILE", "background": True})["structuredContent"]
        pid_file = self.run_dir(sc["run"]) / "dispatch.pid"
        self.assertEqual(int(pid_file.read_text().strip()), sc["pid"])
        SERVER.call_wait({"run": sc["run"], "timeout_s": 60})

    def test_wait_times_out_while_run_is_still_going(self):
        sc = SERVER.call_delegate({"task": "SLEEP still going", "background": True, "alias": "slow"})["structuredContent"]
        result = SERVER.call_wait({"run": "slow", "timeout_s": 1})
        self.assertIn("still running", result["content"][0]["text"])
        SERVER.call_interrupt({"run": sc["run"], "wait_s": 15})
        SERVER.call_wait({"run": sc["run"], "timeout_s": 30})

    def test_status_lists_running_run(self):
        sc = SERVER.call_delegate({"task": "SLEEP listed", "background": True})["structuredContent"]
        self.assertTrue(wait_until(lambda: fake_vibe_pids("SLEEP listed")))
        running = SERVER.call_status({})["structuredContent"]["running"]
        self.assertIn(sc["run"], [r["run"] for r in running])
        SERVER.call_interrupt({"run": sc["run"], "wait_s": 15})
        SERVER.call_wait({"run": sc["run"], "timeout_s": 30})

    def test_interrupt_by_run_without_alias_terminates_vibe(self):
        sc = SERVER.call_delegate({"task": "SLEEP interrupt me", "background": True})["structuredContent"]
        self.assertTrue(wait_until(lambda: fake_vibe_pids("SLEEP interrupt me")))
        interrupted = SERVER.call_interrupt({"run": sc["run"], "wait_s": 15})
        self.assertTrue(interrupted["structuredContent"]["signaled"])
        self.assertFalse(interrupted["isError"])
        result = SERVER.call_wait({"run": sc["run"], "timeout_s": 30})
        self.assertEqual(result["structuredContent"]["status"], "finished")
        self.assertEqual(result["structuredContent"]["outcome"], "terminated")

    def test_interrupt_of_finished_run_reports_nothing_signaled(self):
        sc = SERVER.call_delegate({"task": "Reply DONE", "background": True, "alias": "done"})["structuredContent"]
        SERVER.call_wait({"alias": "done", "timeout_s": 60})
        result = SERVER.call_interrupt({"alias": "done"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["signaled"], [])

    def test_interrupt_of_unknown_run_is_error(self):
        self.assertTrue(SERVER.call_interrupt({"run": "no-such-run"})["isError"])

    def test_run_dir_never_reuses_a_name_in_the_same_second(self):
        base = time.strftime("%Y%m%d-%H%M%S")
        (self.log_dir / "runs" / base).mkdir(parents=True)
        sc = SERVER.call_delegate({"task": "Reply COLLIDE", "background": True})["structuredContent"]
        self.assertNotEqual(sc["run"], base)
        self.assertTrue(self.run_dir(sc["run"]).is_dir())
        SERVER.call_wait({"run": sc["run"], "timeout_s": 60})


class WaitEdgeTests(ServerTestCase):
    def test_unknown_run_is_synthesized_as_finished(self):
        result = SERVER.call_wait({"run": "nope", "timeout_s": 1})
        self.assertIn("no meta.json", result["structuredContent"]["status"])

    def test_export_only_run_is_synthesized_from_export(self):
        run_dir = self.run_dir("exp-only")
        run_dir.mkdir(parents=True)
        (run_dir / "export.json").write_text('{"session_id": "s9", "exit_code": 0, "outcome": "success"}')
        (run_dir / "started.json").write_text('{"mode": "ask"}')
        result = SERVER.call_wait({"run": "exp-only", "timeout_s": 1})
        self.assertEqual(result["structuredContent"]["session_id"], "s9")
        self.assertIn("no meta.json", result["structuredContent"]["status"])

    def test_corrupt_meta_and_started_are_tolerated(self):
        run_dir = self.run_dir("corrupt")
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text("{bad")
        (run_dir / "started.json").write_text("{bad")
        result = SERVER.call_wait({"run": "corrupt", "timeout_s": 1})
        self.assertIn("status", result["structuredContent"])

    def test_bad_timeout_falls_back_to_default_without_blocking(self):
        started = time.time()
        SERVER.call_wait({"run": "nope", "timeout_s": "bad"})
        self.assertLess(time.time() - started, 5)

    def test_resolve_run_handles_timestamp_alias_and_unknown(self):
        self.assertEqual(SERVER.resolve_run(""), "")
        self.assertEqual(SERVER.resolve_run("zzz-unknown"), "zzz-unknown")
        (self.log_dir / "runs" / "20250101-000000").mkdir(parents=True)
        self.assertEqual(SERVER.resolve_run("20250101-000000"), "20250101-000000")
        SERVER.register_alias("named", "20250101-000000", self.run_dir("20250101-000000"))
        self.assertEqual(SERVER.resolve_run("named"), "20250101-000000")


class HelperTests(ServerTestCase):
    def test_result_from_run_falls_back_to_stderr_when_answer_is_empty(self):
        result = SERVER.result_from_run(Path(self.log_dir), {"run": "r", "mode": "ask", "duration_s": 3},
                                        "", "boom on stderr", 1, time.time())
        self.assertIn("boom on stderr", result["content"][0]["text"])
        self.assertTrue(result["isError"])

    def test_register_alias_ignores_missing_alias(self):
        SERVER.register_alias(None, "x", self.log_dir)
        self.assertEqual(SERVER.load_aliases(), {})

    def test_pid_alive_for_self_dead_and_permission_denied(self):
        self.assertTrue(SERVER.pid_alive(os.getpid()))
        self.assertFalse(SERVER.pid_alive(999999))
        self.assertTrue(SERVER.pid_alive(1))

    def test_reap_is_safe_without_children(self):
        SERVER.reap()

    def test_corrupt_aliases_file_reads_as_empty(self):
        (self.log_dir / "aliases.json").write_text("garbage")
        self.assertEqual(SERVER.load_aliases(), {})

    def test_read_export_missing_and_corrupt(self):
        self.assertEqual(SERVER.read_export(self.log_dir / "nowhere"), {})
        (self.log_dir / "runs" / "bad").mkdir(parents=True)
        (self.log_dir / "runs" / "bad" / "export.json").write_text("{")
        self.assertEqual(SERVER.read_export(self.run_dir("bad")), {})


class StatusTests(ServerTestCase):
    def test_empty_state_reports_no_runs_and_no_aliases(self):
        text = SERVER.call_status({})["content"][0]["text"]
        self.assertIn("(no runs yet)", text)
        self.assertIn("(none)", text)

    def test_non_numeric_count_falls_back_to_default(self):
        self.assertFalse(SERVER.call_status({"count": "x"}).get("isError", False))

    def test_newest_export_is_reported_after_a_run(self):
        SERVER.call_delegate({"task": "Reply STATUS"})
        newest = SERVER.call_status({})["structuredContent"]["newest"]
        self.assertEqual(newest["session_id"], "fake-session-1")

    def test_short_rows_and_runs_without_export_are_tolerated(self):
        (self.log_dir / "runs.log").write_text(
            "short\nghost-no-export\t0\t1\task\tdefault\t-\t\t\tpreview\n"
        )
        sc = SERVER.call_status({"count": 50})["structuredContent"]
        self.assertIn("short", [r["run"] for r in sc["runs"]])
        self.assertIsNone(sc["newest"])

    def test_corrupt_started_json_is_skipped_in_running_list(self):
        (self.log_dir / "runs" / "corrupt-started").mkdir(parents=True)
        (self.log_dir / "runs" / "corrupt-started" / "started.json").write_text("{bad")
        self.assertEqual(SERVER.call_status({})["structuredContent"]["running"], [])


class CliTests(ServerTestCase):
    def test_delegate_sync_exits_zero(self):
        code, result = run_cli("delegate", "CLI sync", "--mode", "smart", "--max-price", "0.5",
                               "--max-turns", "2", "--max-tokens", "50", "--time-limit", "60",
                               "--output", "json", "--agent", "a", "--model", "m", "--alias", "clisync")
        self.assertEqual(code, 0)
        self.assertIn("FAKE_ANSWER: CLI sync", result["structuredContent"]["answer"])

    def test_delegate_resume_exits_zero(self):
        code, _ = run_cli("delegate", "CLI follow", "--session-id", "fake-session-1", "--mode", "smart")
        self.assertEqual(code, 0)

    def test_background_delegate_then_wait(self):
        code, _ = run_cli("delegate", "CLI bg", "--background", "--alias", "clibg")
        self.assertEqual(code, 0)
        code, result = run_cli("wait", "clibg", "--timeout", "60")
        self.assertEqual(code, 0)
        self.assertIn("FAKE_ANSWER: CLI bg", result["structuredContent"]["answer"])

    def test_status_exits_zero_and_interrupt_unknown_exits_one(self):
        self.assertEqual(run_cli("status", "--count", "2")[0], 0)
        self.assertEqual(run_cli("interrupt", "no-such-run", "--wait", "1")[0], 1)

    def test_doctor_exits_zero_in_fixture_project(self):
        code, _ = run_cli("doctor")
        self.assertEqual(code, 0)

    def test_main_with_argv_runs_cli_and_exits_zero(self):
        saved = sys.argv
        sys.argv = ["delegate_mcp.py", "status", "--count", "1"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
                SERVER.main()
        finally:
            sys.argv = saved
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("structuredContent", out.getvalue())


class ConfigAndResolutionTests(ServerTestCase):
    def test_config_file_skips_comments_and_malformed_lines(self):
        config = SERVER.config()
        self.assertEqual(config.get("DELEGATE_MODE"), "smart")
        self.assertNotIn("this line has no equals sign", config)

    def test_project_dir_from_cwd_with_scripts_dir(self):
        self.assertEqual(self._load_from(PROJ).PROJECT_DIR.resolve(), PROJ.resolve())

    def test_project_dir_falls_back_to_repo_parent_of_script(self):
        self.assertEqual(self._load_from(ROOT).PROJECT_DIR.resolve(), REPO.resolve())

    def test_project_dir_falls_back_to_script_directory(self):
        standalone = ROOT / "standalone"
        standalone.mkdir()
        shutil.copy(SERVER_PATH, standalone / "delegate_mcp.py")
        module = self._load_from(ROOT, path=standalone / "delegate_mcp.py")
        self.assertEqual(module.PROJECT_DIR.resolve(), standalone.resolve())
        self.assertEqual(module.WRAPPER.resolve(), (standalone / "scripts" / "delegate.sh").resolve())

    def test_wrapper_env_override_is_used(self):
        override = PROJ / "scripts" / "delegate.sh"
        with mock.patch.dict(os.environ, {"DELEGATE_WRAPPER": str(override)}):
            module = load_server(name="vm_env_wrapper")
        self.assertEqual(module.WRAPPER, override)

    def _load_from(self, cwd, path=SERVER_PATH):
        saved_cwd = os.getcwd()
        saved_env = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            os.chdir(cwd)
            return load_server(path, name="vm_resolution")
        finally:
            os.chdir(saved_cwd)
            if saved_env is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = saved_env


class ErrorPathTests(ServerTestCase):
    def test_response_objects_without_method_get_no_reply(self):
        self.assertEqual(rpc({"jsonrpc": "2.0", "id": 9, "result": {}}), [])

    def test_corrupt_dispatch_pid_is_ignored(self):
        run_dir = self.run_dir("garbled")
        run_dir.mkdir(parents=True)
        (run_dir / "dispatch.pid").write_text("garbage")
        self.assertEqual(SERVER.run_pids("garbled"), [])

    def test_stop_process_group_tolerates_already_exited_group(self):
        proc = subprocess.Popen(["true"], start_new_session=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        proc.wait()
        SERVER.stop_process_group(proc)

    def test_stop_process_group_escalates_to_sigkill_when_sigterm_is_ignored(self):
        stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        proc = subprocess.Popen([sys.executable, "-c", stubborn], start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)
        with mock.patch.object(SERVER, "STOP_GRACE_S", 1):
            SERVER.stop_process_group(proc)
        self.assertIsNotNone(proc.poll())

    def test_missing_wrapper_reports_error_with_unknown_run_dir(self):
        with mock.patch.object(SERVER, "WRAPPER", ROOT / "missing-wrapper.sh"):
            result = SERVER.call_delegate({"task": "Reply NOWRAP"})
        self.assertTrue(result["isError"])
        self.assertTrue(result["structuredContent"]["run_dir"].endswith("unknown"))

    def test_unlink_failure_of_meta_tempfile_is_ignored(self):
        with mock.patch.object(SERVER.os, "unlink", side_effect=OSError("busy")):
            result = SERVER.call_delegate({"task": "Reply UNLINK"})
        self.assertEqual(result["structuredContent"]["exit_code"], 0)

    def test_synthesized_wait_updates_alias_registry(self):
        run_dir = self.run_dir("exp-alias")
        run_dir.mkdir(parents=True)
        (run_dir / "export.json").write_text('{"session_id": "s7", "exit_code": 0}')
        SERVER.register_alias("exp-name", "exp-alias", run_dir)
        SERVER.call_wait({"alias": "exp-name", "timeout_s": 1})
        self.assertTrue(SERVER.load_aliases()["exp-name"]["status"].startswith("finished"))

    def test_interrupt_tolerates_non_numeric_wait(self):
        self.assertTrue(SERVER.call_interrupt({"run": "no-such-run", "wait_s": "x"})["isError"])

    def test_interrupt_tolerates_process_that_exits_before_signal(self):
        with mock.patch.object(SERVER, "run_pids", return_value=[999999]):
            result = SERVER.call_interrupt({"run": "ghost", "wait_s": 1})
        self.assertEqual(result["structuredContent"]["signaled"], [])
        self.assertTrue(result["isError"])


class ModeAndDescriptionTests(ServerTestCase):
    def test_mode_enum_offers_only_smart_and_auto(self):
        self.assertEqual(SERVER.DELEGATE_SCHEMA["properties"]["mode"]["enum"], ["smart", "auto"])

    def test_wrapper_rejects_ask_mode(self):
        result = SERVER.call_delegate({"task": "Reply ASK", "mode": "ask"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["exit_code"], 2)

    def test_cli_rejects_ask_mode(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_cli("delegate", "x", "--mode", "ask")

    def test_tool_descriptions_name_the_running_server_not_a_fixed_install(self):
        tools = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]
        by_name = {t["name"]: t["description"] for t in tools}
        self.assertIn("python3 " + shlex.quote(str(SERVER_PATH.resolve())), by_name["delegate_run"])
        for description in by_name.values():
            self.assertNotIn("~/.claude/mcp", description)
            self.assertNotIn("{CLI}", description)

    def test_delegate_wait_description_points_async_callers_at_the_cli(self):
        tools = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})[0]["result"]["tools"]
        wait = next(t["description"] for t in tools if t["name"] == "delegate_wait")
        self.assertIn("use the CLI form", wait)

    def test_instructions_name_the_server_cli_and_failure_handling(self):
        reply = rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})[0]
        instructions = reply["result"]["instructions"]
        self.assertIn("python3 " + shlex.quote(str(SERVER_PATH.resolve())), instructions)
        self.assertNotIn("{CLI}", instructions)
        self.assertIn("isError", instructions)


if __name__ == "__main__":
    unittest.main()
