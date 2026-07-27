#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

show_tool() {
  local label="$1" binary="$2" output status
  shift 2
  if command -v "$binary" >/dev/null 2>&1; then
    if output="$("$@" 2>&1)"; then
      printf '[ok] %s: %s\n' "$label" "$(printf '%s\n' "$output" | sed -n '1p')"
    else
      status=$?
      printf '[warn] %s: version command failed with exit code %s; optional check skipped\n' "$label" "$status"
    fi
  else
    printf '[skip] %s: command not found\n' "$label"
  fi
}

run_make_target() {
  local target="$1"
  if [[ ! -f Makefile ]]; then
    printf '[skip] make %s: Makefile is not present yet\n' "$target"
  elif ! command -v make >/dev/null 2>&1; then
    printf '[skip] make %s: make command not found\n' "$target"
  elif awk -F: -v target="$target" '$1 == target { found=1 } END { exit(found ? 0 : 1) }' Makefile; then
    printf '[run] make %s\n' "$target"
    make "$target"
  else
    printf '[skip] make %s: target is not defined yet\n' "$target"
  fi
}

printf '[init] Hooklane progressive environment check\n'
show_tool Bash bash bash --version
show_tool Git git git --version
show_tool Make make make --version
show_tool Docker docker docker --version
show_tool Docker-Compose docker docker compose version
show_tool kubectl kubectl kubectl version --client
show_tool kind kind kind version
show_tool Helm helm helm version --short
show_tool Gitleaks gitleaks gitleaks version
show_tool OSV-Scanner osv-scanner osv-scanner --version
show_tool Trivy trivy trivy --version
show_tool Kubeconform kubeconform kubeconform -v
show_tool Terraform terraform terraform version

PYTHON_CMD=""
if command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
fi

if [[ -n "$PYTHON_CMD" ]]; then
  printf '[ok] Python: '
  "$PYTHON_CMD" --version
  "$PYTHON_CMD" -c 'import json, pathlib; json.loads(pathlib.Path("docs/features.json").read_text(encoding="utf-8"))'
  printf '[ok] docs/features.json: valid JSON\n'
  if [[ -f pyproject.toml ]]; then
    "$PYTHON_CMD" -c 'import pathlib, tomllib; tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))'
    printf '[ok] pyproject.toml: valid TOML\n'
  else
    printf '[skip] pyproject.toml: file is not present yet\n'
  fi
  if [[ -d src ]]; then
    "$PYTHON_CMD" -c 'import ast, pathlib; [ast.parse(path.read_text(encoding="utf-8"), filename=str(path)) for path in pathlib.Path("src").rglob("*.py")]'
    printf '[ok] src/: read-only Python syntax check passed\n'
  else
    printf '[skip] src/: directory is not present yet\n'
  fi
else
  printf '[skip] Python checks: neither python nor python3 was found\n'
fi

bash -n scripts/init.sh scripts/loop.sh
printf '[ok] shell syntax: scripts/init.sh scripts/loop.sh\n'
run_make_target doctor
run_make_target smoke-fast
printf '[info] No package install, external download, service start, or environment value output was performed.\n'
