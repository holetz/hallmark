#!/usr/bin/env bash
# leak-generic.sh: scan the tracked files of the current repository for the generic leak patterns.
# Runs where `cq` is absent (CI). It mirrors the publishable pattern set of `cq security leak
# --generic-only`; keep the two aligned by hand. It never reads the private denylist.
# Output names file:line and the pattern, never the matched text, so a leak is not echoed to a log.
set -Eeuo pipefail

declare -A PATTERNS=(
  [brazilian-cpf]='\b[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}\b'
  [brazilian-cnpj]='\b[0-9]{2}\.[0-9]{3}\.[0-9]{3}/[0-9]{4}-[0-9]{2}\b'
  [azure-databricks-host]='adb-[0-9]{6,}\.[0-9]+\.azuredatabricks\.net'
  [databricks-pat]='\bdapi[0-9a-f]{32}\b'
  [classic-cluster-id]='\b[0-9]{4}-[0-9]{6}-[a-z0-9]{8}\b'
  [private-key]='-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
)

found=0
for name in "${!PATTERNS[@]}"; do
  if hits="$(git grep -nIiE -e "${PATTERNS[$name]}" -- . 2>/dev/null | cut -d: -f1,2)"; [ -n "$hits" ]; then
    while IFS= read -r loc; do
      echo "leak-generic: $name at $loc" >&2
    done <<<"$hits"
    found=1
  fi
done

if [ "$found" -ne 0 ]; then
  echo "leak-generic: refused; remove the content, never weaken a pattern." >&2
  exit 1
fi
echo "leak-generic: clean"
