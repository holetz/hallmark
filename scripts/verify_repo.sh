#!/usr/bin/env bash
# verify_repo.sh: the single repository gate. Needs neither `cq` nor the private denylist, so CI
# runs this same file; the hook test runs only where `cq` is installed. Zero errors is green.
set -Eeuo pipefail
cd "$(git rev-parse --show-toplevel)"

step() { echo "==> $1"; shift; "$@"; }

step "shell syntax" bash -c 'for f in scripts/*.sh scripts/githooks/*; do bash -n "$f"; done'
if command -v cq >/dev/null 2>&1; then
  step "leak gate behaviour" bash scripts/test-leak-gate.sh
else
  echo "==> leak gate behaviour: skipped (cq not on PATH; the hooks need it)"
fi
step "leak gate (generic patterns)" bash scripts/leak-generic.sh
echo "verify_repo: green"
