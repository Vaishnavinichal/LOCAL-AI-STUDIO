import os
import tempfile
import time
import unittest
from pathlib import Path
from pydantic import ValidationError

from app import main


class TerminalPolicyTests(unittest.TestCase):
    def test_destructive_commands_are_rejected(self):
        for command in ("Remove-Item . -Recurse -Force", "format D:", "shutdown /s", "reg delete HKLM\\Software"):
            with self.subTest(command=command), self.assertRaises(main.HTTPException):
                main._terminal_blocked(command)

    def test_history_recovery_marks_unfinished_process_as_interrupted(self):
        with tempfile.TemporaryDirectory() as temp:
            original = main.DATA_ROOT
            main.DATA_ROOT = Path(temp)
            try:
                main._terminal_record({"execution_id": "unfinished", "status": "running", "command": "python -m build"})
                main._recover_terminal_history()
                rows = main.coding_terminal_history()
                self.assertEqual(rows[0]["status"], "interrupted")
            finally:
                main.DATA_ROOT = original

    def test_zero_timeout_is_valid_only_for_interactive_terminal_preview(self):
        interactive = main.CodingTerminalPreview(command="python -i", timeout=0, interactive=True)
        self.assertEqual(interactive.timeout, 0)
        self.assertTrue(interactive.interactive)
        with self.assertRaises(ValidationError):
            main.CodingTerminalPreview(command="Write-Output ok", timeout=0, interactive=False)

    def test_positive_timeouts_remain_valid_for_both_execution_modes(self):
        noninteractive = main.CodingTerminalPreview(command="Write-Output ok", timeout=30)
        interactive = main.CodingTerminalPreview(command="python -i", timeout=30, interactive=True)
        self.assertEqual(noninteractive.timeout, 30)
        self.assertEqual(interactive.timeout, 30)

    def test_terminal_input_rejects_unknown_execution_ids(self):
        with self.assertRaises(main.HTTPException) as error:
            main.send_coding_terminal_input("missing-session", main.CodingTerminalInput(input="hello"))
        self.assertEqual(error.exception.status_code, 404)


@unittest.skipUnless(os.name == "nt", "PowerShell integration requires Windows")
class WindowsTerminalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sample-project"
        self.root.mkdir()
        self.previous_workspace = main.ACTIVE_CODING_WORKSPACE
        self.previous_data_root = main.DATA_ROOT
        main.ACTIVE_CODING_WORKSPACE = self.root
        main.DATA_ROOT = Path(self.temp.name) / "app-data"
        self.started_ids = []

    def tearDown(self):
        for execution_id in self.started_ids:
            try:
                result = main.get_coding_terminal_execution(execution_id)
                if result["status"] in {"running", "cancel_requested"}:
                    main.stop_coding_terminal_execution(execution_id)
                    self.wait_for(execution_id, timeout=8)
            except Exception:
                pass
        main.ACTIVE_CODING_WORKSPACE = self.previous_workspace
        main.DATA_ROOT = self.previous_data_root
        self.temp.cleanup()

    def wait_for(self, execution_id, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = main.get_coding_terminal_execution(execution_id)
            if result["status"] not in {"running", "cancel_requested"}:
                return result
            time.sleep(0.1)
        self.fail("PowerShell process did not finish before test timeout")

    def wait_for_output(self, execution_id, text, timeout=8, stream="stdout"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = main.get_coding_terminal_execution(execution_id)
            if text in result[stream]: return result
            if result["status"] not in {"running", "cancel_requested"}:
                self.fail(f"Process finished before producing {text!r}: {result}")
            time.sleep(.05)
        self.fail(f"Process did not produce {text!r} before timeout")

    def start_interactive(self, command, timeout=0):
        proposal = main.preview_coding_terminal(main.CodingTerminalPreview(command=command, timeout=timeout, interactive=True, reason="Interactive terminal integration test"))
        self.assertTrue(proposal["approval_required"])
        self.assertTrue(proposal["interactive"])
        self.assertEqual(proposal["timeout_seconds"], timeout)
        started = main.execute_coding_terminal(proposal["id"])
        self.started_ids.append(started["execution_id"])
        self.assertEqual(started["status"], "running")
        self.assertTrue(started["interactive"])
        return started["execution_id"]

    def test_review_execute_and_record_a_real_project_file(self):
        command = "Set-Content -LiteralPath result.txt -Value 'phase5-ok'; Write-Output 'terminal-output-ok'"
        proposal = main.preview_coding_terminal(main.CodingTerminalPreview(command=command, reason="Integration test"))
        self.assertFalse((self.root / "result.txt").exists(), "Preview must not run a command")
        started = main.execute_coding_terminal(proposal["id"])
        result = self.wait_for(started["execution_id"])
        self.assertEqual(result["status"], "completed", result["stderr"])
        self.assertIn("terminal-output-ok", result["stdout"])
        self.assertEqual((self.root / "result.txt").read_text(encoding="utf-8").strip(), "phase5-ok")
        self.assertTrue(any(row.get("execution_id") == started["execution_id"] for row in main.coding_terminal_history()))

    def test_invalid_path_and_cancellation_of_owned_process(self):
        with self.assertRaises(main.HTTPException):
            main.preview_coding_terminal(main.CodingTerminalPreview(command="Write-Output no", relative_path=".."))
        proposal = main.preview_coding_terminal(main.CodingTerminalPreview(command="Start-Sleep -Seconds 20", timeout=30))
        started = main.execute_coding_terminal(proposal["id"])
        main.stop_coding_terminal_execution(started["execution_id"])
        result = self.wait_for(started["execution_id"])
        self.assertEqual(result["status"], "cancelled")

    def test_timeout_stops_an_overlong_command(self):
        proposal = main.preview_coding_terminal(main.CodingTerminalPreview(command="Start-Sleep -Seconds 8", timeout=1))
        started = main.execute_coding_terminal(proposal["id"])
        result = self.wait_for(started["execution_id"], timeout=5)
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["timed_out"])

    def test_interactive_sequential_and_multiline_input_streams_both_outputs(self):
        code = "import sys; sys.stdout.write('READY1'); sys.stdout.flush(); first=input(); print('ACK1='+first, flush=True); print('READY2', file=sys.stderr, flush=True); second=input(); third=input(); print('ACK2='+second, flush=True); print('ACK3='+third, flush=True)"
        execution_id = self.start_interactive(f'python -u -c "{code}"')
        self.wait_for_output(execution_id, "READY1")
        main.send_coding_terminal_input(execution_id, main.CodingTerminalInput(input="alpha\n"))
        self.wait_for_output(execution_id, "ACK1=alpha")
        self.wait_for_output(execution_id, "READY2", stream="stderr")
        main.send_coding_terminal_input(execution_id, main.CodingTerminalInput(input="beta\ngamma\n"))
        result = self.wait_for(execution_id)
        self.assertEqual(result["status"], "completed", result["stderr"])
        self.assertIn("ACK2=beta", result["stdout"])
        self.assertIn("ACK3=gamma", result["stdout"])
        self.assertIn("READY2", result["stderr"])
        self.assertTrue(any(item["execution_id"] == execution_id for item in main.list_coding_terminal_executions()))

    def test_eof_reaches_interactive_process_and_closes_stdin(self):
        code = "import sys; line=sys.stdin.readline(); print('EOF_RECEIVED='+str(line=='') , flush=True)"
        execution_id = self.start_interactive(f'python -u -c "{code}"')
        main.send_coding_terminal_input(execution_id, main.CodingTerminalInput(eof=True))
        result = self.wait_for(execution_id)
        self.assertEqual(result["status"], "completed", result["stderr"])
        self.assertIn("EOF_RECEIVED=True", result["stdout"])
        with self.assertRaises(main.HTTPException) as error:
            main.send_coding_terminal_input(execution_id, main.CodingTerminalInput(input="too late"))
        self.assertEqual(error.exception.status_code, 409)

    def test_interactive_powershell_reads_input_from_stdin(self):
        execution_id = self.start_interactive("$answer = [Console]::In.ReadLine(); Write-Output ('PS_ACK=' + $answer)")
        main.send_coding_terminal_input(execution_id, main.CodingTerminalInput(input="powershell-input\n"))
        result = self.wait_for(execution_id)
        self.assertEqual(result["status"], "completed", result["stderr"])
        self.assertIn("PS_ACK=powershell-input", result["stdout"])

    def test_streaming_reader_redacts_secrets_split_across_chunks(self):
        code = "import sys,time; sys.stdout.write('token='); sys.stdout.flush(); time.sleep(.15); print('stream-secret-value', flush=True)"
        execution_id = self.start_interactive(f'python -u -c "{code}"')
        result = self.wait_for(execution_id)
        self.assertEqual(result["status"], "completed", result["stderr"])
        self.assertIn("token=[REDACTED]", result["stdout"])
        self.assertNotIn("stream-secret-value", result["stdout"])

    def test_interactive_process_crash_keeps_output_and_exit_status(self):
        code = "import sys; print('crash-output', flush=True); print('crash-error', file=sys.stderr, flush=True); sys.exit(7)"
        execution_id = self.start_interactive(f'python -u -c "{code}"')
        result = self.wait_for(execution_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("crash-output", result["stdout"])
        self.assertIn("crash-error", result["stderr"])

    def test_interactive_process_has_no_timeout_and_stop_cleans_child_tree(self):
        code = "import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(child.pid, flush=True); time.sleep(30)"
        execution_id = self.start_interactive(f'python -u -c "{code}"', timeout=0)
        result = self.wait_for_output(execution_id, "\n")
        time.sleep(1.2)
        self.assertEqual(main.get_coding_terminal_execution(execution_id)["status"], "running")
        child_pid = int(result["stdout"].strip().splitlines()[0])
        self.assertTrue(main.stop_coding_terminal_execution(execution_id)["stopping"])
        stopped = self.wait_for(execution_id)
        self.assertEqual(stopped["status"], "cancelled")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try: os.kill(child_pid, 0)
            except OSError: break
            time.sleep(.1)
        else: self.fail("Interactive child process was orphaned after its parent was stopped")


if __name__ == "__main__":
    unittest.main()
