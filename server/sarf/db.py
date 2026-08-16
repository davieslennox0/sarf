"""SQLite persistence: obligation-cap index + proposal audit log.

Never funds, never keys — rows here are bookkeeping about *proposals* and a
cache of which obligation caps an address was last seen owning (ownership is
always re-verified on-chain in validation; the cache only speeds up
get_portfolio and lets the audit trail name things).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS obligation_caps (
  user_address TEXT NOT NULL,
  cap_id       TEXT NOT NULL,
  obligation_id TEXT NOT NULL,
  market_type  TEXT NOT NULL,
  market_name  TEXT,
  updated_at   REAL NOT NULL,
  PRIMARY KEY (user_address, cap_id)
);

CREATE TABLE IF NOT EXISTS proposals (
  proposal_id  TEXT PRIMARY KEY,
  created_at   REAL NOT NULL,
  expires_at   REAL NOT NULL,
  user_address TEXT NOT NULL,
  tool         TEXT NOT NULL,
  params_json  TEXT NOT NULL,
  ptb_base64   TEXT NOT NULL,
  simulation_json TEXT,
  risk_json    TEXT,
  status       TEXT NOT NULL DEFAULT 'proposed',
  tx_digest    TEXT,
  result_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_user ON proposals(user_address, created_at);

-- Dashboard/auth additions (addendum). users = unique identities that have
-- connected, via wallet auth or by using the MCP tools. Sessions are bearer
-- tokens minted after a wallet-signature challenge; no key material anywhere.
CREATE TABLE IF NOT EXISTS users (
  address    TEXT PRIMARY KEY,
  source     TEXT NOT NULL,          -- 'wallet' (signed in) | 'mcp' (used tools)
  first_seen REAL NOT NULL,
  last_seen  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  address    TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stats (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL
);

-- OAuth (MCP connector auth): dynamically registered public clients and
-- single-use PKCE authorization codes. Access tokens are ordinary session
-- rows — OAuth changes how a session is obtained, not what it is.
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id     TEXT PRIMARY KEY,
  client_name   TEXT,
  redirect_uris TEXT NOT NULL,        -- JSON array, exact-match at authorize/token
  created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
  code           TEXT PRIMARY KEY,
  client_id      TEXT NOT NULL,
  redirect_uri   TEXT NOT NULL,
  code_challenge TEXT NOT NULL,       -- PKCE S256
  address        TEXT NOT NULL,       -- wallet-verified before the code exists
  created_at     REAL NOT NULL,
  expires_at     REAL NOT NULL,
  used           INTEGER NOT NULL DEFAULT 0
);

-- Passkeys (WebAuthn). Public keys only: a passkey private key never leaves
-- the user's authenticator, exactly like their wallet key never reaches us.
CREATE TABLE IF NOT EXISTS passkeys (
  credential_id TEXT PRIMARY KEY,
  address       TEXT NOT NULL,
  public_key    BLOB NOT NULL,
  sign_count    INTEGER NOT NULL DEFAULT 0,
  created_at    REAL NOT NULL,
  last_used_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_passkeys_addr ON passkeys(address);

-- Single-use WebAuthn challenges, bound to an address AND a purpose so a
-- registration challenge can never be replayed as a step-up assertion.
CREATE TABLE IF NOT EXISTS passkey_challenges (
  challenge_id TEXT PRIMARY KEY,
  address      TEXT NOT NULL,
  purpose      TEXT NOT NULL,
  expires_at   REAL NOT NULL,
  used         INTEGER NOT NULL DEFAULT 0
);

-- Session-key grants (EIP-7702). One live grant per address: re-granting
-- rotates the key, and the contract only ever tracks the newest, so a second
-- live row here would describe a grant the chain does not have.
--
-- `sealed_key` is a session private key encrypted under a key derived from
-- SARF_SESSION_SECRET (see xlayer/delegation.py). It is NOT the user's wallet
-- key — Sarf never has that — and it is powerless outside the caps the user
-- signed on-chain. The caps recorded here are a copy for display; the ones
-- that bind are in the contract, because a limit enforced in this process is
-- a limit an attacker who reaches this process can skip.
CREATE TABLE IF NOT EXISTS grants (
  address         TEXT PRIMARY KEY,
  session_address TEXT NOT NULL,
  sealed_key      TEXT NOT NULL,
  delegate        TEXT NOT NULL,
  router          TEXT NOT NULL,
  stable          TEXT NOT NULL,
  expiry          INTEGER NOT NULL,
  per_trade_cap   INTEGER NOT NULL,
  daily_cap       INTEGER NOT NULL,
  created_at      REAL NOT NULL,
  rotated_at      REAL NOT NULL,
  revoked_at      REAL
);

-- X Layer order audit log. Records what was proposed and, once the user's
-- wallet broadcasts it, the resulting tx hash. Never holds keys or signatures.
CREATE TABLE IF NOT EXISTS orders (
  order_id     TEXT PRIMARY KEY,
  created_at   REAL NOT NULL,
  expires_at   REAL NOT NULL,
  address      TEXT NOT NULL,
  side         TEXT NOT NULL,          -- 'buy' | 'sell'
  symbol       TEXT NOT NULL,          -- on-chain x-suffix symbol, e.g. AAPLx
  amount_in    TEXT NOT NULL,          -- minimal units, as string (u256-safe)
  quoted_out   TEXT NOT NULL,
  est_usd      REAL,
  tx_json      TEXT NOT NULL,          -- the unsigned transaction we built
  status       TEXT NOT NULL DEFAULT 'proposed',
  tx_hash      TEXT,
  result_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_addr ON orders(address, created_at);
"""

# Columns added after the base schema shipped; applied idempotently.
_MIGRATIONS = [
    "ALTER TABLE proposals ADD COLUMN summary_text TEXT",
    "ALTER TABLE proposals ADD COLUMN risk_notes_json TEXT",
    # Revocation bookkeeping: an explicitly revoked session is marked, not
    # deleted, so the audit trail can distinguish "revoked (and why)" from
    # "naturally expired". Internal only — the token holder always sees the
    # same session_expired behavior regardless.
    "ALTER TABLE sessions ADD COLUMN revoked_at REAL",
    "ALTER TABLE sessions ADD COLUMN revocation_reason TEXT",
    # Human-readable order detail (summary, risk notes, fee, formatted
    # amounts). The signer page is the LAST review surface before the user
    # signs, so it has to show exactly what the assistant showed — deriving a
    # shorter version of it from raw columns is how a signer quietly stops
    # displaying the fee and the risk notes.
    "ALTER TABLE orders ADD COLUMN display_json TEXT",
    # Approval mode. Only "autonomous" is issued now — Always Ask was removed
    # because a passkey cannot render inside a chat widget, so it degraded to a
    # link out on every trade. The column stays: existing rows keep their value,
    # and the enforcement path still reads it, so an old always_ask grant
    # continues to demand a passkey rather than being silently loosened by an
    # upgrade. New grants are autonomous and bounded by the contract caps.
    "ALTER TABLE grants ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'always_ask'",
    # Ceiling for autonomous mode, in stable units. Only consulted when
    # approval_mode = 'autonomous'; above it, a passkey is required regardless.
    "ALTER TABLE grants ADD COLUMN autonomous_limit INTEGER NOT NULL DEFAULT 0",
    # Always Ask was removed from the platform on 2026-08-11 (see api.py). Rows
    # still carrying it are migrated rather than left holding a value nothing
    # reads any more — a stored mode no code path honours is worse than none.
    # Grants with no limit inherit their per-trade cap, which the contract
    # enforces regardless, so this widens nothing beyond what was already
    # authorised on-chain.
    "UPDATE grants SET approval_mode='autonomous' WHERE approval_mode<>'autonomous'",
    "UPDATE grants SET autonomous_limit=per_trade_cap WHERE autonomous_limit<=0",
]

# Stop-loss / take-profit levels, one row per (address, symbol).
#
# These are WATCH levels, not resting orders. Nothing here executes on its own:
# see set_risk_params in providers/xlayer_rwa.py for why that is a deliberate
# limit rather than an unfinished feature.
_RISK_TABLE = """
CREATE TABLE IF NOT EXISTS risk_params (
  address     TEXT NOT NULL,
  symbol      TEXT NOT NULL,
  stop_loss   REAL,
  take_profit REAL,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  PRIMARY KEY (address, symbol)
);
"""

# How long a revoked session row is retained after revocation for auditing
# (a compromise investigation needs to see WHEN and WHY a token was killed).
# Non-revoked rows are pruned as soon as they expire — they carry no signal.
REVOKED_SESSION_RETENTION_SECONDS = 30 * 86400


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    created_at: float
    expires_at: float
    user_address: str
    tool: str
    params: dict[str, Any]
    ptb_base64: str
    status: str


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.executescript(_RISK_TABLE)
        for mig in _MIGRATIONS:
            try:
                self._conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # column already exists
        self._lock = threading.Lock()

    # -- obligation cap index -------------------------------------------------

    def upsert_cap(
        self, user_address: str, cap_id: str, obligation_id: str,
        market_type: str, market_name: str | None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO obligation_caps VALUES (?,?,?,?,?,?)
                   ON CONFLICT(user_address, cap_id) DO UPDATE SET
                     obligation_id=excluded.obligation_id,
                     market_type=excluded.market_type,
                     market_name=excluded.market_name,
                     updated_at=excluded.updated_at""",
                (user_address, cap_id, obligation_id, market_type, market_name, time.time()),
            )

    def caps_for_user(self, user_address: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT cap_id, obligation_id, market_type, market_name FROM obligation_caps"
            " WHERE user_address=?",
            (user_address,),
        )
        return [
            {"cap_id": r[0], "obligation_id": r[1], "market_type": r[2], "market_name": r[3]}
            for r in cur.fetchall()
        ]

    # -- proposals / audit log ------------------------------------------------

    def create_proposal(
        self, *, user_address: str, tool: str, params: dict[str, Any],
        ptb_base64: str, simulation: dict[str, Any] | None,
        risk: dict[str, Any] | None, ttl_seconds: int,
        summary_text: str | None = None, risk_notes: list[str] | None = None,
    ) -> Proposal:
        now = time.time()
        pid = f"sarf_{uuid.uuid4().hex}"
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO proposals
                   (proposal_id, created_at, expires_at, user_address, tool,
                    params_json, ptb_base64, simulation_json, risk_json, status,
                    summary_text, risk_notes_json)
                   VALUES (?,?,?,?,?,?,?,?,?,'proposed',?,?)""",
                (
                    pid, now, now + ttl_seconds, user_address, tool,
                    json.dumps(params), ptb_base64,
                    json.dumps(simulation) if simulation is not None else None,
                    json.dumps(risk) if risk is not None else None,
                    summary_text,
                    json.dumps(risk_notes) if risk_notes is not None else None,
                ),
            )
        return Proposal(pid, now, now + ttl_seconds, user_address, tool, params, ptb_base64, "proposed")

    def proposal_view(self, proposal_id: str) -> dict[str, Any] | None:
        """Everything the signer page needs to render a confirmation card.

        A proposal_id is an unguessable 128-bit capability; holding it grants
        read access to this view only — executing still needs the owner's
        wallet signature over the exact bytes.
        """
        cur = self._conn.execute(
            """SELECT proposal_id, created_at, expires_at, user_address, tool,
                      params_json, ptb_base64, simulation_json, risk_notes_json,
                      summary_text, status, tx_digest
               FROM proposals WHERE proposal_id=?""",
            (proposal_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "proposal_id": r[0],
            "created_at": r[1],
            "expires_at": r[2],
            "user_address": r[3],
            "tool": r[4],
            "params": json.loads(r[5]),
            "ptb_base64": r[6],
            "simulation": json.loads(r[7]) if r[7] else None,
            "risk_notes": json.loads(r[8]) if r[8] else [],
            "human_summary": r[9],
            "status": r[10],
            "tx_digest": r[11],
        }

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        cur = self._conn.execute(
            """SELECT proposal_id, created_at, expires_at, user_address, tool,
                      params_json, ptb_base64, status
               FROM proposals WHERE proposal_id=?""",
            (proposal_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return Proposal(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6], r[7])

    def mark_proposal(
        self, proposal_id: str, status: str,
        tx_digest: str | None = None, result: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE proposals SET status=?, tx_digest=?, result_json=? WHERE proposal_id=?",
                (status, tx_digest, json.dumps(result) if result is not None else None, proposal_id),
            )

    def refresh_proposal_bytes(
        self, proposal_id: str, *, ptb_base64: str,
        simulation: dict[str, Any] | None, risk: dict[str, Any] | None,
        risk_notes: list[str] | None = None,
    ) -> bool:
        """Replace a live proposal's PTB with a rebuild of the same params.

        Oracle attestations (Pyth VAAs) are baked into the bytes at build time
        and go stale faster than a human can review and sign, so the signer
        refreshes the bytes immediately before the wallet prompt. Identity is
        unchanged on purpose: same proposal_id, same params, same expires_at —
        only bytes/simulation/risk move, and only while status is 'proposed'
        (a consumed or expired proposal can never be resurrected this way).
        risk_notes=None keeps the stored notes (used where they can't be
        regenerated faithfully)."""
        sets = "ptb_base64=?, simulation_json=?, risk_json=?"
        args: list[Any] = [
            ptb_base64,
            json.dumps(simulation) if simulation is not None else None,
            json.dumps(risk) if risk is not None else None,
        ]
        if risk_notes is not None:
            sets += ", risk_notes_json=?"
            args.append(json.dumps(risk_notes))
        args.append(proposal_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE proposals SET {sets} WHERE proposal_id=? AND status='proposed'",
                args,
            )
            return cur.rowcount == 1

    def audit_trail(self, user_address: str, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """SELECT proposal_id, created_at, tool, status, tx_digest, summary_text, expires_at
               FROM proposals WHERE user_address=? ORDER BY created_at DESC LIMIT ?""",
            (user_address, limit),
        )
        return [
            {
                "proposal_id": r[0], "created_at": r[1], "tool": r[2],
                "status": r[3], "tx_digest": r[4], "summary": r[5], "expires_at": r[6],
            }
            for r in cur.fetchall()
        ]

    # -- users / sessions / stats (dashboard addendum) --------------------------

    def upsert_user(self, address: str, source: str) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO users (address, source, first_seen, last_seen)
                   VALUES (?,?,?,?)
                   ON CONFLICT(address) DO UPDATE SET
                     last_seen=excluded.last_seen,
                     -- once a user has proven the address by wallet signature,
                     -- keep that stronger attribution
                     source=CASE WHEN users.source='wallet' THEN 'wallet' ELSE excluded.source END""",
                (address, source, now, now),
            )

    def count_users(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def distinct_position_addresses(self) -> list[str]:
        cur = self._conn.execute("SELECT DISTINCT user_address FROM obligation_caps")
        return [r[0] for r in cur.fetchall()]

    def put_session(self, token_id: str, address: str, ttl_seconds: int) -> None:
        """Store a session row. Token minting/verification (HMAC over the id)
        lives in auth.py; this table only decides expiry and revocation."""
        now = time.time()
        with self._lock, self._conn:
            # Prune: expired-and-never-revoked rows immediately (no audit
            # value); revoked rows only after the audit retention window.
            self._conn.execute(
                "DELETE FROM sessions WHERE expires_at < ? AND revoked_at IS NULL", (now,)
            )
            self._conn.execute(
                "DELETE FROM sessions WHERE revoked_at IS NOT NULL AND revoked_at < ?",
                (now - REVOKED_SESSION_RETENTION_SECONDS,),
            )
            self._conn.execute(
                "INSERT INTO sessions (token, address, created_at, expires_at) VALUES (?,?,?,?)",
                (token_id, address, now, now + ttl_seconds),
            )

    def session_address(self, token_id: str) -> str | None:
        r = self._conn.execute(
            "SELECT address, expires_at, revoked_at FROM sessions WHERE token=?", (token_id,)
        ).fetchone()
        if not r or r[1] < time.time() or r[2] is not None:
            return None
        return r[0]

    def revoke_session(self, token_id: str, reason: str | None = None) -> None:
        """Mark (not delete) so the audit trail keeps when/why. The token
        holder still just sees session_expired — the reason is internal."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET revoked_at=?, revocation_reason=? WHERE token=?",
                (time.time(), reason, token_id),
            )

    def revoke_sessions_for_address(self, address: str, reason: str | None = None) -> int:
        """Kill every live session for an address — 'End session' means end it
        everywhere: dashboard bearer AND any MCP connector tokens (OAuth or
        ?key=) minted for the same wallet. Returns how many were revoked."""
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE sessions SET revoked_at=?, revocation_reason=?"
                " WHERE address=? AND revoked_at IS NULL AND expires_at > ?",
                (now, reason, address, now),
            )
            return cur.rowcount

    def session_record(self, token_id: str) -> dict[str, Any] | None:
        """Full session row for internal auditing (includes revoked/expired
        rows still within retention). Never exposed to token holders."""
        r = self._conn.execute(
            "SELECT token, address, created_at, expires_at, revoked_at, revocation_reason"
            " FROM sessions WHERE token=?",
            (token_id,),
        ).fetchone()
        if not r:
            return None
        return {
            "token_id": r[0], "address": r[1], "created_at": r[2],
            "expires_at": r[3], "revoked_at": r[4], "revocation_reason": r[5],
        }

    # -- OAuth clients / authorization codes -----------------------------------

    def create_oauth_client(self, client_id: str, client_name: str | None,
                            redirect_uris: list[str]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO oauth_clients VALUES (?,?,?,?)",
                (client_id, client_name, json.dumps(redirect_uris), time.time()),
            )

    def get_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT client_id, client_name, redirect_uris FROM oauth_clients WHERE client_id=?",
            (client_id,),
        ).fetchone()
        if not r:
            return None
        return {"client_id": r[0], "client_name": r[1], "redirect_uris": json.loads(r[2])}

    def put_oauth_code(self, code: str, client_id: str, redirect_uri: str,
                       code_challenge: str, address: str, ttl_seconds: int) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            self._conn.execute(
                "INSERT INTO oauth_codes VALUES (?,?,?,?,?,?,?,0)",
                (code, client_id, redirect_uri, code_challenge, address, now, now + ttl_seconds),
            )

    def consume_oauth_code(self, code: str) -> dict[str, Any] | None:
        """Single-use: the first caller gets the row, everyone after gets None
        (a replayed code must fail even inside its TTL)."""
        with self._lock, self._conn:
            r = self._conn.execute(
                "SELECT client_id, redirect_uri, code_challenge, address, expires_at, used"
                " FROM oauth_codes WHERE code=?",
                (code,),
            ).fetchone()
            if not r or r[5] or r[4] < time.time():
                return None
            self._conn.execute("UPDATE oauth_codes SET used=1 WHERE code=?", (code,))
        return {"client_id": r[0], "redirect_uri": r[1], "code_challenge": r[2], "address": r[3]}

    # ------------------------------------------------------------- passkeys

    def put_passkey(self, *, credential_id: str, address: str,
                    public_key: bytes, sign_count: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO passkeys (credential_id,address,public_key,sign_count,created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(credential_id) DO UPDATE SET
                       sign_count=excluded.sign_count""",
                (credential_id, address.lower(), public_key, sign_count, time.time()),
            )

    def get_passkey(self, credential_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT credential_id,address,public_key,sign_count,last_used_at "
            "FROM passkeys WHERE credential_id=?", (credential_id,),
        ).fetchone()
        if not r:
            return None
        return {"credential_id": r[0], "address": r[1], "public_key": bytes(r[2]),
                "sign_count": r[3], "last_used_at": r[4]}

    def passkeys_for_address(self, address: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT credential_id,sign_count,created_at,last_used_at "
            "FROM passkeys WHERE address=? ORDER BY created_at", (address.lower(),),
        ).fetchall()
        return [{"credential_id": r[0], "sign_count": r[1],
                 "created_at": r[2], "last_used_at": r[3]} for r in rows]

    def touch_passkey(self, credential_id: str, *, sign_count: int, verified_at: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE passkeys SET sign_count=?, last_used_at=? WHERE credential_id=?",
                (sign_count, verified_at, credential_id),
            )

    def consume_passkey_verification(self, address: str) -> None:
        """Spend the current assertion so it cannot authorize a second action.

        Always Ask means every trade, not "the first trade and then anything
        else for an hour". Without this, one verification covered the whole
        session window and trades 2..n went through with no prompt — which is
        both weaker than the mode's name promises and weaker than the design it
        was modelled on, where an approval is valid for exactly one
        transaction and a captured authorization cannot be replayed.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE passkeys SET last_used_at=NULL WHERE address=?",
                (address.lower(),),
            )

    def last_passkey_verification(self, address: str) -> float | None:
        r = self._conn.execute(
            "SELECT MAX(last_used_at) FROM passkeys WHERE address=?", (address.lower(),),
        ).fetchone()
        return r[0] if r and r[0] is not None else None

    def delete_passkeys_for_address(self, address: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM passkeys WHERE address=?", (address.lower(),))
        return cur.rowcount

    def put_passkey_challenge(self, *, challenge_id: str, address: str,
                              purpose: str, expires_at: float) -> None:
        with self._lock, self._conn:
            # Opportunistically prune: challenges are worthless once expired.
            self._conn.execute("DELETE FROM passkey_challenges WHERE expires_at < ?", (time.time(),))
            self._conn.execute(
                "INSERT INTO passkey_challenges VALUES (?,?,?,?,0)",
                (challenge_id, address.lower(), purpose, expires_at),
            )

    def consume_passkey_challenge(self, challenge_id: str) -> dict[str, Any] | None:
        """Single-use: returns the row and marks it used in one transaction."""
        with self._lock, self._conn:
            r = self._conn.execute(
                "SELECT address,purpose,expires_at,used FROM passkey_challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchone()
            if not r or r[3]:
                return None
            self._conn.execute(
                "UPDATE passkey_challenges SET used=1 WHERE challenge_id=?", (challenge_id,)
            )
        return {"address": r[0], "purpose": r[1], "expires_at": r[2]}

    # --------------------------------------------------------------- grants

    def put_grant(self, *, address: str, session_address: str, sealed_key: str,
                  delegate: str, router: str, stable: str, expiry: int,
                  per_trade_cap: int, daily_cap: int,
                  approval_mode: str = "always_ask",
                  autonomous_limit: int = 0) -> None:
        """Record a grant, replacing any previous one for this address.

        REPLACE rather than INSERT because the contract keeps exactly one
        grant per account: authorising again overwrites it on-chain, so
        keeping the old row would leave Sarf signing with a key the contract
        has already retired.
        """
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO grants
                   (address,session_address,sealed_key,delegate,router,stable,expiry,
                    per_trade_cap,daily_cap,created_at,rotated_at,revoked_at,
                    approval_mode,autonomous_limit)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                (address.lower(), session_address, sealed_key, delegate.lower(),
                 router.lower(), stable.lower(), int(expiry), int(per_trade_cap),
                 int(daily_cap), now, now,
                 "autonomous",
                 int(autonomous_limit)),
            )

    def get_grant(self, address: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            """SELECT address,session_address,sealed_key,delegate,router,stable,expiry,
                      per_trade_cap,daily_cap,created_at,rotated_at,revoked_at,
                      approval_mode,autonomous_limit
               FROM grants WHERE address=?""", (address.lower(),)
        ).fetchone()
        if not r:
            return None
        keys = ("address", "session_address", "sealed_key", "delegate", "router",
                "stable", "expiry", "per_trade_cap", "daily_cap", "created_at",
                "rotated_at", "revoked_at", "approval_mode", "autonomous_limit")
        # strict=True because zip() truncates to the shorter side by default:
        # adding a column to the SELECT above and forgetting it here would
        # silently drop it, and the field that goes missing is the one deciding
        # whether a trade needs a passkey. Loud beats subtly wrong.
        return dict(zip(keys, r, strict=True))

    def put_risk_params(self, *, address: str, symbol: str,
                        stop_loss: float | None, take_profit: float | None) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO risk_params (address,symbol,stop_loss,take_profit,
                                            created_at,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(address,symbol) DO UPDATE SET
                       stop_loss=excluded.stop_loss,
                       take_profit=excluded.take_profit,
                       updated_at=excluded.updated_at""",
                (address.lower(), symbol.upper(), stop_loss, take_profit, now, now),
            )

    def risk_params_for(self, address: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT symbol,stop_loss,take_profit,updated_at FROM risk_params
               WHERE address=? ORDER BY symbol""", (address.lower(),)
        ).fetchall()
        return [dict(zip(("symbol", "stop_loss", "take_profit", "updated_at"), r,
                         strict=True)) for r in rows]

    def clear_risk_params(self, address: str, symbol: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM risk_params WHERE address=? AND symbol=?",
                (address.lower(), symbol.upper()))
        return cur.rowcount > 0

    def revoke_grant(self, address: str) -> bool:
        """Mark a grant revoked locally and destroy the key material. NOT the
        security boundary — the on-chain revoke() is. This stops Sarf from
        trying to use the key; the contract is what stops anyone else.

        The sealed key is blanked rather than kept: a revoked grant will never
        be signed with again (every path checks revoked_at first), so retaining
        an encrypted signing key for it is storage with no purpose and a
        window if the session secret ever leaks. The row survives for the audit
        trail; only the secret goes.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE grants SET revoked_at=?, sealed_key='' "
                "WHERE address=? AND revoked_at IS NULL",
                (time.time(), address.lower()),
            )
        return cur.rowcount > 0

    def expire_grants(self, now: float | None = None) -> int:
        """Retire every grant whose expiry has passed. -> rows retired.

        Expiry is already enforced on-chain — SarfSessionKey refuses a swap
        past `expiry`, and nothing here can extend that — so this is not what
        stops a trade. It closes two gaps on our side of the line:

        1. Sarf stops holding a signing key for a grant that can no longer
           authorise anything.
        2. The row stops reading as live. A grant that lapsed an hour ago was
           still returned with its session key attached, and every surface that
           only asked "is there a grant?" showed the expired one as the
           account's current key.

        Idempotent, so it is safe to call on every read as well as on a timer.
        """
        t = time.time() if now is None else now
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE grants SET revoked_at=?, sealed_key='' "
                "WHERE revoked_at IS NULL AND expiry <= ?",
                (t, t),
            )
        return cur.rowcount

    def rotate_grant_key(self, address: str, *, session_address: str, sealed_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE grants SET session_address=?, sealed_key=?, rotated_at=? WHERE address=?",
                (session_address, sealed_key, time.time(), address.lower()),
            )

    # --------------------------------------------------------------- orders

    def create_order(self, *, address: str, side: str, symbol: str, amount_in: int,
                     quoted_out: int, est_usd: float | None, tx: dict[str, Any],
                     ttl_seconds: int, display: dict[str, Any] | None = None) -> str:
        order_id = "sarf_ord_" + uuid.uuid4().hex
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO orders (order_id,created_at,expires_at,address,side,symbol,
                                       amount_in,quoted_out,est_usd,tx_json,status,display_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'proposed',?)""",
                (order_id, now, now + ttl_seconds, address.lower(), side, symbol,
                 str(amount_in), str(quoted_out), est_usd, json.dumps(tx),
                 json.dumps(display) if display else None),
            )
        return order_id

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT order_id,created_at,expires_at,address,side,symbol,amount_in,"
            "quoted_out,est_usd,tx_json,status,tx_hash,result_json,display_json "
            "FROM orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if not r:
            return None
        out = {
            "order_id": r[0], "created_at": r[1], "expires_at": r[2], "address": r[3],
            "side": r[4], "symbol": r[5], "amount_in": r[6], "quoted_out": r[7],
            "est_usd": r[8], "tx": json.loads(r[9]), "status": r[10],
            "tx_hash": r[11], "result": json.loads(r[12]) if r[12] else None,
            "expired": r[2] < time.time(),
        }
        # Display fields (summary, risk notes, fee, formatted amounts) never
        # override the authoritative columns above.
        if r[13]:
            for k, v in json.loads(r[13]).items():
                out.setdefault(k, v)
        return out

    def mark_order(self, order_id: str, status: str, *, tx_hash: str | None = None,
                   result: dict[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE orders SET status=?, tx_hash=COALESCE(?,tx_hash), "
                "result_json=COALESCE(?,result_json) WHERE order_id=?",
                (status, tx_hash, json.dumps(result) if result else None, order_id),
            )

    def orders_for_address(self, address: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT order_id,created_at,side,symbol,amount_in,quoted_out,est_usd,status,tx_hash "
            "FROM orders WHERE address=? ORDER BY created_at DESC LIMIT ?",
            (address.lower(), int(limit)),
        ).fetchall()
        return [{"order_id": r[0], "created_at": r[1], "side": r[2], "symbol": r[3],
                 "amount_in": r[4], "quoted_out": r[5], "est_usd": r[6],
                 "status": r[7], "tx_hash": r[8]} for r in rows]

    def set_stat(self, key: str, value: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO stats VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                                                  updated_at=excluded.updated_at""",
                (key, json.dumps(value), time.time()),
            )

    def get_stat(self, key: str) -> tuple[dict[str, Any], float] | None:
        r = self._conn.execute(
            "SELECT value_json, updated_at FROM stats WHERE key=?", (key,)
        ).fetchone()
        return (json.loads(r[0]), r[1]) if r else None
