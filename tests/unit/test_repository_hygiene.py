"""Focused coverage for repository hygiene candidate and placeholder checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _repository_hygiene() -> ModuleType:
    path = ROOT / "scripts" / "repository_hygiene.py"
    spec = importlib.util.spec_from_file_location("hooklane_repository_hygiene_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


repository_hygiene = _repository_hygiene()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _temporary_repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (tmp_path / "tracked.md").write_text("tracked documentation\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".gitignore", "tracked.md")
    return tmp_path


def _set_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(repository_hygiene, "ROOT", root)


@pytest.mark.parametrize(
    "identity",
    (
        "YOUR" + "_REPO",
        "YOUR" + "-REPO",
        "YOUR" + "_ORG",
        "YOUR" + "-ORG",
        "OWNER" + "/" + "REPO",
        "github.com/" + "example" + "/" + "repo",
        "github.com/" + "owner" + "/" + "repo",
    ),
)
def test_untracked_placeholder_public_candidate_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    _set_root(monkeypatch, _temporary_repository(tmp_path))
    candidate = tmp_path / "candidate.md"
    candidate.write_text(f"Remote identity: {identity}\n", encoding="utf-8")

    candidates = repository_hygiene.commit_candidate_files()
    assert Path("candidate.md") in candidates

    with pytest.raises(
        repository_hygiene.HygieneError,
        match="placeholder remote identity in public source",
    ):
        repository_hygiene.validate_public_text(repository_hygiene.public_text_files(candidates))


@pytest.mark.parametrize("text", ("your-repository", "your-repositorys", "your-reporter"))
def test_near_prefix_public_words_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    _set_root(monkeypatch, _temporary_repository(tmp_path))
    (tmp_path / "candidate.md").write_text(f"Documentation: {text}\n", encoding="utf-8")

    candidates = repository_hygiene.commit_candidate_files()
    repository_hygiene.validate_public_text(repository_hygiene.public_text_files(candidates))


def test_ci_trust_model_documentation_url_passes_hygiene() -> None:
    path = ROOT / "docs" / "CI_TRUST_MODEL.md"
    text = path.read_text(encoding="utf-8")
    assert (
        "https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-"
        "features/customizing-your-repository/about-code-owners"
    ) in text
    repository_hygiene.validate_public_text((Path("docs/CI_TRUST_MODEL.md"),))


def test_untracked_legitimate_markdown_is_scanned_and_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_root(monkeypatch, _temporary_repository(tmp_path))
    (tmp_path / "candidate.md").write_text(
        "This public commit candidate documents your-repositorys settings.\n",
        encoding="utf-8",
    )

    candidates = repository_hygiene.commit_candidate_files()
    public_candidates = repository_hygiene.public_text_files(candidates)
    assert Path("candidate.md") in public_candidates
    repository_hygiene.validate_public_text(public_candidates)


def test_ignored_generated_cache_is_excluded_from_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_root(monkeypatch, _temporary_repository(tmp_path))
    ignored = tmp_path / "cache" / "generated.md"
    ignored.parent.mkdir()
    ignored.write_text("Remote identity: " + "YOUR" + "_REPO\n", encoding="utf-8")

    candidates = repository_hygiene.commit_candidate_files()
    assert Path("cache/generated.md") not in candidates
    repository_hygiene.validate_public_text(repository_hygiene.public_text_files(candidates))


def test_tracked_file_state_remains_distinct_from_untracked_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_root(monkeypatch, _temporary_repository(tmp_path))
    (tmp_path / "candidate.md").write_text("candidate\n", encoding="utf-8")

    tracked = repository_hygiene.tracked_files()
    candidates = repository_hygiene.commit_candidate_files()
    assert Path("tracked.md") in tracked
    assert Path("candidate.md") not in tracked
    assert Path("candidate.md") in candidates

    tracked_secret = tmp_path / ".env"
    tracked_secret.write_text("placeholder value\n", encoding="utf-8")
    _git(tmp_path, "add", "--force", "--", ".env")
    with pytest.raises(
        repository_hygiene.HygieneError,
        match="tracked environment file is prohibited",
    ):
        repository_hygiene.validate_tracked_paths(repository_hygiene.tracked_files())
