#!/usr/bin/env bash
# Sets up fit on this machine:
#   - .venv (pinned Python via uv) with dev deps, for running tests
#   - a `uv tool install` of fit itself, so plain `fit` works on PATH
# Mirrors the steps in README.md.
#
# Usage:
#   ./install.sh            # install fit + dev deps (pytest)
#   ./install.sh --garmin   # also install the garminconnect extra

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

tool_extras=""
venv_extras="dev"
if [[ "${1:-}" == "--garmin" ]]; then
    tool_extras="[garmin]"
    venv_extras="dev,garmin"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required but not found on PATH." >&2
    echo "Install it from https://docs.astral.sh/uv/ and re-run this script." >&2
    exit 1
fi

if [[ -d .venv ]]; then
    echo "==> .venv already exists, reusing it"
else
    echo "==> Creating .venv (uv reads .python-version for the interpreter)"
    uv venv
fi

echo "==> Installing fit[$venv_extras] into .venv (for running tests)"
uv pip install -e ".[$venv_extras]"

echo "==> Installing fit as a uv tool (puts plain \`fit\` on your PATH)"
uv tool install --editable ".${tool_extras}" --force

echo "==> Smoke test"
fit usage

cat <<'EOF'

Done. `fit` is on your PATH (via `uv tool install`) and will pick up source
changes immediately since it's installed --editable.

Data lives in ~/.fit/ (override with FIT_DATA_DIR).
Run tests with: .venv/bin/pytest
EOF
