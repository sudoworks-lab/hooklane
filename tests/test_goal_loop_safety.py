from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP_SCRIPT = REPO_ROOT / "scripts" / "loop.sh"


FAKE_CODEX = textwrap.dedent(
    r"""
    #!/usr/bin/env python3
    import json
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    args = sys.argv[1:]
    prompt = sys.stdin.read()
    capture = Path(os.environ["FAKE_PROMPT_CAPTURE"])
    with capture.open("a", encoding="utf-8") as output:
        output.write("=== PROCESS ===\n")
        output.write(prompt)

    pid_path = os.environ.get("FAKE_PID_FILE")
    if pid_path:
        Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")

    mode = os.environ.get("FAKE_SIGNAL_MODE", "normal")
    if mode in {"terminate", "kill"}:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    if mode == "kill":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    child_pid_path = os.environ.get("FAKE_CHILD_PID_FILE")
    if child_pid_path:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(child_pid_path).write_text(str(child.pid), encoding="utf-8")

    if os.environ.get("FAKE_TEST_STARTED") == "1":
        print(json.dumps({
            "type": "item.started",
            "item": {"type": "command_execution", "command": "python3 -m unittest"},
        }), flush=True)

    def mark_selected_feature_passed():
        features_path = Path(os.environ["FAKE_FEATURES_PATH"])
        document = json.loads(features_path.read_text(encoding="utf-8"))
        for feature in document["features"]:
            marker = f"feature ID: {feature['id']}"
            if marker in prompt:
                feature["passes"] = True
                feature["verified_at"] = "2026-07-11T00:00:00+09:00"
                break
        features_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if os.environ.get("FAKE_MARK_PASS_BEFORE_SLEEP") == "1":
        mark_selected_feature_passed()

    sleep_for = float(os.environ.get("FAKE_SLEEP", "0"))
    if sleep_for:
        time.sleep(sleep_for)

    if os.environ.get("FAKE_MARK_PASS") == "1":
        mark_selected_feature_passed()

    final_index = args.index("--output-last-message") + 1
    Path(args[final_index]).write_text("feature process finished\n", encoding="utf-8")
    print('{"type":"turn.completed"}', flush=True)
    raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
    """
).lstrip()


class GoalLoopSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)
        (self.workspace / "PROMPT.md").write_text(
            (REPO_ROOT / "PROMPT.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        docs = self.workspace / "docs"
        docs.mkdir()
        self.features_path = docs / "features.json"
        self.write_features()
        self.fake_bin = self.workspace / "bin"
        self.fake_bin.mkdir()
        codex = self.fake_bin / "codex"
        codex.write_text(FAKE_CODEX, encoding="utf-8")
        codex.chmod(0o755)
        self.prompt_capture = self.workspace / "prompts.txt"

    def write_features(self, *, first_passes: bool = False) -> None:
        document = {
            "features": [
                {
                    "id": "F001",
                    "title": "first bounded feature",
                    "description": "first acceptance criterion",
                    "steps": ["python3 -m unittest tests.test_first"],
                    "allowed_files": ["src/first.py", "tests/test_first.py"],
                    "prohibited_scope": ["F002"],
                    "passes": first_passes,
                    "verified_at": None,
                    "blocked": False,
                },
                {
                    "id": "F002",
                    "title": "second bounded feature",
                    "description": "second acceptance criterion",
                    "steps": ["python3 -m unittest tests.test_second"],
                    "passes": False,
                    "verified_at": None,
                    "blocked": False,
                },
            ]
        }
        self.features_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_loop(
        self,
        *arguments: str,
        timeout: float = 5,
        **extra_environment: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.fake_bin}{os.pathsep}{env['PATH']}"
        env["FAKE_PROMPT_CAPTURE"] = str(self.prompt_capture)
        env["FAKE_FEATURES_PATH"] = str(self.features_path)
        env.update(extra_environment)
        return subprocess.run(
            ["bash", str(LOOP_SCRIPT), *arguments],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def run_dirs(self) -> list[Path]:
        return sorted((self.workspace / "logs").glob("run-*"))

    def test_runner_injects_exactly_one_selected_feature_contract(self) -> None:
        completed = self.run_loop(
            "codex",
            "1",
            "--iteration-timeout",
            "2",
            FAKE_MARK_PASS="1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        prompt = self.prompt_capture.read_text(encoding="utf-8")
        self.assertIn("feature ID: F001", prompt)
        self.assertIn("first bounded feature", prompt)
        self.assertIn("first acceptance criterion", prompt)
        self.assertIn("次のfeatureを選ばない", prompt)
        self.assertIn("subagentを起動しない", prompt)
        self.assertIn("agent delegationを使わない", prompt)
        self.assertIn("`/goal`を作成しない", prompt)
        self.assertIn("wait_agentを使用しない", prompt)
        self.assertNotIn("feature ID: F002", prompt)
        generated = self.run_dirs()[0] / "iter-001" / "prompt.md"
        self.assertEqual(generated.read_text(encoding="utf-8"), prompt.split("=== PROCESS ===\n", 1)[1])

    def test_two_runner_invocations_select_f001_then_f002(self) -> None:
        first = self.run_loop("codex", "1", FAKE_MARK_PASS="1")
        second = self.run_loop("codex", "1", FAKE_MARK_PASS="1")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        prompts = self.prompt_capture.read_text(encoding="utf-8").split("=== PROCESS ===\n")[1:]
        self.assertEqual(len(prompts), 2)
        self.assertIn("feature ID: F001", prompts[0])
        self.assertNotIn("feature ID: F002", prompts[0])
        self.assertIn("feature ID: F002", prompts[1])

    def test_external_runner_starts_a_new_process_for_next_feature(self) -> None:
        completed = self.run_loop("codex", "2", FAKE_MARK_PASS="1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        prompts = self.prompt_capture.read_text(encoding="utf-8").split("=== PROCESS ===\n")[1:]
        self.assertEqual(len(prompts), 2)
        self.assertIn("feature ID: F001", prompts[0])
        self.assertIn("feature ID: F002", prompts[1])
        run_dir = self.run_dirs()[0]
        self.assertEqual(len(list(run_dir.glob("iter-*"))), 2)
        first = json.loads((run_dir / "iter-001" / "metadata.json").read_text())
        second = json.loads((run_dir / "iter-002" / "metadata.json").read_text())
        self.assertEqual(first["completion_state"], "feature_complete")
        self.assertEqual(second["completion_state"], "complete")

    def test_timeout_stops_run_writes_receipt_and_keeps_feature_false(self) -> None:
        pid_file = self.workspace / "agent.pid"
        child_pid_file = self.workspace / "child.pid"
        completed = self.run_loop(
            "codex",
            "3",
            "--iteration-timeout",
            "0.15",
            "--termination-grace",
            "0.05",
            FAKE_SLEEP="60",
            FAKE_SIGNAL_MODE="kill",
            FAKE_TEST_STARTED="1",
            FAKE_MARK_PASS_BEFORE_SLEEP="1",
            FAKE_PID_FILE=str(pid_file),
            FAKE_CHILD_PID_FILE=str(child_pid_file),
        )

        self.assertNotEqual(completed.returncode, 0)
        run_dir = self.run_dirs()[0]
        self.assertEqual(len(list(run_dir.glob("iter-*"))), 1)
        receipt = json.loads(
            (run_dir / "iter-001" / "timeout-receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["status"], "timed_out")
        self.assertEqual(receipt["recovery_state"], "interrupted_retryable")
        self.assertEqual(receipt["feature_id"], "F001")
        self.assertEqual(receipt["attempt"], 1)
        self.assertEqual(receipt["timeout_limit_seconds"], 0.15)
        self.assertEqual(receipt["termination_method"], "kill")
        self.assertFalse(receipt["feature_state"]["passes"])
        self.assertTrue(receipt["tests_started"])
        self.assertFalse(receipt["tests_completed"])
        self.assertTrue(receipt["retryable"])
        features = json.loads(self.features_path.read_text(encoding="utf-8"))
        self.assertFalse(features["features"][0]["passes"])
        for path in (pid_file, child_pid_file):
            pid = int(path.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_timeout_termination_escalates_only_as_needed(self) -> None:
        for mode, expected_method in (
            ("graceful", "interrupt"),
            ("terminate", "terminate"),
            ("kill", "kill"),
        ):
            with self.subTest(mode=mode):
                if (self.workspace / "logs").exists():
                    before = set(self.run_dirs())
                else:
                    before = set()
                completed = self.run_loop(
                    "codex",
                    "1",
                    "--iteration-timeout",
                    "0.1",
                    "--termination-grace",
                    "0.05",
                    FAKE_SLEEP="60",
                    FAKE_SIGNAL_MODE=mode,
                )
                self.assertNotEqual(completed.returncode, 0)
                run_dir = (set(self.run_dirs()) - before).pop()
                receipt = json.loads(
                    (run_dir / "iter-001" / "timeout-receipt.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["termination_method"], expected_method)

    def test_next_manual_run_resumes_timed_out_feature(self) -> None:
        timed_out = self.run_loop(
            "codex",
            "3",
            "--iteration-timeout",
            "0.1",
            "--termination-grace",
            "0.05",
            FAKE_SLEEP="60",
        )
        resumed = self.run_loop("codex", "1", FAKE_MARK_PASS="1")

        self.assertNotEqual(timed_out.returncode, 0)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        prompts = self.prompt_capture.read_text(encoding="utf-8").split("=== PROCESS ===\n")[1:]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("feature ID: F001" in prompt for prompt in prompts))
        self.assertTrue(all("feature ID: F002" not in prompt for prompt in prompts))

    def test_attempt_limit_is_bounded_and_does_not_advance_feature(self) -> None:
        completed = self.run_loop(
            "codex",
            "10",
            "--max-attempts-per-feature",
            "2",
        )

        self.assertNotEqual(completed.returncode, 0)
        prompts = self.prompt_capture.read_text(encoding="utf-8").split("=== PROCESS ===\n")[1:]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("feature ID: F001" in prompt for prompt in prompts))
        self.assertTrue(all("feature ID: F002" not in prompt for prompt in prompts))
        metadata = json.loads((self.run_dirs()[0] / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["completion_state"], "manual_review")
        self.assertEqual(metadata["attempt"], 2)

    def test_timeout_is_rejected_when_not_positive(self) -> None:
        for option, value in (
            ("--iteration-timeout", "0"),
            ("--termination-grace", "-1"),
            ("--max-attempts-per-feature", "0"),
        ):
            with self.subTest(option=option):
                completed = self.run_loop("codex", "1", option, value)
                self.assertEqual(completed.returncode, 64)
        self.assertFalse((self.workspace / "logs").exists())

    def test_status_and_dry_run_do_not_start_agent_or_write_artifacts(self) -> None:
        status = self.run_loop("codex", "10", "--status")
        dry_run = self.run_loop(
            "codex",
            "10",
            "--dry-run",
            "--iteration-timeout",
            "600",
        )

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("total=2 passed=0 blocked=0 pending=2", status.stdout)
        self.assertIn("[selected] F001", status.stdout)
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn("[selected] F001", dry_run.stdout)
        self.assertIn("timeout=600.0", dry_run.stdout)
        self.assertFalse(self.prompt_capture.exists())
        self.assertFalse((self.workspace / "logs").exists())


if __name__ == "__main__":
    unittest.main()
