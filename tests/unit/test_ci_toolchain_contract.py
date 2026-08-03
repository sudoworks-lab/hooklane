from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = (ROOT / "scripts" / "install_ci_tools.py").read_text(encoding="utf-8")


def test_terraform_ci_tool_is_exact_and_checksum_pinned() -> None:
    assert 'name="terraform"' in INSTALLER
    assert 'version="1.15.5"' in INSTALLER
    assert 'asset="terraform_1.15.5_linux_amd64.zip"' in INSTALLER
    assert '"https://releases.hashicorp.com/terraform/1.15.5/"' in INSTALLER
    assert '"terraform_1.15.5_linux_amd64.zip"' in INSTALLER
    assert '"terraform_1.15.5_SHA256SUMS"' in INSTALLER
    assert 'archive_format="zip"' in INSTALLER
    assert 'archive_member="terraform"' in INSTALLER
    assert 'BIN_DIR = Path.home() / ".local" / "bin"' in INSTALLER


def test_ci_workflow_requires_pinned_terraform_verification() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert 'HOOKLANE_TERRAFORM_REQUIRED: "1"' in workflow
    assert 'export PATH="$HOME/.local/bin:$PATH"' in workflow
    assert "terraform version" in workflow
