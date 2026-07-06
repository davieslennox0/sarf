#!/usr/bin/env bash
# One-shot setup: vendor SDK clone+build, sidecar deps, python venv, hooks.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> git hooks (secret scan on every commit)"
git config core.hooksPath .githooks

echo "==> vendor: current-sdk"
if [ ! -d vendor/current-sdk ]; then
  git clone --depth 1 https://github.com/current-finance/current-sdk vendor/current-sdk
fi
(cd vendor/current-sdk && corepack enable && pnpm install && cd sdk && pnpm build || npx tsc || true)
test -f vendor/current-sdk/sdk/dist/src/index.js || { echo "SDK build failed"; exit 1; }

echo "==> txbuilder deps"
(cd txbuilder && npm install --no-fund --no-audit && npx tsc)

echo "==> python venv"
python3 -m venv server/.venv
server/.venv/bin/pip install -q -r server/requirements.txt

echo "==> env"
[ -f .env ] || { cp .env.example .env; echo "created .env from template — fill it in"; }
git check-ignore .env >/dev/null || { echo "FATAL: .env is not gitignored"; exit 1; }

echo "==> tests"
(cd server && .venv/bin/python -m pytest tests/ -q)

echo "done. start with: pm2 start ecosystem.config.cjs"
