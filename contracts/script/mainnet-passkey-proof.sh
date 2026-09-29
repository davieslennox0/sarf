#!/usr/bin/env bash
#
# Deploy SarfDelegate to X Layer MAINNET and prove the passkey path end to end
# against the chain's real RIP-7212 precompile.
#
# This is the one thing the Foundry suite cannot cover: forge executes locally
# even when forking, and its EVM has no P256VERIFY, so the contract's passkey
# branch has only ever run against a mock. Here it runs against 0x100 itself.
#
# WHAT IT NEEDS
#   A funded key. Real OKB, but barely: at 0.02 gwei the whole run costs about
#   0.00005 OKB (under a cent). Defaults to the zap test wallet, which the
#   user funded for exactly this kind of work.
#
#     export XL_MAINNET_KEY=0x...          # or leave unset to use the zap test wallet
#
# WHAT IT PROVES, IN ORDER
#   1. the contract deploys on chain 196
#   2. an EIP-7702 authorisation points the EOA at it (code becomes 0xef0100…)
#   3. registerPasskey works from the account's own key
#   4. setScopeWithPasskey verifies a REAL P-256 WebAuthn assertion through
#      the chain's precompile, and the scope it sets is readable afterwards
#   5. a replay of the same assertion is refused on-chain
#
# It changes nothing in the server. It DOES deploy an unaudited contract to
# mainnet and delegate the test wallet's own EOA to it; that wallet holds only
# test funds, and nothing can act under the delegation until a scope is set.
set -euo pipefail

RPC="${XL_MAINNET_RPC:-https://rpc.xlayer.tech}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="${XL_MAINNET_KEY:-}"
if [ -z "$KEY" ]; then
  # The file stores it bare, without the 0x cast wants.
  KEY="$(sed -n 's/^SARF_ZAP_TEST_PRIVATE_KEY=//p' /root/.sarf-zap-testwallet | head -1)"
  [ -n "$KEY" ] || { echo "!! no key in /root/.sarf-zap-testwallet" >&2; exit 1; }
  case "$KEY" in 0x*) ;; *) KEY="0x$KEY" ;; esac
  echo "==> no XL_MAINNET_KEY set; using the zap test wallet"
fi
ADDR="$(cast wallet address --private-key "$KEY")"
CHAIN="$(cast chain-id --rpc-url "$RPC")"
[ "$CHAIN" = "196" ] || { echo "!! expected chain 196 (X Layer mainnet), got $CHAIN" >&2; exit 1; }

BAL="$(cast balance "$ADDR" --rpc-url "$RPC")"
echo "==> deployer $ADDR on chain $CHAIN, balance $BAL wei"
if [ "$BAL" = "0" ]; then
  echo "!! $ADDR has no OKB. Fund it and re-run; ~0.0001 OKB covers the proof." >&2
  exit 1
fi

cd "$ROOT"
echo "==> 1/5 deploying SarfDelegate"
DELEGATE="$(forge create src/SarfDelegate.sol:SarfDelegate \
  --rpc-url "$RPC" --private-key "$KEY" --broadcast --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["deployedTo"])')"
echo "    deployed at $DELEGATE"

echo "==> 2/5 attaching the 7702 delegation"
cast send "$ADDR" --auth "$DELEGATE" --private-key "$KEY" --rpc-url "$RPC" >/dev/null
CODE="$(cast code "$ADDR" --rpc-url "$RPC")"
case "$CODE" in
  0xef0100*) echo "    code is $CODE" ;;
  *) echo "!! no delegation indicator: $CODE" >&2; exit 1 ;;
esac

echo "==> 3/5 registering a passkey and 4/5 setting a scope with it"
# The assertion is built in Python: a real P-256 key, a real WebAuthn
# clientDataJSON, and the digest the contract itself will recompute.
DELEGATE="$DELEGATE" ACCOUNT="$ADDR" RPC="$RPC" KEY="$KEY" \
  python3 "$ROOT/script/passkey_proof.py"

echo
echo "==> done. Delegate: $DELEGATE (chain 196)"
echo "    The passkey path is now proven against the real precompile."
echo "    This does NOT make it production-ready: the contract is unaudited"
echo "    and ZAP_7702_ENABLED stays false."
