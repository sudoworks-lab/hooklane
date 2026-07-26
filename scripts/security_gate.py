"""Run Hooklane's pinned, fail-closed security scanner contract."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "security-policy.json"
TOOL_VERSIONS = {
    "gitleaks": "8.30.1",
    "osv-scanner": "2.4.0",
    "trivy": "0.72.0",
}
VERSION_ARGUMENTS = {
    "gitleaks": ["version"],
    "osv-scanner": ["--version"],
    "trivy": ["--version"],
}
IMAGES = (
    "hooklane-api:0.1.1",
    "hooklane-worker:0.1.1",
    "hooklane-mock-sink:0.1.1",
)


class SecurityGateError(RuntimeError):
    """A scanner, policy, or result contract failed closed."""


class SecurityFinding(SecurityGateError):
    """A scanner returned a policy-relevant finding."""


@dataclass(frozen=True)
class TrivyCounts:
    operating_system: int
    language: int

    @property
    def total(self) -> int:
        return self.operating_system + self.language


def object_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SecurityGateError(f"{label} is not a JSON object")
    return cast(dict[str, object], value)


def object_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise SecurityGateError(f"{label} is not a JSON array")
    return cast(list[object], value)


def load_policy() -> dict[str, object]:
    try:
        document: object = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SecurityGateError("security policy could not be parsed") from error
    return object_dict(document, "security policy")


def validate_policy() -> None:
    policy = load_policy()
    if policy.get("version") != 1 or policy.get("fail_closed") is not True:
        raise SecurityGateError("security policy must be version 1 and fail closed")
    if policy.get("exceptions") != []:
        raise SecurityGateError("security exceptions require human approval")

    tools = object_dict(policy.get("tools"), "security policy tools")
    for name, expected in TOOL_VERSIONS.items():
        entry = object_dict(tools.get(name), f"security policy tool {name}")
        if entry.get("version") != expected:
            raise SecurityGateError(f"security policy version mismatch for {name}")

    gitleaks = object_dict(tools.get("gitleaks"), "Gitleaks policy")
    if gitleaks.get("scope") != ["git-history", "working-tree"]:
        raise SecurityGateError("Gitleaks must scan history and the working tree")
    if gitleaks.get("redact_percent") != 100:
        raise SecurityGateError("Gitleaks output must be fully redacted")

    osv = object_dict(tools.get("osv-scanner"), "OSV-Scanner policy")
    if osv.get("lockfile") != "requirements.lock":
        raise SecurityGateError("OSV-Scanner must use requirements.lock")
    if osv.get("fail_on_any_vulnerability") is not True:
        raise SecurityGateError("OSV-Scanner must fail on every vulnerability")

    trivy = object_dict(tools.get("trivy"), "Trivy policy")
    if trivy.get("scanners") != ["vuln"]:
        raise SecurityGateError("Trivy must keep secret scanning separate")
    if trivy.get("fail_severities") != ["HIGH", "CRITICAL"]:
        raise SecurityGateError("Trivy must fail on High and Critical findings")
    if trivy.get("images") != list(IMAGES):
        raise SecurityGateError("Trivy image set does not match project images")


def run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SecurityGateError("scanner execution failed or timed out") from error


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SecurityGateError(f"required scanner unavailable: {name}")
    result = run([name, *VERSION_ARGUMENTS[name]], timeout=15)
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or TOOL_VERSIONS[name] not in output:
        raise SecurityGateError(f"required scanner version mismatch: {name}")
    print(f"[ok] {name} {TOOL_VERSIONS[name]}")


def parse_json_output(result: subprocess.CompletedProcess[str], label: str) -> object:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SecurityGateError(f"{label} did not return parseable JSON") from error


def evaluate_gitleaks(result: subprocess.CompletedProcess[str], label: str) -> None:
    findings = object_list(parse_json_output(result, label), f"{label} result")
    if result.returncode == 0 and not findings:
        print(f"[ok] {label}: 0 secret findings")
        return
    if result.returncode == 1 and findings:
        raise SecurityFinding(f"{label}: secret finding detected; output suppressed")
    raise SecurityGateError(f"{label}: scanner failure")


def scan_secret() -> None:
    require_tool("gitleaks")
    common = [
        "--redact=100",
        "--no-banner",
        "--no-color",
        "--log-level",
        "error",
        "--timeout",
        "120",
        "--report-format",
        "json",
        "--report-path",
        "-",
    ]
    evaluate_gitleaks(run(["gitleaks", "git", *common, "."], timeout=150), "git history")
    evaluate_gitleaks(run(["gitleaks", "dir", *common, "."], timeout=150), "working tree")


def scan_dependency() -> None:
    require_tool("osv-scanner")
    result = run(
        [
            "osv-scanner",
            "scan",
            "source",
            "--lockfile",
            "requirements.txt:requirements.lock",
            "--format",
            "json",
            "--verbosity",
            "error",
        ],
        timeout=180,
    )
    document = object_dict(parse_json_output(result, "OSV-Scanner"), "OSV-Scanner result")
    vulnerability_count = 0
    for raw_result in object_list(document.get("results"), "OSV-Scanner results"):
        scanned = object_dict(raw_result, "OSV-Scanner scan result")
        for raw_package in object_list(scanned.get("packages"), "OSV-Scanner packages"):
            package = object_dict(raw_package, "OSV-Scanner package")
            vulnerabilities = package.get("vulnerabilities", [])
            vulnerability_count += len(object_list(vulnerabilities, "OSV vulnerabilities"))
    if result.returncode == 0 and vulnerability_count == 0:
        print("[ok] OSV-Scanner requirements.lock: 0 vulnerabilities")
        return
    if vulnerability_count > 0:
        raise SecurityFinding(
            f"OSV-Scanner requirements.lock: {vulnerability_count} vulnerabilities"
        )
    raise SecurityGateError("OSV-Scanner failed without a vulnerability result")


def trivy_counts(document: object, required_target: str) -> TrivyCounts:
    report = object_dict(document, "Trivy report")
    results = object_list(report.get("Results"), "Trivy results")
    if not results:
        raise SecurityGateError("Trivy did not scan a supported target")
    matched_target = False
    operating_system = 0
    language = 0
    for raw_result in results:
        result = object_dict(raw_result, "Trivy result")
        target = result.get("Target")
        if isinstance(target, str) and required_target in target:
            matched_target = True
        vulnerabilities = object_list(result.get("Vulnerabilities", []), "Trivy vulnerabilities")
        if result.get("Class") == "os-pkgs":
            operating_system += len(vulnerabilities)
        else:
            language += len(vulnerabilities)
    if not matched_target:
        raise SecurityGateError("Trivy result does not contain the required target")
    return TrivyCounts(operating_system=operating_system, language=language)


def evaluate_trivy(
    result: subprocess.CompletedProcess[str],
    *,
    label: str,
    required_target: str,
) -> None:
    counts = trivy_counts(parse_json_output(result, label), required_target)
    summary = f"OS={counts.operating_system}, language={counts.language}"
    if result.returncode == 0 and counts.total == 0:
        print(f"[ok] {label}: High/Critical {summary}")
        return
    if counts.total > 0:
        raise SecurityFinding(f"{label}: High/Critical {summary}")
    raise SecurityGateError(f"{label}: scanner failure")


def trivy_common() -> list[str]:
    return [
        "--scanners",
        "vuln",
        "--severity",
        "HIGH,CRITICAL",
        "--exit-code",
        "1",
        "--format",
        "json",
        "--no-progress",
        "--disable-telemetry",
        "--skip-version-check",
        "--timeout",
        "5m",
    ]


def scan_filesystem() -> None:
    require_tool("trivy")
    result = run(
        [
            "trivy",
            "filesystem",
            *trivy_common(),
            "--file-patterns",
            r"pip:requirements\.lock",
            "--skip-dirs",
            ".git",
            "--skip-dirs",
            ".venv",
            "--skip-dirs",
            "logs",
            "--skip-dirs",
            "artifacts",
            "--skip-dirs",
            "__pycache__",
            ".",
        ],
        timeout=330,
    )
    evaluate_trivy(result, label="Trivy filesystem", required_target="requirements.lock")


def scan_images() -> None:
    require_tool("trivy")
    failed = False
    for image in IMAGES:
        result = run(
            ["trivy", "image", *trivy_common(), "--image-src", "docker", image],
            timeout=330,
        )
        try:
            evaluate_trivy(result, label=f"Trivy image {image}", required_target=image)
        except SecurityGateError as error:
            print(f"[fail] {error}")
            failed = True
    if failed:
        raise SecurityGateError("one or more project image scans failed")


def execute(name: str, action: Callable[[], None]) -> bool:
    try:
        action()
    except SecurityGateError as error:
        print(f"[fail] {name}: {error}")
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("secret", "dependency", "filesystem", "image", "all"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not execute("policy", validate_policy):
        return 1
    actions = {
        "secret": scan_secret,
        "dependency": scan_dependency,
        "filesystem": scan_filesystem,
        "image": scan_images,
    }
    selected = tuple(actions) if args.mode == "all" else (args.mode,)
    passed = True
    for name in selected:
        passed = execute(name, actions[name]) and passed
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
