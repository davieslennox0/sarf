#!/usr/bin/env bash
#
# Build the frontend ON THIS BOX, without taking the site down while it runs.
#
# This is the deploy path. CI compiles the tree to prove it still builds from a
# clean checkout, but publishes nothing — the bundle that gets served is the one
# this script produces. It works around the thing that makes local builds
# dangerous here rather than pretending it away:
#
#   `vite build` empties frontend/dist BEFORE it starts. On a box with ~50 MB
#   of free RAM the build can take eight minutes or die of an out-of-memory
#   abort part-way through, and in both cases the live site is serving nothing
#   for the duration. That is not a build failure, it is an outage caused by
#   building.
#
# So this builds into a staging directory and swaps it in at the end. The live
# bundle is untouched until a complete new one exists, and a failed build
# changes nothing at all.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/frontend"

STAGE="dist-next"
HEAP="${VITE_BUILD_HEAP_MB:-1800}"

rm -rf "$STAGE"
echo "==> building into $STAGE (heap ${HEAP}MB) — the live bundle is untouched"
NODE_OPTIONS="--max-old-space-size=$HEAP" npx vite build --outDir "$STAGE" --emptyOutDir

[ -f "$STAGE/index.html" ] || { echo "!! no index.html produced — not swapping" >&2; exit 1; }

rm -rf dist.previous
[ -d dist ] && mv dist dist.previous
mv "$STAGE" dist
echo "==> swapped in $(find dist -type f | wc -l) files; previous bundle at frontend/dist.previous"

if [ "${1:-}" != "--no-restart" ]; then
  # Not needed for a pure asset swap — StaticFiles resolves per request — but a
  # local build usually accompanies Python changes, and a restart is the only
  # thing that loads those.
  pm2 restart sarf-server --update-env >/dev/null
  sleep 4
  echo "healthz: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8760/healthz)"
fi
