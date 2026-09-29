#!/usr/bin/env bash
#
# One command to undo the Earn deployment.
#
# What the deployment consists of, and therefore what this undoes:
#   1. earn-server        a second pm2 app on :8761 with its own database
#   2. frontend/dist      rebuilt to carry the /earn pages (previous kept)
#   3. /etc/caddy/Caddyfile   routes for /earn, /earn/* and /earn-api/*
#
# What it deliberately does NOT touch:
#   - sarf-server. It was never restarted for this deployment and it is not
#     restarted here either. Its database is untouched in both directions.
#   - server/data/earn.db. Deleting a database is not a rollback, and if Earn
#     ever held a real position the row is the only record of it. The file is
#     left in place, inert, once the app is stopped.
#
# Usage:  scripts/rollback-earn.sh            (stop Earn, restore bundle + Caddy)
#         scripts/rollback-earn.sh --keep-bundle   (leave the current frontend)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CADDY_BACKUP=/etc/caddy/Caddyfile.bak-20260929-earn
KEEP_BUNDLE="${1:-}"

echo "==> 1/4 stopping and removing the earn-server app"
pm2 delete earn-server >/dev/null 2>&1 || echo "    (earn-server was not running)"

echo "==> 2/4 restoring the Caddy config from before Earn"
if [ -f "$CADDY_BACKUP" ]; then
  cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak-rollback-$(date +%Y%m%d-%H%M%S)"
  cp "$CADDY_BACKUP" /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
    && systemctl reload caddy \
    && echo "    restored and reloaded"
else
  echo "    !! $CADDY_BACKUP missing — leaving the config alone" >&2
fi

echo "==> 3/4 restoring the previous frontend bundle"
if [ "$KEEP_BUNDLE" = "--keep-bundle" ]; then
  echo "    skipped (--keep-bundle)"
elif [ -d "$ROOT/frontend/dist.previous" ]; then
  rm -rf "$ROOT/frontend/dist.rolled-back"
  mv "$ROOT/frontend/dist" "$ROOT/frontend/dist.rolled-back"
  mv "$ROOT/frontend/dist.previous" "$ROOT/frontend/dist"
  echo "    swapped; the Earn bundle is at frontend/dist.rolled-back"
else
  echo "    !! frontend/dist.previous missing — leaving the bundle alone" >&2
fi

echo "==> 4/4 checking what is left standing"
# sarf-server serves the bundle with StaticFiles, which resolves per request,
# so the swap above needs no restart. Say what is true rather than restarting
# the one app this rollback exists to protect.
sleep 2
for u in https://getsarf.xyz/ https://getsarf.xyz/zap https://getsarf.xyz/healthz; do
  printf "    %-38s %s\n" "$u" "$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$u")"
done
printf "    %-38s %s  (404 expected after rollback)\n" "https://getsarf.xyz/earn" \
  "$(curl -s -o /dev/null -m 15 -w '%{http_code}' https://getsarf.xyz/earn)"
pm2 jlist | python3 -c "
import json,sys
for p in json.load(sys.stdin):
    if p['name'] in ('sarf-server','earn-server'):
        print(f\"    pm2 {p['name']:12} {p['pm2_env']['status']} restarts={p['pm2_env']['restart_time']}\")"
echo
echo "==> done. sarf-server was not restarted. server/data/earn.db left in place."
echo "    To undo the code as well: git revert ddd7a66 (the backtest gate) and"
echo "    the Earn deploy commit, then rebuild the frontend."
