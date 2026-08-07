"""EVM-side validation primitives for the X Layer boundary.

Same contract as validation.py: pure functions, no I/O, every input treated as
untrusted client data. Sui used 32-byte 0x ids; X Layer is an ordinary EVM
chain, so addresses are 20 bytes and need EIP-55 handling.

Checksum policy: we accept lowercase and correctly-checksummed mixed case, and
REJECT a mixed-case string whose checksum does not verify. A bad checksum is
the one cheap signal that an address was corrupted in transit (a typo, a
truncated copy/paste, a homoglyph swap) — the whole point of EIP-55 — and this
layer feeds a trading tool where a wrong address means lost funds.
"""

from __future__ import annotations

import re

from ..validation import ValidationError

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TXHASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _keccak(data: bytes) -> bytes:
    # eth_utils/pycryptodome are not dependencies; hashlib ships sha3_256 which
    # is NOT keccak-256 (different padding), so use the pure-python fallback
    # only when a real keccak is unavailable.
    try:
        from Crypto.Hash import keccak as _k  # type: ignore

        h = _k.new(digest_bits=256)
        h.update(data)
        return h.digest()
    except Exception:
        return _keccak_pure(data)


# --- minimal keccak-256 (FIPS-202 Keccak, not SHA3) --------------------------
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]
_M = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M


def _keccak_f(a: list[list[int]]) -> None:
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & _M & b[(x + 2) % 5][y])
        a[0][0] ^= _RC[rnd]


def _keccak_pure(data: bytes) -> bytes:
    rate = 136  # 1088 bits for keccak-256
    padded = bytearray(data)
    padded.append(0x01)  # keccak padding (SHA3 would use 0x06)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    state = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)

    out = bytearray()
    for i in range(4):  # 32 bytes
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


def to_checksum_address(addr: str) -> str:
    """EIP-55 checksummed form of a 0x-prefixed 20-byte address."""
    low = addr.lower().replace("0x", "")
    digest = _keccak(low.encode()).hex()
    return "0x" + "".join(
        ch.upper() if int(digest[i], 16) >= 8 and ch.isalpha() else ch
        for i, ch in enumerate(low)
    )


def validate_evm_address(value: object, *, what: str = "address") -> str:
    """-> lowercase 0x address. Rejects a failing EIP-55 checksum."""
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string")
    v = value.strip()
    if not _ADDR_RE.match(v):
        raise ValidationError(
            f"{what} must be a 0x-prefixed 20-byte EVM address (X Layer, chain 196)"
        )
    body = v[2:]
    if body != body.lower() and body != body.upper():
        # Mixed case means the sender asserted a checksum — hold them to it.
        if to_checksum_address(v) != v:
            raise ValidationError(
                f"{what} has an invalid EIP-55 checksum; it may be mistyped or corrupted"
            )
    return v.lower()


def validate_tx_hash(value: object, *, what: str = "tx_hash") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string")
    v = value.strip().lower()
    if not _TXHASH_RE.match(v):
        raise ValidationError(f"{what} must be a 0x-prefixed 32-byte transaction hash")
    return v
