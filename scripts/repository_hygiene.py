"""Audit tracked repository hygiene without reading secret-bearing files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from typing import Never, cast


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
FEATURES = ROOT / "docs" / "features.json"
RELEASE_EVIDENCE = ROOT / "docs" / "RELEASE_EVIDENCE.md"
RELEASE_NOTES = ROOT / "docs" / "releases" / "v0.1.1.md"
THIRD_PARTY_NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
LICENSE = ROOT / "LICENSE"
STATUS = Path("docs") / "STATUS.md"
FORBIDDEN_PARTS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "artifacts",
    "logs",
    "node_modules",
}
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".tfstate")
PUBLIC_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".toml", ".json", ".sh"}
LATEST_SURFACES = (
    Path("README.md"),
    Path("docs/DEMO.md"),
    Path("docs/ARCHITECTURE.md"),
    Path("docs/OPERATIONS.md"),
    Path("docs/SECURITY.md"),
    Path("docs/LIMITATIONS.md"),
    Path("docs/SLO.md"),
    Path("Dockerfile"),
    Path("compose.yaml"),
    Path("charts/hooklane/values.yaml"),
    Path(".github/workflows/ci.yml"),
)
EXPECTED_MIT_LICENSE = """MIT License

Copyright (c) 2026 Hooklane contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class HygieneError(RuntimeError):
    """Raised when tracked state is unsuitable for verification or release review."""


def fail(message: str) -> Never:
    raise HygieneError(message)


def git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        fail("Git repository hygiene query failed")
    return result.stdout


def tracked_files() -> tuple[Path, ...]:
    return tuple(
        Path(name)
        for name in git_output("ls-files", "-z").split("\0")
        if name
    )


def validate_tracked_paths(files: tuple[Path, ...]) -> None:
    for path in files:
        lowered = path.name.lower()
        if lowered.startswith(".env") and path.name != ".env.example":
            fail(f"tracked environment file is prohibited: {path.as_posix()}")
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            fail(f"tracked cache, log, or artifact path is prohibited: {path.as_posix()}")
        if lowered.endswith(FORBIDDEN_SUFFIXES):
            fail(f"tracked private material filename is prohibited: {path.as_posix()}")
        if lowered.startswith(("id_rsa", "id_ed25519", "credentials", "secrets")):
            fail(f"tracked credential-like filename is prohibited: {path.as_posix()}")


def public_text_files(files: tuple[Path, ...]) -> tuple[Path, ...]:
    selected: list[Path] = []
    for path in files:
        if path.name == ".env.example":
            continue
        if path.suffix in PUBLIC_SUFFIXES or path.name in {"Makefile", "Dockerfile"}:
            selected.append(path)
    return tuple(selected)


def validate_public_text(files: tuple[Path, ...]) -> None:
    personal_path = re.compile(r"/(?:home|Users)/[^/\s]+/")
    wsl_path = re.compile(r"/mnt/[a-zA-Z]/Users/")
    email = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    incomplete = re.compile(r"(?m)(?:#|//|<!--)\s*(?:TODO|FIXME|XXX)\b")
    debug = re.compile(r"(?:breakpoint\(|pdb\.set_trace|console\.log\(|print\([^\n]*DEBUG)")
    signed_url = re.compile(
        r"https?://[^\s?]+\?[^\s]*(?:token|signature|credential|x-amz-signature)=",
        re.IGNORECASE,
    )
    placeholder_remote = re.compile(
        r"github\.com/(?:"
        r"example|your[-_]?org|owner"
        r")/|OWNER"
        r"/REPO|YOUR[-_](?:ORG|REPO)",
        re.IGNORECASE,
    )

    for relative_path in public_text_files(files):
        path = ROOT / relative_path
        text = path.read_text(encoding="utf-8")
        if personal_path.search(text) or wsl_path.search(text):
            fail(f"personal absolute path in public source: {relative_path.as_posix()}")
        if email.search(text):
            fail(f"email-like identifier in public source: {relative_path.as_posix()}")
        if incomplete.search(text):
            fail(f"unresolved implementation marker in public source: {relative_path.as_posix()}")
        if debug.search(text):
            fail(f"debug statement in public source: {relative_path.as_posix()}")
        if signed_url.search(text):
            fail(f"signed URL or query credential in public source: {relative_path.as_posix()}")
        if placeholder_remote.search(text):
            fail(f"placeholder remote identity in public source: {relative_path.as_posix()}")
        if (
            relative_path != Path("docs/GOAL.md")
            and STATUS.as_posix() in text
        ):
            fail(f"public source depends on excluded STATUS ledger: {relative_path.as_posix()}")

    tracked = set(files)
    for relative_path in LATEST_SURFACES:
        if relative_path not in tracked:
            continue
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        if re.search(r":latest\b", text):
            fail(f"unfixed image tag in release surface: {relative_path.as_posix()}")


def validate_readme_release_state(files: tuple[Path, ...]) -> None:
    if Path("README.md") not in files:
        fail("README.md is not tracked")
    readme = README.read_text(encoding="utf-8")
    required_paths = {
        Path("LICENSE"),
        Path("THIRD_PARTY_NOTICES.md"),
        Path("docs/RELEASE_EVIDENCE.md"),
        Path("docs/DEVELOPMENT.md"),
        Path("docs/releases/v0.1.1.md"),
    }
    missing = sorted(path.as_posix() for path in required_paths.difference(files))
    if missing:
        fail(f"public release file is missing: {', '.join(missing)}")
    if STATUS in files or (ROOT / STATUS).exists():
        fail("internal STATUS ledger must not exist in the public snapshot")
    if LICENSE.read_text(encoding="utf-8") != EXPECTED_MIT_LICENSE:
        fail("LICENSE does not match the approved MIT contract")

    for marker in (
        "MIT License",
        "source-only",
        "prebuilt container image",
        "docs/RELEASE_EVIDENCE.md",
        "THIRD_PARTY_NOTICES.md",
        "requirements.lock",
        "GitHub hosted Actions",
        "v0.1.1のtagがcurrent source baseline",
    ):
        if marker not in readme:
            fail(f"README release state is missing: {marker}")
    for obsolete in (
        "License未選定",
        "利用許諾と解釈しない",
        "public release: license",
    ):
        if obsolete in readme:
            fail(f"README retains obsolete license state: {obsolete}")

    evidence = RELEASE_EVIDENCE.read_text(encoding="utf-8")
    notices = THIRD_PARTY_NOTICES.read_text(encoding="utf-8")
    for marker in (
        "F001〜F029",
        "29/29",
        "blocked feature countは0",
        "make verify",
        "make demo-smoke",
        "make e2e-kind",
        "make rollout-smoke",
        "make observability-smoke",
        "make incident-smoke",
        "make clean-room",
        "GitHub hosted Actionsは公開mainで実行済み",
        "Quality, security, and chart gatesはsuccess",
        "kind delivery and recovery E2Eはsuccess",
        "source-only",
    ):
        if marker not in evidence:
            fail(f"release evidence is missing: {marker}")
    for section in (
        "## 対象",
        "## vendored third-party sourceなし",
        "## Python dependency",
        "## container baseとruntime image",
        "## 開発と検証tool",
        "## GitHub Actions",
        "## 配布に関する注記",
        "## exact versionの確認方法",
    ):
        if section not in notices:
            fail(f"third-party notice section is missing: {section}")
    for marker in (
        "完全な法的判断を示すものではない",
        "第三者source treeや実行binaryをvendorしない",
        "prebuilt container image、container-registry artifact、release archive、binary distributionは配布しない",
        "各上流のlicenseとnotice",
    ):
        if marker not in notices:
            fail(f"third-party notice contract is missing: {marker}")

    notes = RELEASE_NOTES.read_text(encoding="utf-8")
    for section in (
        "## 概要",
        "## READMEと文書構成の整理",
        "## 古いCI・公開状態の記載修正",
        "## Goal Loop開発文書の分離",
        "## package / repository metadata",
        "## application behavior",
        "## 検証結果",
        "## 既知の制約",
    ):
        if section not in notes:
            fail(f"v0.1.1 release notes section is missing: {section}")


def feature_state() -> tuple[int, int, int]:
    try:
        document: object = json.loads(FEATURES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HygieneError("feature state could not be parsed") from error
    if not isinstance(document, dict):
        fail("feature state root is not an object")
    raw_features = document.get("features")
    if not isinstance(raw_features, list):
        fail("feature state list is missing")
    features = cast(list[object], raw_features)
    passed = 0
    blocked = 0
    for raw_feature in features:
        if not isinstance(raw_feature, dict):
            fail("feature state contains a non-object")
        if raw_feature.get("passes") is True:
            passed += 1
        if raw_feature.get("blocked") is True:
            blocked += 1
    return len(features), passed, blocked


def validate_feature_state(*, require_complete: bool) -> None:
    total, passed, blocked = feature_state()
    if blocked:
        fail(f"feature state contains {blocked} blocked item(s)")
    if require_complete and (total != 29 or passed != total):
        fail(f"final feature state is incomplete: {passed}/{total}")
    print(f"[ok] feature state: {passed}/{total} passed, blocked={blocked}")


def validate_clean_worktree(*, require_clean: bool) -> None:
    if not require_clean:
        return
    if git_output("status", "--porcelain"):
        fail("release-readiness audit requires a clean worktree")


def run_checks(*, require_complete: bool, require_clean: bool) -> None:
    files = tracked_files()
    validate_tracked_paths(files)
    validate_public_text(files)
    validate_readme_release_state(files)
    validate_feature_state(require_complete=require_complete)
    validate_clean_worktree(require_clean=require_clean)
    print(
        "[ok] tracked secret/cache/history/log filenames, personal paths, email, "
        "debug markers, unresolved markers, image tags, and release metadata passed"
    )
    print("[ok] MIT license, source-only distribution, release evidence, and third-party notices passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run_checks(
            require_complete=bool(args.require_complete),
            require_clean=bool(args.require_clean),
        )
    except (OSError, HygieneError, UnicodeDecodeError) as error:
        print(f"[fail] repository hygiene: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
