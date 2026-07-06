#!/usr/bin/env bash
# Fails (exit 1) if any staged file is a .env file, a key file, or contains a
# private-key-shaped string. Wired in twice on purpose:
#   - .githooks/pre-commit  (local; enable with: git config core.hooksPath .githooks)
#   - CI (see README)       (remote; catches commits made without the hook)
# Rationale: a previous project leaked a key through a .gitignore miss, so we
# do not rely on .gitignore alone — this checks what is actually staged.
set -euo pipefail

fail=0

# 1) Forbidden filenames staged (allow .env.example — it must contain no real values).
while IFS= read -r f; do
  base="$(basename "$f")"
  case "$base" in
    .env|.env.*)
      if [ "$base" != ".env.example" ]; then
        echo "BLOCKED: attempting to commit env file: $f" >&2
        fail=1
      fi
      ;;
    *.pem|*.key|sui.keystore)
      echo "BLOCKED: attempting to commit key material: $f" >&2
      fail=1
      ;;
  esac
  case "$f" in
    *sui_config/*|*.sui/*)
      echo "BLOCKED: attempting to commit sui config dir contents: $f" >&2
      fail=1
      ;;
  esac
done < <(git diff --cached --name-only --diff-filter=ACM)

# 2) Private-key patterns inside staged content.
#    - suiprivkey1...        Sui bech32 private key
#    - AGE-SECRET-KEY / PEM  generic key blocks
#    - 64-hex assigned to a *KEY/SECRET/MNEMONIC-looking var (object IDs are 64-hex
#      too, so we only flag hex bound to a secret-looking assignment)
patterns=(
  'suiprivkey1[0-9a-z]{20,}'
  '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
  'AGE-SECRET-KEY-1[0-9A-Z]+'
  '(PRIVATE_KEY|SECRET_KEY|MNEMONIC|SEED_PHRASE)[[:space:]]*[=:][[:space:]]*["'"'"']?(0x)?[0-9a-fA-F]{32,}'
)
for p in "${patterns[@]}"; do
  if git diff --cached -U0 | grep -E "^\+" | grep -Ev "^\+\+\+" | grep -qE "$p"; then
    echo "BLOCKED: staged content matches private-key pattern: $p" >&2
    git diff --cached -U0 | grep -E "^\+" | grep -E "$p" | head -3 | sed 's/^/  /' >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "" >&2
  echo "Commit rejected by scripts/check-secrets.sh. Move secrets to .env (gitignored)." >&2
  exit 1
fi
echo "check-secrets: OK (no env files or key material staged)"
