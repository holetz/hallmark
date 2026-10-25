#!/usr/bin/env bash
# test-leak-gate.sh: prove the hooks and the generic scan in a throwaway repository.
# The denylist is a temp file with a dummy term; the real one is never read.
# Sample secrets are assembled at run time so this file itself matches no pattern.
set -Euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0
check() { # check <label> <expected-exit: zero|nonzero> <actual-exit>
  if { [ "$2" = zero ] && [ "$3" -eq 0 ]; } || { [ "$2" = nonzero ] && [ "$3" -ne 0 ]; }; then
    echo "ok   $1"
  else
    echo "FAIL $1 (exit $3)"; fail=1
  fi
}

DENY="$TMP/denylist.txt"
echo "dummyforbiddenterm" >"$DENY"
export QUENCHING_DENYLIST_FILE="$DENY"
cpf="123.456.789-""09"

REPO="$TMP/repo"
git init -q -b main "$REPO"
git -C "$REPO" config user.name "Test"
git -C "$REPO" config user.email "test@example.com"
git -C "$REPO" config core.hooksPath "$HERE/githooks"

# clean change passes pre-commit
echo "hello" >"$REPO/a.txt"; git -C "$REPO" add a.txt
git -C "$REPO" commit -q -m "clean" ; check "pre-commit accepts a clean change" zero $?

# staged CPF is refused
echo "doc $cpf" >"$REPO/b.txt"; git -C "$REPO" add b.txt
git -C "$REPO" commit -q -m "leaky" 2>/dev/null; check "pre-commit refuses a staged CPF" nonzero $?
( cd "$REPO" && bash "$HERE/leak-generic.sh" ) >/dev/null 2>&1; check "leak-generic refuses a tracked CPF" nonzero $?
git -C "$REPO" rm -q -f --cached b.txt; rm "$REPO/b.txt"
( cd "$REPO" && bash "$HERE/leak-generic.sh" ) >/dev/null 2>&1; check "leak-generic accepts a clean tree" zero $?

# missing denylist fails closed
echo "more" >>"$REPO/a.txt"; git -C "$REPO" add a.txt
QUENCHING_DENYLIST_FILE="$TMP/absent.txt" git -C "$REPO" commit -q -m "no denylist" 2>/dev/null
check "pre-commit fails closed without the denylist" nonzero $?

# missing cq fails closed (PATH without cq)
mkdir "$TMP/bin"; for t in bash git env dirname; do ln -s "$(command -v $t)" "$TMP/bin/$t"; done
( cd "$REPO" && PATH="$TMP/bin" bash "$HERE/githooks/pre-commit" ) >/dev/null 2>&1
check "pre-commit fails closed without cq" nonzero $?
git -C "$REPO" commit -q -m "ok again"; check "pre-commit accepts again" zero $?

# pre-push: a commit carrying a CPF is refused
git init -q --bare "$TMP/remote.git"
git -C "$REPO" remote add origin "$TMP/remote.git"
git -C "$REPO" push -q origin main 2>/dev/null; check "pre-push accepts clean commits" zero $?
echo "doc $cpf" >"$REPO/c.txt"; git -C "$REPO" add c.txt
git -C "$REPO" -c core.hooksPath=/dev/null commit -q -m "leaky, hook bypassed for the test"
git -C "$REPO" push -q origin main 2>/dev/null; check "pre-push refuses a commit carrying a CPF" nonzero $?

exit "$fail"
