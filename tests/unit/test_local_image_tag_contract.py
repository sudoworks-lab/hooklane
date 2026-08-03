from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
IMAGE_TAG_SCRIPT = ROOT / "scripts" / "image_tag.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GIT_IMAGE_TAG = "git-ebe9577d7bc29fe10c431a821f524e3ba9c40d88"


def run_image_tag(*arguments: str, image_tag: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("IMAGE_TAG", None)
    if image_tag is not None:
        environment["IMAGE_TAG"] = image_tag
    return subprocess.run(
        [sys.executable, str(IMAGE_TAG_SCRIPT), *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_default_image_tag_is_the_release_baseline() -> None:
    result = run_image_tag()
    assert result.returncode == 0, result.stderr


def test_git_sha_image_tag_is_accepted() -> None:
    result = run_image_tag("--image-tag", GIT_IMAGE_TAG)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "invalid_tag",
    ("latest", "git-abc123", "git-A7b55d7c41f5f818c1702adddfdb62f42564b37c", " git-abc ", ""),
)
def test_mutable_incomplete_or_whitespace_image_tags_are_rejected(invalid_tag: str) -> None:
    result = run_image_tag("--image-tag", invalid_tag)
    assert result.returncode == 1
    assert "registry" not in result.stderr.lower()
    assert "credential" not in result.stderr.lower()


def test_build_scan_contract_and_verify_use_one_explicit_tag() -> None:
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-n",
            "images-build",
            "image-contract",
            "security-image",
            "container-policy-check",
            f"IMAGE_TAG={GIT_IMAGE_TAG}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout
    for image in ("hooklane-api", "hooklane-worker", "hooklane-mock-sink"):
        assert f"{image}:{GIT_IMAGE_TAG}" in output
    assert f'image_contract.py --image-tag "{GIT_IMAGE_TAG}"' in output
    assert f'security_gate.py image --image-tag "{GIT_IMAGE_TAG}"' in output
    assert f'container_policy_check.py --target image --image-tag "{GIT_IMAGE_TAG}"' in output

    verify = subprocess.run(
        ["make", "--no-print-directory", "-n", "verify", f"IMAGE_TAG={GIT_IMAGE_TAG}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert f'security_gate.py all --image-tag "{GIT_IMAGE_TAG}"' in verify.stdout


def test_ci_uses_commit_tag_and_does_not_fallback_to_release_tag() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("Determine immutable source image identity") == 2
    assert 'PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}' in workflow
    assert 'if [[ "$GITHUB_EVENT_NAME" == "pull_request" ]]; then' in workflow
    assert 'SOURCE_SHA="$PR_HEAD_SHA"' in workflow
    assert 'SOURCE_SHA="$GITHUB_SHA"' in workflow
    assert '[[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]' in workflow
    assert 'IMAGE_TAG="git-$SOURCE_SHA"' in workflow
    assert 'IMAGE_TAG="git-$GITHUB_SHA"' not in workflow
    assert 'make images-build IMAGE_TAG="$IMAGE_TAG"' in workflow
    assert "make observability-images" in workflow
    assert 'make image-contract IMAGE_TAG="$IMAGE_TAG"' in workflow
    assert 'make verify IMAGE_TAG="$IMAGE_TAG"' in workflow
    assert "make e2e-kind IMAGE_TAG=\"$IMAGE_TAG\"" in workflow
    assert "0.1.1" not in workflow
    assert "docker push" not in workflow


def test_missing_image_fails_with_a_clear_local_error() -> None:
    missing_tag = "git-0000000000000000000000000000000000000000"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "image_contract.py"), "--image-tag", missing_tag],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "api image is not available locally" in result.stderr
    assert "registry" not in result.stderr.lower()
    assert "credential" not in result.stderr.lower()
