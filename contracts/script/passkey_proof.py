"""Steps 3-5 of the mainnet passkey proof: register a passkey, set a scope with
a real WebAuthn assertion, and check the chain refuses a replay.

Run by mainnet-passkey-proof.sh, which passes DELEGATE/ACCOUNT/RPC/KEY.

The point of doing this on-chain: everything here is verified by X Layer's own
RIP-7212 precompile at 0x100, which Foundry's EVM does not implement. The unit
and fork tests mock it; this does not.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

from cryptography.hazmat.primitives import hashes
from eth_account import Account
from eth_utils import to_checksum_address
from cryptography.hazmat.primitives.asymmetric import ec, utils as au
from eth_abi import encode as abi_encode
from eth_utils import keccak

RPC = os.environ["RPC"]
ACCOUNT = os.environ["ACCOUNT"]
KEY = os.environ["KEY"]
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
# The keeper the scope will name. A throwaway address: this proof is about the
# passkey, and nothing is granted to anyone who could use it.
KEEPER = "0x000000000000000000000000000000000000C0FF"
# A token that must be capped for the scope to be meaningful; any address does.
TOKEN = "0x0000000000000000000000000000000000000001"


def rpc(method, params):
    req = urllib.request.Request(
        RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        {"Content-Type": "application/json"})
    out = json.load(urllib.request.urlopen(req, timeout=30))
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def cast(*args):
    r = subprocess.run(["cast", *args, "--rpc-url", RPC], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def num(out: str) -> int:
    """cast prints big numbers as `123 [1.23e2]`; keep the exact half."""
    return int(out.split()[0], 0)


def send(*args):
    return cast("send", *args, "--private-key", KEY)


def send_raw(data: str) -> str:
    """Sign and broadcast a call to the account itself.

    Not `cast send`: the WebAuthn assertion carries a JSON string inside a
    tuple, and cast's argument parser stops at the first escaped quote. The
    calldata is built with eth_abi instead, which also keeps the exact bytes
    the contract will hash.
    """
    acct = Account.from_key(KEY)
    tx = {"chainId": num(cast("chain-id")), "to": to_checksum_address(ACCOUNT),
          "data": data, "value": 0,
          "nonce": int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16),
          "gas": 600_000, "gasPrice": int(rpc("eth_gasPrice", []), 16)}
    signed = acct.sign_transaction(tx)
    h = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
    for _ in range(40):
        r = rpc("eth_getTransactionReceipt", [h])
        if r:
            if int(r["status"], 16) != 1:
                raise RuntimeError(f"reverted: {h}")
            return h
        time.sleep(1.5)
    raise RuntimeError(f"no receipt for {h}")


def b64url(b: bytes) -> str:
    out, acc, bits = "", 0, 0
    for byte in b:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 6:
            bits -= 6
            out += B64[(acc >> bits) & 0x3F]
    if bits:
        out += B64[(acc << (6 - bits)) & 0x3F]
    return out


# --- a passkey -------------------------------------------------------------
priv = ec.generate_private_key(ec.SECP256R1())
pub = priv.public_key().public_numbers()
print(f"    passkey x={hex(pub.x)[:18]}… y={hex(pub.y)[:18]}…")
send(ACCOUNT, "registerPasskey(uint256,uint256)", str(pub.x), str(pub.y))
on_chain_x = num(cast("call", ACCOUNT, "passkeyX()(uint256)"))
assert on_chain_x == pub.x, "passkey did not register"
print("    registerPasskey ok")

# --- the scope, and the digest the contract will recompute ------------------
expiry = int(time.time()) + 3 * 86400
nonce = int(time.time())
targets = [KEEPER]           # any whitelisted target; not exercised here
tokens = [TOKEN]
per_action = [10**18]
daily = [10**18]

digest = keccak(abi_encode(
    ["address", "uint256", "address", "uint64", "bytes32", "bytes32", "bytes32", "bytes32", "uint256"],
    [ACCOUNT, num(cast("chain-id")), KEEPER, expiry,
     keccak(b"".join(bytes.fromhex(a[2:]).rjust(32, b"\0") for a in targets)),
     keccak(b"".join(bytes.fromhex(a[2:]).rjust(32, b"\0") for a in tokens)),
     keccak(b"".join(v.to_bytes(32, "big") for v in per_action)),
     keccak(b"".join(v.to_bytes(32, "big") for v in daily)),
     nonce]))

# --- a real WebAuthn assertion over it -------------------------------------
auth_data = bytes.fromhex(
    "49960de5880e8c687434170f6476605b8fe4aeb9a28632c7995cf3ba831d976304000000ff")
client_data = ('{"type":"webauthn.get","challenge":"' + b64url(digest) +
               '","origin":"https://getsarf.xyz","crossOrigin":false}').encode()
challenge_index = client_data.index(b'"challenge":"')
msg_hash = hashlib.sha256(auth_data + hashlib.sha256(client_data).digest()).digest()
r, s = au.decode_dss_signature(
    priv.sign(msg_hash, ec.ECDSA(au.Prehashed(hashes.SHA256()))))
if s > N // 2:
    s = N - s

SIG = ("setScopeWithPasskey((address,uint64,address[],address[],uint128[],uint128[]),"
       "uint256,(bytes,string,uint256,uint256,uint256))")
TYPES = ["(address,uint64,address[],address[],uint128[],uint128[])", "uint256",
         "(bytes,string,uint256,uint256,uint256)"]


def scope_call(nonce_: int, r_: int, s_: int) -> str:
    spec_ = (to_checksum_address(KEEPER), expiry,
             [to_checksum_address(t) for t in targets],
             [to_checksum_address(t) for t in tokens], per_action, daily)
    sig_ = (auth_data, client_data.decode(), challenge_index, r_, s_)
    return "0x" + keccak(text=SIG)[:4].hex() + abi_encode(TYPES, [spec_, nonce_, sig_]).hex()


print("    calling setScopeWithPasskey (verified by 0x100 on-chain)…")
send_raw(scope_call(nonce, r, s))

got = cast("call", ACCOUNT, "scope()(address,uint64)").split()
assert got[0].lower() == KEEPER.lower(), f"scope keeper is {got[0]}"
print(f"    scope set on-chain by a P-256 signature: keeper={got[0]} expiry={got[1]}")

# --- and a replay must fail -------------------------------------------------
try:
    send_raw(scope_call(nonce, r, s))
except RuntimeError as e:
    assert "revert" in str(e).lower() or "PasskeyNonceUsed" in str(e), e
    print("    replay of the same assertion refused on-chain")
else:
    print("!! REPLAY SUCCEEDED — that is a bug, stop and investigate", file=sys.stderr)
    sys.exit(1)
