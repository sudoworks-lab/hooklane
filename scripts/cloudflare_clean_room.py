"""Run the Cloudflare CI gate from source-only files and an isolated home."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TOOLS = ("make", "node", "npm", "npx")


class CleanRoomError(RuntimeError):
    """Raised when the isolated Cloudflare gate cannot be executed."""


def source_files() -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise CleanRoomError("source candidate enumeration failed")
    return tuple(Path(item.decode("utf-8")) for item in completed.stdout.split(b"\0") if item)


def prepare_tool_path(tool_bin: Path) -> str:
    tool_bin.mkdir()
    for name in REQUIRED_TOOLS:
        source = shutil.which(name)
        if source is None:
            raise CleanRoomError(f"required clean-room tool was not found: {name}")
        (tool_bin / name).symlink_to(Path(source).resolve())
    return os.pathsep.join((str(tool_bin), "/usr/bin", "/bin"))


def copy_candidate(destination: Path) -> None:
    for relative_path in source_files():
        source = ROOT / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def main() -> int:
    try:
        if Path(sys.executable).absolute().is_relative_to(ROOT):
            raise CleanRoomError("clean-room interpreter must not come from a repository venv")
        with tempfile.TemporaryDirectory(prefix="hooklane-cloudflare-clean-room-") as temporary:
            boundary = Path(temporary)
            candidate = boundary / "repository"
            candidate.mkdir()
            copy_candidate(candidate)

            home = boundary / "home"
            cache = boundary / "cache"
            data = boundary / "data"
            for directory in (home, cache, data):
                directory.mkdir()
            environment = {
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "PATH": prepare_tool_path(boundary / "bin"),
                "XDG_CACHE_HOME": str(cache),
                "XDG_DATA_HOME": str(data),
            }
            completed = subprocess.run(
                ["make", "cloudflare-ci-check", f"PYTHON={sys.executable}"],
                cwd=candidate,
                env=environment,
                check=False,
                timeout=900,
            )
            if completed.returncode != 0:
                raise CleanRoomError(
                    f"isolated Cloudflare CI gate returned {completed.returncode}"
                )
    except (OSError, subprocess.TimeoutExpired, CleanRoomError) as error:
        print(f"[fail] Cloudflare clean room: {error}")
        return 1
    print(
        "[pass] Cloudflare clean room: source-only copy, isolated HOME, pinned bootstrap, "
        "and local backend gate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
