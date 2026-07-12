#!/usr/bin/env python3
"""Run bounded, one-feature Goal Loop processes with durable artifacts."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Never, cast


PROMISE_DONE = "<promise>ALL_FEATURES_PASS</promise>"
PROMISE_BLOCKED = "<promise>BLOCKED</promise>"
DEFAULT_ITERATION_TIMEOUT_SECONDS = 1800.0
DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS_PER_FEATURE = 1
TIMEOUT_EXIT_CODE = 4
ACTIVE_AGENT: subprocess.Popen[bytes] | None = None
RECEIVED_SIGNAL: int | None = None


class UsageErrorParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    started_at: datetime
    ended_at: datetime
    elapsed_seconds: float
    timed_out: bool
    timeout_at: datetime | None
    termination_method: str | None


@dataclass(frozen=True)
class IterationOutcome:
    state: str
    runner_exit_code: int | None
    signal_name: str | None
    interruption_reason: str | None
    retryable: bool
    human_action: str | None


def send_process_group_signal(
    process: subprocess.Popen[bytes],
    signal_number: int,
) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal_number)
        elif signal_number == signal.SIGINT and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal_number)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def handle_signal(signal_number: int, _frame: object) -> None:
    global RECEIVED_SIGNAL
    if RECEIVED_SIGNAL is None:
        RECEIVED_SIGNAL = signal_number
    process = ACTIVE_AGENT
    if process is not None:
        send_process_group_signal(process, signal_number)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not parsed > 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = UsageErrorParser(description=__doc__)
    parser.add_argument(
        "agent",
        nargs="?",
        choices=("claude", "codex"),
        default="claude",
    )
    parser.add_argument(
        "max_iterations",
        nargs="?",
        type=positive_integer,
        default=10,
        help="maximum number of fresh agent processes in this runner invocation",
    )
    parser.add_argument(
        "--iteration-timeout",
        type=positive_number,
        default=os.environ.get(
            "GOAL_LOOP_ITERATION_TIMEOUT",
            str(DEFAULT_ITERATION_TIMEOUT_SECONDS),
        ),
        metavar="SECONDS",
        help="wall-clock limit for each agent process (default: 1800)",
    )
    parser.add_argument(
        "--termination-grace",
        type=positive_number,
        default=os.environ.get(
            "GOAL_LOOP_TERMINATION_GRACE",
            str(DEFAULT_TERMINATION_GRACE_SECONDS),
        ),
        metavar="SECONDS",
        help="wait after interrupt and terminate before escalation (default: 5)",
    )
    parser.add_argument(
        "--max-attempts-per-feature",
        type=positive_integer,
        default=os.environ.get(
            "GOAL_LOOP_MAX_ATTEMPTS_PER_FEATURE",
            str(DEFAULT_MAX_ATTEMPTS_PER_FEATURE),
        ),
        metavar="COUNT",
        help="bounded attempts for one feature in a single run (default: 1)",
    )
    inspection = parser.add_mutually_exclusive_group()
    inspection.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and show the selected feature without artifacts or agent execution",
    )
    inspection.add_argument(
        "--status",
        action="store_true",
        help="report feature counts and the next selected feature without artifacts or agent execution",
    )
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def create_run_dir(logs_dir: Path) -> Path:
    stem = f"run-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    candidate = logs_dir / stem
    suffix = 0
    while True:
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            suffix += 1
            candidate = logs_dir / f"{stem}-{suffix}"


def git_command(workspace: Path, arguments: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip("\n")


def git_snapshot(workspace: Path) -> tuple[str | None, str]:
    head = git_command(workspace, ["rev-parse", "--verify", "HEAD"])
    status = git_command(workspace, ["status", "--short"])
    return head, status or ""


def git_snapshot_text(head: str | None, status: str) -> str:
    return f"head: {head or 'null'}\nstatus:\n{status}\n"


def load_feature_document(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("features"), list):
        raise ValueError("docs/features.json must contain a features array")
    for index, feature in enumerate(document["features"]):
        if not isinstance(feature, dict) or not isinstance(feature.get("id"), str):
            raise ValueError(f"features[{index}] must contain a string id")
    return document


def pending_feature(document: dict[str, Any]) -> dict[str, Any] | None:
    for feature in document["features"]:
        if feature.get("passes") is not True and feature.get("blocked") is not True:
            return cast(dict[str, Any], feature)
    return None


def feature_by_id(document: dict[str, Any], feature_id: str) -> dict[str, Any] | None:
    for feature in document["features"]:
        if feature.get("id") == feature_id:
            return cast(dict[str, Any], feature)
    return None


def feature_state(feature: dict[str, Any] | None) -> dict[str, bool | None]:
    if feature is None:
        return {"passes": None, "blocked": None}
    return {
        "passes": feature.get("passes") is True,
        "blocked": feature.get("blocked") is True,
    }


def keep_timed_out_feature_pending(
    document: dict[str, Any],
    feature_id: str,
    features_path: Path,
) -> dict[str, Any] | None:
    feature = feature_by_id(document, feature_id)
    if feature is None:
        return None
    if feature.get("passes") is True or feature.get("verified_at") is not None:
        feature["passes"] = False
        feature["verified_at"] = None
        features_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return feature


def all_features_pass(document: dict[str, Any]) -> bool:
    features = document["features"]
    return bool(features) and all(feature.get("passes") is True for feature in features)


def all_remaining_features_blocked(document: dict[str, Any]) -> bool:
    incomplete = [feature for feature in document["features"] if feature.get("passes") is not True]
    return bool(incomplete) and all(feature.get("blocked") is True for feature in incomplete)


def feature_counts(document: dict[str, Any]) -> dict[str, int]:
    features = document["features"]
    return {
        "total": len(features),
        "passed": sum(feature.get("passes") is True for feature in features),
        "blocked": sum(
            feature.get("passes") is not True and feature.get("blocked") is True
            for feature in features
        ),
        "pending": sum(
            feature.get("passes") is not True and feature.get("blocked") is not True
            for feature in features
        ),
    }


def print_status(document: dict[str, Any]) -> None:
    counts = feature_counts(document)
    selected = pending_feature(document)
    print(
        "[features] "
        f"total={counts['total']} passed={counts['passed']} "
        f"blocked={counts['blocked']} pending={counts['pending']}"
    )
    print(f"[selected] {selected['id'] if selected is not None else 'none'}")


def display_list(value: Any, fallback: str) -> str:
    if isinstance(value, list) and value:
        return "\n".join(f"  - {item}" for item in value)
    if isinstance(value, str) and value.strip():
        return f"  - {value.strip()}"
    return f"  - {fallback}"


def build_iteration_prompt(
    base_prompt: str,
    feature: dict[str, Any],
    *,
    attempt: int,
    evidence_destination: Path,
) -> str:
    feature_id = feature["id"]
    title = feature.get("title") or feature.get("description") or feature_id
    acceptance = feature.get("acceptance_criteria") or feature.get("description")
    tests = feature.get("test_expectations") or feature.get("steps")
    allowed = feature.get("allowed_files") or feature.get("scope")
    prohibited = feature.get("prohibited_scope")
    contract = f"""# Runner-assigned bounded feature

このprocessで扱うfeatureは指定された1件だけです。

- feature ID: {feature_id}
- title: {title}
- current attempt: {attempt}
- evidence destination: {evidence_destination}
- acceptance criteria:
{display_list(acceptance, "docs/features.jsonの指定featureに記載された条件")}
- test expectations:
{display_list(tests, "指定featureのstepsとdocs/PLAN.mdの対応テスト")}
- allowed files / scope:
{display_list(allowed, "指定featureを完了するために必要な最小範囲")}
- prohibited scope:
{display_list(prohibited, "指定feature以外のfeatureと無関係な変更")}

## Process境界（最優先）

- 指定feature以外を実装しない。
- 次のfeatureを選ばない。
- subagentを起動しない。
- agent delegationを使わない。
- `/goal`を作成しない。
- wait_agentを使用しない。
- reviewerや監査agentをクリティカルパスへ追加しない。
- feature完了後は最終報告を出してprocessを終了する。
- プロジェクト全体の完了まで同一turnを継続しない。
- 別featureの問題を発見した場合は変更せず、follow-up候補として最終報告に記録する。
- 同じprocessで別featureへ進んだり、親turn内でGoal Loopを再実装したりしない。

"""
    return contract + base_prompt


def extract_codex_thread_id(events_path: Path) -> str | None:
    if not events_path.is_file():
        return None
    with events_path.open(encoding="utf-8", errors="replace") as events:
        for line in events:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "thread.started":
                continue
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


def test_activity(agent: str, iteration_dir: Path) -> tuple[bool, bool]:
    path = iteration_dir / ("events.jsonl" if agent == "codex" else "stdout.log")
    if not path.is_file():
        return False, False
    started = False
    completed = False
    markers = ("pytest", "unittest", "npm test", "pnpm test", "yarn test", "cargo test", "go test")
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            lowered = line.lower()
            if not any(marker in lowered for marker in markers):
                continue
            if "completed" in lowered or "finished" in lowered or "tool_call_finish" in lowered:
                completed = True
            else:
                started = True
    return started or completed, completed


def artifact_paths(agent: str, run_dir: Path, iteration_dir: Path) -> dict[str, str]:
    paths = {
        "prompt": str(iteration_dir / "prompt.md"),
        "final": str(iteration_dir / "final.md"),
        "stderr": str(iteration_dir / "stderr.log"),
        "iteration_metadata": str(iteration_dir / "metadata.json"),
        "timeout_receipt": str(iteration_dir / "timeout-receipt.json"),
        "run_metadata": str(run_dir / "metadata.json"),
        "summary": str(run_dir / "summary.txt"),
        "git_before": str(run_dir / "git-before.txt"),
        "git_after": str(run_dir / "git-after.txt"),
    }
    paths["events" if agent == "codex" else "stdout"] = str(
        iteration_dir / ("events.jsonl" if agent == "codex" else "stdout.log")
    )
    return paths


def wait_after_signal(process: subprocess.Popen[bytes], seconds: float) -> bool:
    try:
        process.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def terminate_timed_out_process(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
) -> tuple[int | None, str]:
    send_process_group_signal(process, signal.SIGINT)
    if wait_after_signal(process, grace_seconds):
        return process.returncode, "interrupt"

    if os.name == "posix":
        send_process_group_signal(process, signal.SIGTERM)
    else:
        try:
            process.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if wait_after_signal(process, grace_seconds):
        return process.returncode, "terminate"

    if os.name == "posix":
        send_process_group_signal(process, signal.SIGKILL)
    else:
        try:
            process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    wait_after_signal(process, grace_seconds)
    return process.poll(), "kill"


def run_agent_process(
    command: list[str],
    prompt_stream: BinaryIO,
    stdout_stream: BinaryIO,
    stderr_stream: BinaryIO,
    *,
    timeout_seconds: float,
    grace_seconds: float,
) -> ProcessResult:
    global ACTIVE_AGENT
    popen_options: dict[str, Any] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    started_at = utc_now()
    monotonic_started = time.monotonic()
    process: subprocess.Popen[bytes] = subprocess.Popen(
        command,
        stdin=prompt_stream,
        stdout=stdout_stream,
        stderr=stderr_stream,
        **popen_options,
    )
    ACTIVE_AGENT = process
    if RECEIVED_SIGNAL is not None:
        send_process_group_signal(process, RECEIVED_SIGNAL)
    try:
        try:
            exit_code: int | None = process.wait(timeout=timeout_seconds)
            timeout_at = None
            timed_out = False
            termination_method = None
        except subprocess.TimeoutExpired:
            timeout_at = utc_now()
            timed_out = True
            exit_code, termination_method = terminate_timed_out_process(
                process,
                grace_seconds,
            )
        ended_at = utc_now()
        return ProcessResult(
            exit_code=exit_code,
            started_at=started_at,
            ended_at=ended_at,
            elapsed_seconds=round(time.monotonic() - monotonic_started, 6),
            timed_out=timed_out,
            timeout_at=timeout_at,
            termination_method=termination_method,
        )
    finally:
        ACTIVE_AGENT = None


def missing_executable_result(started_at: datetime, exit_code: int = 127) -> ProcessResult:
    ended_at = utc_now()
    return ProcessResult(
        exit_code=exit_code,
        started_at=started_at,
        ended_at=ended_at,
        elapsed_seconds=max(0.0, (ended_at - started_at).total_seconds()),
        timed_out=False,
        timeout_at=None,
        termination_method=None,
    )


def run_codex(
    prompt: Path,
    iteration_dir: Path,
    *,
    timeout_seconds: float,
    grace_seconds: float,
) -> ProcessResult:
    final_message = iteration_dir / "final.md"
    events = iteration_dir / "events.jsonl"
    stderr = iteration_dir / "stderr.log"
    command = [
        "codex",
        "exec",
        "--json",
        "--output-last-message",
        str(final_message),
        "--sandbox",
        "workspace-write",
        "-",
    ]
    final_message.touch()
    started_at = utc_now()
    try:
        with (
            prompt.open("rb") as prompt_stream,
            events.open("wb") as stdout_stream,
            stderr.open("wb") as stderr_stream,
        ):
            return run_agent_process(
                command,
                prompt_stream,
                stdout_stream,
                stderr_stream,
                timeout_seconds=timeout_seconds,
                grace_seconds=grace_seconds,
            )
    except FileNotFoundError as error:
        stderr.write_text(f"{error}\n", encoding="utf-8")
        events.touch(exist_ok=True)
        return missing_executable_result(started_at)


def run_claude(
    prompt: Path,
    iteration_dir: Path,
    *,
    timeout_seconds: float,
    grace_seconds: float,
) -> ProcessResult:
    stdout = iteration_dir / "stdout.log"
    stderr = iteration_dir / "stderr.log"
    command = ["claude", "-p", "--permission-mode", "acceptEdits"]
    started_at = utc_now()
    try:
        with (
            prompt.open("rb") as prompt_stream,
            stdout.open("wb") as stdout_stream,
            stderr.open("wb") as stderr_stream,
        ):
            result = run_agent_process(
                command,
                prompt_stream,
                stdout_stream,
                stderr_stream,
                timeout_seconds=timeout_seconds,
                grace_seconds=grace_seconds,
            )
    except FileNotFoundError as error:
        stderr.write_text(f"{error}\n", encoding="utf-8")
        stdout.touch(exist_ok=True)
        result = missing_executable_result(started_at)
    (iteration_dir / "final.md").write_bytes(stdout.read_bytes())
    return result


def run_agent(
    agent: str,
    prompt: Path,
    iteration_dir: Path,
    *,
    timeout_seconds: float,
    grace_seconds: float,
) -> ProcessResult:
    runner = run_codex if agent == "codex" else run_claude
    return runner(
        prompt,
        iteration_dir,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
    )


def determine_outcome(
    *,
    args: argparse.Namespace,
    process_result: ProcessResult,
    final_message: str,
    artifact_error: bool,
    selected_state: dict[str, bool | None],
    document: dict[str, Any],
    attempt: int,
    final_iteration: bool,
) -> IterationOutcome:
    signal_number = RECEIVED_SIGNAL
    if signal_number is not None:
        return IterationOutcome(
            state="interrupted",
            runner_exit_code=128 + signal_number,
            signal_name=signal.Signals(signal_number).name,
            interruption_reason="signal",
            retryable=True,
            human_action="Re-run the runner after reviewing the interrupted artifacts.",
        )
    if process_result.timed_out:
        return IterationOutcome(
            state="timed_out",
            runner_exit_code=TIMEOUT_EXIT_CODE,
            signal_name=None,
            interruption_reason="wall_clock_timeout",
            retryable=True,
            human_action="Review the timeout receipt, then manually start a new run to retry this feature.",
        )
    if artifact_error:
        return IterationOutcome(
            state="failed",
            runner_exit_code=3,
            signal_name=None,
            interruption_reason="artifact_error",
            retryable=False,
            human_action="Repair artifact storage before retrying.",
        )
    if process_result.exit_code != 0:
        return IterationOutcome(
            state="failed",
            runner_exit_code=3,
            signal_name=None,
            interruption_reason="agent_error",
            retryable=True,
            human_action="Review stderr and process evidence before a manual retry.",
        )
    if selected_state["passes"] is True:
        if all_features_pass(document):
            state = "complete"
            exit_code: int | None = 0
        elif final_iteration:
            state = "feature_complete"
            exit_code = 0
        else:
            state = "feature_complete"
            exit_code = None
        return IterationOutcome(state, exit_code, None, None, False, None)
    if selected_state["blocked"] is True or PROMISE_BLOCKED in final_message:
        return IterationOutcome(
            state="blocked",
            runner_exit_code=2,
            signal_name=None,
            interruption_reason="feature_blocked",
            retryable=False,
            human_action="Resolve the recorded blocker before starting a new run.",
        )
    if attempt >= args.max_attempts_per_feature:
        return IterationOutcome(
            state="manual_review",
            runner_exit_code=2,
            signal_name=None,
            interruption_reason="attempt_limit",
            retryable=True,
            human_action="Review repeated-failure evidence before manually starting a new run.",
        )
    if final_iteration:
        strict = os.environ.get("GOAL_LOOP_STRICT_INCOMPLETE") == "1"
        return IterationOutcome(
            state="incomplete",
            runner_exit_code=1 if strict else 0,
            signal_name=None,
            interruption_reason="max_iterations",
            retryable=True,
            human_action="Start a new bounded run to retry the same feature.",
        )
    return IterationOutcome("incomplete", None, None, None, True, None)


def terminal_summary(
    iteration: int,
    outcome: IterationOutcome,
    agent_exit_code: int | None,
    iteration_dir: Path,
) -> str:
    if outcome.state == "timed_out":
        status = f"[timeout] iteration {iteration} exceeded its wall-clock limit"
    elif outcome.signal_name is not None:
        status = f"[interrupted] iteration {iteration} interrupted"
    elif outcome.state == "failed":
        status = f"[failure] iteration {iteration} failed"
    elif outcome.state in {"manual_review", "blocked"}:
        status = f"[failure] iteration {iteration} stopped"
    else:
        status = f"[success] iteration {iteration} completed"
    lines = [
        status,
        f"[state] {outcome.state}",
        f"[agent-exit] {agent_exit_code}",
        f"[runner-exit] {outcome.runner_exit_code}",
    ]
    if outcome.signal_name is not None:
        lines.append(f"[signal] {outcome.signal_name}")
    lines.extend((f"[logs] {iteration_dir}", ""))
    return "\n".join(lines)


def timeout_receipt(
    *,
    run_dir: Path,
    iteration_dir: Path,
    iteration: int,
    feature: dict[str, Any],
    attempt: int,
    process_result: ProcessResult,
    timeout_limit: float,
    tests_started: bool,
    tests_completed: bool,
) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "iteration": iteration,
        "feature_id": feature["id"],
        "attempt": attempt,
        "status": "timed_out",
        "recovery_state": "interrupted_retryable",
        "start_time": timestamp(process_result.started_at),
        "timeout_time": timestamp(process_result.timeout_at),
        "termination_completed_at": timestamp(process_result.ended_at),
        "elapsed_seconds": process_result.elapsed_seconds,
        "timeout_limit_seconds": timeout_limit,
        "subprocess_exit_status": process_result.exit_code,
        "termination_method": process_result.termination_method,
        "feature_state": feature_state(feature),
        "tests_started": tests_started,
        "tests_completed": tests_completed,
        "evidence_path": str(iteration_dir),
        "retryable": True,
        "human_action": "Review this receipt and evidence, then manually start a new run; the same feature remains selectable.",
    }


def finalize_iteration(
    *,
    args: argparse.Namespace,
    run_started_at: datetime,
    run_dir: Path,
    iteration_dir: Path,
    iteration: int,
    feature: dict[str, Any],
    attempt: int,
    process_result: ProcessResult,
    final_message: str,
    artifact_error: bool,
    selected_state: dict[str, bool | None],
    document: dict[str, Any],
    git_head_before: str | None,
    git_head_after: str | None,
    git_status_before: str,
    git_status_after: str,
    codex_thread_id: str | None,
    tests_started: bool,
    tests_completed: bool,
) -> int | None:
    while True:
        signal_before = RECEIVED_SIGNAL
        outcome = determine_outcome(
            args=args,
            process_result=process_result,
            final_message=final_message,
            artifact_error=artifact_error,
            selected_state=selected_state,
            document=document,
            attempt=attempt,
            final_iteration=iteration == args.max_iterations,
        )
        metadata = {
            "run_id": run_dir.name,
            "start_time": timestamp(process_result.started_at),
            "end_time": timestamp(process_result.ended_at),
            "agent": args.agent,
            "max_iterations": args.max_iterations,
            "iteration": iteration,
            "feature_id": feature["id"],
            "feature_title": feature.get("title") or feature.get("description"),
            "attempt": attempt,
            "max_attempts_per_feature": args.max_attempts_per_feature,
            "completion_state": outcome.state,
            "agent_exit_code": process_result.exit_code,
            "runner_exit_code": outcome.runner_exit_code,
            "timeout_limit_seconds": args.iteration_timeout,
            "timeout_at": timestamp(process_result.timeout_at),
            "elapsed_seconds": process_result.elapsed_seconds,
            "termination_method": process_result.termination_method,
            "feature_state": selected_state,
            "tests_started": tests_started,
            "tests_completed": tests_completed,
            "retryable": outcome.retryable,
            "human_action": outcome.human_action,
            "git_head_before": git_head_before,
            "git_head_after": git_head_after,
            "git_status_before": git_status_before,
            "git_status_after": git_status_after,
            "log_paths": artifact_paths(args.agent, run_dir, iteration_dir),
            "signal": outcome.signal_name,
            "interruption_reason": outcome.interruption_reason,
            "codex_thread_id": codex_thread_id,
        }
        (iteration_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        summary = None
        if outcome.runner_exit_code is not None:
            run_metadata = {
                **metadata,
                "start_time": timestamp(run_started_at),
                "end_time": timestamp(utc_now()),
            }
            (run_dir / "metadata.json").write_text(
                json.dumps(run_metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            summary = terminal_summary(
                iteration,
                outcome,
                process_result.exit_code,
                iteration_dir,
            )
            (run_dir / "summary.txt").write_text(summary, encoding="utf-8")

        if RECEIVED_SIGNAL != signal_before:
            continue
        if summary is not None:
            print(summary, end="")
            if RECEIVED_SIGNAL != signal_before:
                continue
        return outcome.runner_exit_code


def run_loop(
    args: argparse.Namespace,
    workspace: Path,
    base_prompt_path: Path,
    features_path: Path,
) -> int:
    initial_document = load_feature_document(features_path)
    if pending_feature(initial_document) is None:
        if all_features_pass(initial_document):
            print("[complete] all features already pass")
            return 0
        if all_remaining_features_blocked(initial_document):
            print("[blocked] all remaining features are blocked", file=sys.stderr)
            return 2
        print("[complete] no selectable feature remains")
        return 0

    run_started_at = utc_now()
    git_head_before, git_status_before = git_snapshot(workspace)
    run_dir = create_run_dir(workspace / "logs")
    (run_dir / "git-before.txt").write_text(
        git_snapshot_text(git_head_before, git_status_before),
        encoding="utf-8",
    )
    attempts: dict[str, int] = {}
    base_prompt = base_prompt_path.read_text(encoding="utf-8")

    for iteration in range(1, args.max_iterations + 1):
        document = load_feature_document(features_path)
        feature = pending_feature(document)
        if feature is None:
            if all_features_pass(document):
                print("[complete] all features already pass")
                return 0
            if all_remaining_features_blocked(document):
                print("[blocked] all remaining features are blocked", file=sys.stderr)
                return 2
            print("[complete] no selectable feature remains")
            return 0

        feature_id = feature["id"]
        attempt = attempts.get(feature_id, 0) + 1
        attempts[feature_id] = attempt
        iteration_dir = run_dir / f"iter-{iteration:03d}"
        iteration_dir.mkdir()
        generated_prompt = build_iteration_prompt(
            base_prompt,
            feature,
            attempt=attempt,
            evidence_destination=iteration_dir,
        )
        prompt_path = iteration_dir / "prompt.md"
        prompt_path.write_text(generated_prompt, encoding="utf-8")
        print(
            f"[start] iteration {iteration}/{args.max_iterations} "
            f"feature={feature_id} attempt={attempt}/{args.max_attempts_per_feature}",
            flush=True,
        )
        print(f"[logs] {iteration_dir}", flush=True)

        process_result = run_agent(
            args.agent,
            prompt_path,
            iteration_dir,
            timeout_seconds=args.iteration_timeout,
            grace_seconds=args.termination_grace,
        )

        final_path = iteration_dir / "final.md"
        artifact_error = False
        try:
            final_message = final_path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            artifact_error = True
            final_message = ""
            print(f"[artifact-error] final.md: {error}", file=sys.stderr)

        try:
            document_after = load_feature_document(features_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            artifact_error = True
            document_after = document
            print(f"[artifact-error] features.json: {error}", file=sys.stderr)
        selected_after = feature_by_id(document_after, feature_id)
        if process_result.timed_out and not artifact_error:
            selected_after = keep_timed_out_feature_pending(
                document_after,
                feature_id,
                features_path,
            )
        selected_state = feature_state(selected_after)
        tests_started, tests_completed = test_activity(args.agent, iteration_dir)

        if process_result.timed_out:
            receipt = timeout_receipt(
                run_dir=run_dir,
                iteration_dir=iteration_dir,
                iteration=iteration,
                feature=selected_after or feature,
                attempt=attempt,
                process_result=process_result,
                timeout_limit=args.iteration_timeout,
                tests_started=tests_started,
                tests_completed=tests_completed,
            )
            (iteration_dir / "timeout-receipt.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        git_head_after, git_status_after = git_snapshot(workspace)
        (run_dir / "git-after.txt").write_text(
            git_snapshot_text(git_head_after, git_status_after),
            encoding="utf-8",
        )
        codex_thread_id = (
            extract_codex_thread_id(iteration_dir / "events.jsonl")
            if args.agent == "codex"
            else None
        )
        exit_code = finalize_iteration(
            args=args,
            run_started_at=run_started_at,
            run_dir=run_dir,
            iteration_dir=iteration_dir,
            iteration=iteration,
            feature=feature,
            attempt=attempt,
            process_result=process_result,
            final_message=final_message,
            artifact_error=artifact_error,
            selected_state=selected_state,
            document=document_after,
            git_head_before=git_head_before,
            git_head_after=git_head_after,
            git_status_before=git_status_before,
            git_status_after=git_status_after,
            codex_thread_id=codex_thread_id,
            tests_started=tests_started,
            tests_completed=tests_completed,
        )
        if exit_code is not None:
            return exit_code

    return 0


def main() -> int:
    global RECEIVED_SIGNAL
    RECEIVED_SIGNAL = None
    args = parse_args()
    workspace = Path.cwd()
    prompt = workspace / "PROMPT.md"
    features = workspace / "docs" / "features.json"
    if not prompt.is_file():
        print("error: PROMPT.md was not found in the current directory.", file=sys.stderr)
        return 66
    if not features.is_file():
        print("error: docs/features.json was not found in the current directory.", file=sys.stderr)
        return 66

    try:
        if args.status or args.dry_run:
            document = load_feature_document(features)
            print_status(document)
            if args.dry_run:
                print(
                    "[dry-run] "
                    f"agent={args.agent} max_iterations={args.max_iterations} "
                    f"timeout={args.iteration_timeout} "
                    f"max_attempts_per_feature={args.max_attempts_per_feature}"
                )
            return 0
        install_signal_handlers()
        return run_loop(args, workspace, prompt, features)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[artifact-error] {error}", file=sys.stderr)
        print("[runner-exit] 3", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
