#!/usr/bin/env bash
#
# Install the CI-built frontend bundle. No npm, no vite, no node.
#
# WHY THIS EXISTS
#   This box has 961 MB of RAM and roughly 50 MB of it free. `vite build` on
#   this dependency tree needs more than that, and when it fails it fails
#   destructively: vite empties frontend/dist before it starts, so an OOM
#   half-way through leaves the server with no bundle to serve and the site
#   returning 500 until a build finally succeeds. That is not a good property
#   for the deploy step of a live site.
#
#   So the bundle is built by GitHub Actions (.github/workflows/ci.yml) and
#   published to the `frontend-dist` branch. This script fetches it.
#
# WHY A PULL, NOT A PUSH
#   The alternative is CI deploying over SSH, which means a private key for
#   this box sitting in GitHub secrets and an inbound path from a third party
#   into the machine that holds the relayer wallet. Pulling needs neither: the
#   box reaches out, and the worst a compromised CI can do is publish a bad
#   bundle — which it could do either way.
#
# USAGE
#   scripts/deploy-frontend.sh              # fetch, install, restart the server
#   scripts/deploy-frontend.sh --no-restart # fetch and install only
#
set -euo pipefail

BRANCH="${FRONTEND_DIST_BRANCH:-frontend-dist}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/frontend/dist"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

cd "$ROOT"

echo "==> fetching origin/$BRANCH"
git fetch --quiet --depth=1 origin "$BRANCH"
built=$(git log -1 --format='%s (%cr)' "origin/$BRANCH")
echo "    $built"

# Stage into a temp directory first. The live dist is replaced only once a
# complete tree is on disk, so a failed fetch or a broken archive can never
# leave the site serving half a bundle — the exact failure mode building here
# had.
staging="$(mktemp -d "$ROOT/frontend/.dist-incoming.XXXXXX")"
trap 'rm -rf "$staging"' EXIT
git archive "origin/$BRANCH" | tar -x -C "$staging"

if [ ! -f "$staging/index.html" ]; then
  echo "!! no index.html in origin/$BRANCH — refusing to install" >&2
  exit 1
fi

previous="$ROOT/frontend/dist.previous"
rm -rf "$previous"
[ -d "$DIST" ] && mv "$DIST" "$previous"
mv "$staging" "$DIST"
trap - EXIT
echo "==> installed $(find "$DIST" -type f | wc -l) files into frontend/dist"
[ -d "$previous" ] && echo "    previous bundle kept at frontend/dist.previous"

if [ "$RESTART" = "1" ]; then
  # StaticFiles resolves paths per request, so a swapped directory is picked up
  # without this — but the server is restarted anyway because a deploy usually
  # carries Python changes too, and a restart is the only thing that loads them.
  echo "==> restarting sarf-server"
  pm2 restart sarf-server --update-env >/dev/null
  sleep 4
  code=$(curl -s -o /dev/null -w '%{http_code}' "${SARF_HEALTH_URL:-http://127.0.0.1:8760/healthz}")
  echo "    healthz: $code"
  [ "$code" = "200" ] || { echo "!! server did not come back healthy" >&2; exit 1; }
fi

echo "done."
