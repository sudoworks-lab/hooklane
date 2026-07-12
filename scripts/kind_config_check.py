"""Validate the fixed local kind configuration without creating a cluster."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Any, Never, cast


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "deploy" / "kind" / "cluster.yaml"
TOOLCHAIN_PATH = ROOT / "toolchain.toml"


def fail(message: str) -> Never:
    raise RuntimeError(message)


def load_toolchain() -> dict[str, Any]:
    data: object = tomllib.loads(TOOLCHAIN_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail("toolchain.toml root must be an object")
    return cast(dict[str, Any], data)


def main() -> int:
    toolchain = load_toolchain()
    optional = cast(dict[str, Any], toolchain["tools"]["optional"])
    images = cast(dict[str, Any], toolchain["images"])
    resources = cast(dict[str, Any], toolchain["resources"])
    expected_kind_version = str(optional["kind"])
    expected_node_image = str(images["kind_node"])

    completed = subprocess.run(
        ["kind", "version"],
        check=True,
        capture_output=True,
        text=True,
    )
    if f"v{expected_kind_version}" not in completed.stdout:
        fail("installed kind version does not match toolchain.toml")

    config = CONFIG_PATH.read_text(encoding="utf-8")
    required_fragments = (
        "kind: Cluster",
        "apiVersion: kind.x-k8s.io/v1alpha4",
        "role: control-plane",
        f"image: {expected_node_image}",
        "containerPort: 30080",
        "hostPort: 18082",
        'listenAddress: "127.0.0.1"',
        "protocol: TCP",
        f"at least {resources['min_cpus']} CPUs",
        f"{resources['min_memory_gib']} GiB memory",
        f"{resources['min_disk_gib']} GiB free disk",
    )
    for fragment in required_fragments:
        if fragment not in config:
            fail(f"kind config is missing required contract: {fragment}")
    if len(re.findall(r"^\s*- role:", config, flags=re.MULTILINE)) != 1:
        fail("kind config must define exactly one local control-plane node")
    if ":latest" in config:
        fail("kind config must not use latest")
    print("[ok] fixed kind node, loopback port, and resource assumptions passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        print(f"[fail] kind config: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
