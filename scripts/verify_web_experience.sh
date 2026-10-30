#!/usr/bin/env bash
# Development-only verification; never installs or restarts host services.
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${CLOUDHOST_VERIFY_WORK:-${PROJECT}/.web-verification}"
mkdir -p "${WORK}/work"
export TMPDIR="${WORK}/work"
cd "${PROJECT}"
"${CLOUDHOST_TEST_PYTHON:-python3}" -m pytest tests \
  --basetemp="${WORK}/pytest" \
  --junitxml="${WORK}/results.xml" "$@"
