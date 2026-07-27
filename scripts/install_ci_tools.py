"""Install the pinned CI tools from official releases with checksum verification."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


BIN_DIR = Path.home() / ".local" / "bin"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    asset: str
    asset_url: str
    checksum_url: str
    archive_member: str | None
    version_arguments: tuple[str, ...]
    archive_format: str | None = None


TOOLS = (
    ToolSpec(
        name="terraform",
        version="1.15.5",
        asset="terraform_1.15.5_linux_amd64.zip",
        asset_url=(
            "https://releases.hashicorp.com/terraform/1.15.5/"
            "terraform_1.15.5_linux_amd64.zip"
        ),
        checksum_url=(
            "https://releases.hashicorp.com/terraform/1.15.5/"
            "terraform_1.15.5_SHA256SUMS"
        ),
        archive_member="terraform",
        version_arguments=("version",),
        archive_format="zip",
    ),
    ToolSpec(
        name="gitleaks",
        version="8.30.1",
        asset="gitleaks_8.30.1_linux_x64.tar.gz",
        asset_url=(
            "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
            "gitleaks_8.30.1_linux_x64.tar.gz"
        ),
        checksum_url=(
            "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
            "gitleaks_8.30.1_checksums.txt"
        ),
        archive_member="gitleaks",
        version_arguments=("version",),
    ),
    ToolSpec(
        name="osv-scanner",
        version="2.4.0",
        asset="osv-scanner_linux_amd64",
        asset_url=(
            "https://github.com/google/osv-scanner/releases/download/v2.4.0/"
            "osv-scanner_linux_amd64"
        ),
        checksum_url=(
            "https://github.com/google/osv-scanner/releases/download/v2.4.0/"
            "osv-scanner_SHA256SUMS"
        ),
        archive_member=None,
        version_arguments=("--version",),
    ),
    ToolSpec(
        name="trivy",
        version="0.72.0",
        asset="trivy_0.72.0_Linux-64bit.tar.gz",
        asset_url=(
            "https://github.com/aquasecurity/trivy/releases/download/v0.72.0/"
            "trivy_0.72.0_Linux-64bit.tar.gz"
        ),
        checksum_url=(
            "https://github.com/aquasecurity/trivy/releases/download/v0.72.0/"
            "trivy_0.72.0_checksums.txt"
        ),
        archive_member="trivy",
        version_arguments=("--version",),
    ),
    ToolSpec(
        name="kubeconform",
        version="0.7.0",
        asset="kubeconform-linux-amd64.tar.gz",
        asset_url=(
            "https://github.com/yannh/kubeconform/releases/download/v0.7.0/"
            "kubeconform-linux-amd64.tar.gz"
        ),
        checksum_url=(
            "https://github.com/yannh/kubeconform/releases/download/v0.7.0/CHECKSUMS"
        ),
        archive_member="kubeconform",
        version_arguments=("-v",),
    ),
    ToolSpec(
        name="helm",
        version="4.2.3",
        asset="helm-v4.2.3-linux-amd64.tar.gz",
        asset_url="https://get.helm.sh/helm-v4.2.3-linux-amd64.tar.gz",
        checksum_url="https://get.helm.sh/helm-v4.2.3-linux-amd64.tar.gz.sha256sum",
        archive_member="linux-amd64/helm",
        version_arguments=("version", "--short"),
    ),
    ToolSpec(
        name="kind",
        version="0.32.0",
        asset="kind-linux-amd64",
        asset_url=(
            "https://github.com/kubernetes-sigs/kind/releases/download/v0.32.0/"
            "kind-linux-amd64"
        ),
        checksum_url=(
            "https://github.com/kubernetes-sigs/kind/releases/download/v0.32.0/"
            "kind-linux-amd64.sha256sum"
        ),
        archive_member=None,
        version_arguments=("version",),
    ),
    ToolSpec(
        name="kubectl",
        version="1.34.1",
        asset="kubectl",
        asset_url="https://dl.k8s.io/release/v1.34.1/bin/linux/amd64/kubectl",
        checksum_url="https://dl.k8s.io/release/v1.34.1/bin/linux/amd64/kubectl.sha256",
        archive_member=None,
        version_arguments=("version", "--client"),
    ),
)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hooklane-ci-tool-installer"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            downloaded: object = response.read()
            if not isinstance(downloaded, bytes):
                raise RuntimeError("official tool download was not bytes")
            return downloaded
    except OSError as error:
        raise RuntimeError("official tool download failed") from error


def published_checksum(data: bytes, asset: str) -> str:
    candidates: list[str] = []
    for line in data.decode("utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        checksum = fields[0]
        if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
            continue
        candidates.append(checksum)
        if len(fields) > 1 and Path(fields[-1].lstrip("*")).name == asset:
            return checksum
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(f"published checksum missing for {asset}")


def binary_from_asset(spec: ToolSpec, asset_data: bytes) -> bytes:
    if spec.archive_member is None:
        return asset_data
    if spec.archive_format == "zip":
        with zipfile.ZipFile(io.BytesIO(asset_data)) as archive:
            try:
                return archive.read(spec.archive_member)
            except KeyError as error:
                raise RuntimeError(f"binary member missing for {spec.name}") from error
    with tarfile.open(fileobj=io.BytesIO(asset_data), mode="r:gz") as archive:
        try:
            member = archive.getmember(spec.archive_member)
        except KeyError as error:
            raise RuntimeError(f"binary member missing for {spec.name}") from error
        handle = archive.extractfile(member)
        if handle is None:
            raise RuntimeError(f"binary member unreadable for {spec.name}")
        binary: object = handle.read()
        if not isinstance(binary, bytes):
            raise RuntimeError(f"binary member is not bytes for {spec.name}")
        return binary


def version_matches(spec: ToolSpec, executable: Path) -> bool:
    if not executable.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(executable), *spec.version_arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{completed.stdout}\n{completed.stderr}"
    return completed.returncode == 0 and spec.version in output


def install(spec: ToolSpec) -> None:
    destination = BIN_DIR / spec.name
    if version_matches(spec, destination):
        print(f"[ok] {spec.name} {spec.version} already installed")
        return

    asset_data = fetch(spec.asset_url)
    expected = published_checksum(fetch(spec.checksum_url), spec.asset)
    actual = hashlib.sha256(asset_data).hexdigest()
    if actual != expected:
        raise RuntimeError(f"checksum mismatch for {spec.name}")

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=BIN_DIR,
            prefix=f".{spec.name}-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(binary_from_asset(spec, asset_data))
        temporary.chmod(0o755)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    if not version_matches(spec, destination):
        raise RuntimeError(f"installed version mismatch for {spec.name}")
    print(f"[ok] {spec.name} {spec.version} installed from {spec.asset_url}")
    print(f"[ok] {spec.name} published SHA-256 {actual}")


def main() -> int:
    try:
        for tool in TOOLS:
            install(tool)
    except RuntimeError as error:
        print(f"[fail] CI tool installation: {error}")
        return 1
    print(f"[ok] pinned CI tools are available in {BIN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
