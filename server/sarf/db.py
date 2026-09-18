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

-- Refresh tokens, so a connector stays connected.
--
-- Access tokens are 30 minutes, which is right for a credential that travels
-- in headers and sometimes in URLs. Without a way to renew one, though, that
-- number was also how long an MCP connector worked before Claude showed
-- "Reconnect" and the user had to sign with their wallet again — several times
-- a day, for a connection they had already approved. Steady is a feature.
--
-- The security model is unchanged, because the access token was never what
-- moved money: signing needs the user's wallet or their passkey, in-chat
-- execution needs a session-key grant that expires on its own an hour after it
-- is granted, and transfers can never be delegated at all. A refresh token
-- renews the right to READ and to BUILD unsigned transactions.
--
-- Rotating, single-use, with reuse detection: every refresh returns a new
-- token in the same family and burns the old one. A token presented twice
-- means a copy is in circulation, so the whole family dies immediately and the
-- user signs in again. `expires_at` is the family's absolute end and is NOT
-- extended by rotation — an unattended connector still lapses.
CREATE TABLE IF NOT EXISTS oauth_refresh (
  token_id          TEXT PRIMARY KEY,
  family_id         TEXT NOT NULL,
  address           TEXT NOT NULL,
  client_id         TEXT NOT NULL,
  client_name       TEXT,
  created_at        REAL NOT NULL,
  expires_at        REAL NOT NULL,
  used_at           REAL,
  revoked_at        REAL,
  revocation_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_family ON oauth_refresh(family_id);
CREATE INDEX IF NOT EXISTS idx_refresh_addr ON oauth_refresh(address, revoked_at);

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

-- Deposits in flight: a CCTP burn on Base waiting to be minted on X Layer.
--
-- This table is the difference between a deposit and a lost afternoon. The
-- burn is signed in the browser and the mint happens a few seconds later, so
-- the first version held the transaction hash in React state — close the tab
-- in between and nothing in the product knew a deposit existed. The money was
-- never at risk (Circle holds the attestation, and the mint can only ever pay
-- the recipient inside the signed message) but only the user could rescue it,
-- and only if they had kept the hash.
--
-- Recorded here the moment it is broadcast, a sweeper finishes it whether or
-- not anyone is watching.
CREATE TABLE IF NOT EXISTS deposits (
  burn_tx     TEXT PRIMARY KEY,
  address     TEXT NOT NULL,
  amount_usd  REAL,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending|minted|failed
  mint_tx     TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_deposits_addr ON deposits(address, created_at);

-- Gas Sarf has given away on Base so a user could sign their own burn.
--
-- One row per top-up, not one per address, because the question this has to
-- answer is "how much in the last day" and a running total cannot be aged out.
-- It is an audit log of value leaving the relayer as much as it is a throttle.
CREATE TABLE IF NOT EXISTS gas_drips (
  tx_hash    TEXT PRIMARY KEY,
  address    TEXT NOT NULL,
  wei        TEXT NOT NULL,        -- decimal string: wei overflows SQLite ints
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gas_drips_addr ON gas_drips(address, created_at);

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

-- Signed trade receipts issued via the Nota protocol (github.com/davieslennox0/nota,
-- Sui + Walrus) after an order confirms on-chain. One row per order.
-- `status` is honest about whether issuance actually happened --
-- 'skipped_not_configured' when no Nota/Sui credentials are set server-side,
-- never a fabricated receipt.
CREATE TABLE IF NOT EXISTS trade_receipts (
  order_id    TEXT PRIMARY KEY,
  address     TEXT NOT NULL,
  status      TEXT NOT NULL,
  receipt_id  TEXT,
  blob_id     TEXT,
  view_url    TEXT,
  tx_digest   TEXT,
  detail      TEXT,
  created_at  REAL NOT NULL
);
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
    # Which client this session was minted for. The dashboard could say "a
    # session is live" and nothing more, which is a poor answer to the question
    # people actually have — *what* is connected to my wallet? The name comes
    # from RFC 7591 dynamic registration, i.e. the client's own declaration
    # ("Claude"), so it is a label, not an authenticated fact: it says which
    # registration minted the token, and two clients could register the same
    # name. It is displayed as such and nothing is authorised on the strength
    # of it.
    "ALTER TABLE sessions ADD COLUMN client_name TEXT",
    "ALTER TABLE sessions ADD COLUMN client_id TEXT",
    # Agent-to-agent grants were built and then dropped from scope before
    # they ever shipped. A dev database that ran that build may still hold the
    # two tables, and they store bearer-token hashes, which should not
    # outlive the code that checked them. Order rows are left alone: any
    # orders.origin / agent_grant_id columns such a database gained are
    # simply no longer read.
    "DROP TABLE IF EXISTS agent_grant_usage",
    "DROP TABLE IF EXISTS agent_grants",
    # Stop-loss/take-profit auto-fire bookkeeping: the last time this level
    # fired (or attempted to) and what happened, so the watcher never
    # re-fires the same breach every poll interval.
    "ALTER TABLE risk_params ADD COLUMN last_triggered_at REAL",
    "ALTER TABLE risk_params ADD COLUMN last_trigger_status TEXT",
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

# Admin console action log.
#
# The console can revoke someone's sessions, kill their session-key grant and
# re-queue a stuck deposit. Those are small powers next to signing, but they
# are the first things in this system one person can do TO another, and the
# whole argument for allowing an email-shaped identity to hold them (see
# privy_auth.py) rests on them being few, bounded and visible afterwards.
# This table is the "visible afterwards" half, so it is written before the
# action runs, not after it succeeds — an action that blew up halfway is
# exactly the one worth having a record of.
#
# `actor_email` is the Google address off the verified identity token and
# `actor_address` the wallet session it was presented alongside; both are
# recorded because either alone leaves an ambiguous trail.
# Single-asset zap positions (xlayer/zap.py). One row per position; the
# position's full transition history lives in zap_events. `flow` is the
# multi-transaction sequence in progress (enter / exit / reenter) and
# `flow_step` how far through it the wallet has signed. `flow_ctx` holds the
# amounts each confirmed step actually credited, read back from its receipt.
# Steps are never built from estimates.
_ZAP_TABLES = """
CREATE TABLE IF NOT EXISTS zap_positions (
  position_id      TEXT PRIMARY KEY,
  address          TEXT NOT NULL,
  pool_key         TEXT NOT NULL,
  deposit_symbol   TEXT NOT NULL,
  deposit_amount   TEXT NOT NULL,
  deposit_usd      REAL,
  il_threshold_bps INTEGER NOT NULL,
  reentry_bps      INTEGER NOT NULL,
  state            TEXT NOT NULL,
  p_initial        REAL,
  lp_index_initial REAL,
  entered_at       REAL,
  lp_amount        TEXT,
  hold_rwa         TEXT,
  hold_other       TEXT,
  parked_amount    TEXT,
  parked_index     TEXT,
  flow             TEXT,
  flow_step        INTEGER NOT NULL DEFAULT 0,
  flow_ctx         TEXT NOT NULL DEFAULT '{}',
  pending_tx       TEXT,
  last_price       REAL,
  last_il_bps      REAL,
  last_checked_at  REAL,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zap_addr ON zap_positions(address, created_at);

CREATE TABLE IF NOT EXISTS zap_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id  TEXT NOT NULL,
  at           REAL NOT NULL,
  kind         TEXT NOT NULL,
  detail_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_zap_events_pos ON zap_events(position_id, at);
"""

_ADMIN_TABLE = """
CREATE TABLE IF NOT EXISTS admin_audit (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at    REAL NOT NULL,
  actor_email   TEXT NOT NULL,
  actor_address TEXT,
  action        TEXT NOT NULL,
  target        TEXT,
  detail_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_time ON admin_audit(created_at DESC);
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
        self._conn.executescript(_ADMIN_TABLE)
        self._conn.executescript(_ZAP_TABLES)
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

    def put_session(self, token_id: str, address: str, ttl_seconds: int,
                    *, client_name: str | None = None,
                    client_id: str | None = None) -> None:
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
                "INSERT INTO sessions (token, address, created_at, expires_at,"
                " client_name, client_id) VALUES (?,?,?,?,?,?)",
                (token_id, address, now, now + ttl_seconds, client_name, client_id),
            )

    def active_sessions(self, address: str) -> list[dict[str, Any]]:
        """Live sessions for an address, newest first.

        What "connected" means on the dashboard: an unrevoked, unexpired token.
        The client name is whatever the OAuth client registered as — Claude
        calls itself Claude — and is None for a session minted on the website
        or for a legacy ?key= connector, which the caller renders as such
        rather than inventing an attribution.
        """
        rows = self._conn.execute(
            "SELECT token, created_at, expires_at, client_name, client_id FROM sessions "
            "WHERE address=? AND revoked_at IS NULL AND expires_at > ? "
            "ORDER BY created_at DESC",
            (address.lower(), time.time()),
        ).fetchall()
        return [
            {
                # The token id is a credential half — never returned. The row is
                # identified by when it was created, which is all the UI needs.
                "created_at": r[1],
                "expires_at": r[2],
                "client_name": r[3],
                "client_id": r[4],
            }
            for r in rows
        ]

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

    # -- OAuth refresh tokens --------------------------------------------------
    # See the oauth_refresh schema comment for why these exist and what they
    # can and cannot renew.

    def put_refresh(self, *, token_id: str, family_id: str, address: str,
                    client_id: str, client_name: str | None,
                    expires_at: float) -> None:
        now = time.time()
        with self._lock, self._conn:
            # Prune spent and long-dead rows. A used token is kept until its
            # family expires, because "this was presented twice" is exactly the
            # thing the row is there to be able to notice.
            self._conn.execute("DELETE FROM oauth_refresh WHERE expires_at < ?",
                               (now - REVOKED_SESSION_RETENTION_SECONDS,))
            self._conn.execute(
                "INSERT INTO oauth_refresh (token_id,family_id,address,client_id,"
                "client_name,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                (token_id, family_id, address.lower(), client_id, client_name,
                 now, expires_at),
            )

    def refresh_record(self, token_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT token_id,family_id,address,client_id,client_name,created_at,"
            "expires_at,used_at,revoked_at FROM oauth_refresh WHERE token_id=?",
            (token_id,),
        ).fetchone()
        if not r:
            return None
        return dict(zip(("token_id", "family_id", "address", "client_id", "client_name",
                         "created_at", "expires_at", "used_at", "revoked_at"),
                        r, strict=True))

    def consume_refresh(self, token_id: str) -> dict[str, Any] | None:
        """Spend a refresh token exactly once. -> the row, or None.

        The UPDATE is the check: only an unused, unrevoked, unexpired row is
        claimed, and SQLite serialises it, so two clients racing the same token
        cannot both win. None means "not spendable" — the caller distinguishes
        never-existed from already-spent by reading the row afterwards, because
        already-spent is a theft signal and never-existed is a typo.
        """
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE oauth_refresh SET used_at=? WHERE token_id=? AND used_at IS NULL"
                " AND revoked_at IS NULL AND expires_at > ?",
                (now, token_id, now),
            )
            if cur.rowcount != 1:
                return None
        return self.refresh_record(token_id)

    def revoke_refresh_family(self, family_id: str, reason: str) -> int:
        """Kill a whole rotation chain. Returns how many rows were live."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE oauth_refresh SET revoked_at=?, revocation_reason=?"
                " WHERE family_id=? AND revoked_at IS NULL",
                (time.time(), reason, family_id),
            )
            return cur.rowcount

    def revoke_refresh_for_address(self, address: str, reason: str) -> int:
        """What makes "End session" actually end it: without this, a connector
        holding a refresh token would simply mint itself a new access token and
        the disconnect would last thirty seconds."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE oauth_refresh SET revoked_at=?, revocation_reason=?"
                " WHERE address=? AND revoked_at IS NULL",
                (time.time(), reason, address.lower()),
            )
            return cur.rowcount

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
                       updated_at=excluded.updated_at,
                       last_triggered_at=NULL,
                       last_trigger_status=NULL""",
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

    # ------------------------------------------------------------------ zap

    _ZAP_JSON = ("flow_ctx",)

    def create_zap_position(self, **cols: Any) -> str:
        position_id = "zap_" + uuid.uuid4().hex
        now = time.time()
        row = {"position_id": position_id, "created_at": now, "updated_at": now, **cols}
        if "flow_ctx" in row:
            row["flow_ctx"] = json.dumps(row["flow_ctx"])
        keys = ",".join(row)
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO zap_positions ({keys}) VALUES ({','.join('?' * len(row))})",
                tuple(row.values()),
            )
        return position_id

    def get_zap_position(self, position_id: str) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM zap_positions WHERE position_id=?",
                                 (position_id,))
        r = cur.fetchone()
        if not r:
            return None
        out = dict(zip([c[0] for c in cur.description], r))
        out["flow_ctx"] = json.loads(out["flow_ctx"] or "{}")
        return out

    def zap_positions_for(self, address: str) -> list[dict[str, Any]]:
        ids = self._conn.execute(
            "SELECT position_id FROM zap_positions WHERE address=? ORDER BY created_at DESC",
            (address.lower(),),
        ).fetchall()
        return [p for (i,) in ids if (p := self.get_zap_position(i))]

    def zap_positions_in(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        ids = self._conn.execute(
            f"SELECT position_id FROM zap_positions WHERE state IN ({','.join('?' * len(states))})",
            states,
        ).fetchall()
        return [p for (i,) in ids if (p := self.get_zap_position(i))]

    def update_zap_position(self, position_id: str, *, expect_state: str | None = None,
                            **cols: Any) -> bool:
        """Update columns; with expect_state, only if the row is still in that
        state. The watcher and a user's click can race on the same position,
        and the loser of that race must not overwrite the winner's transition."""
        if "flow_ctx" in cols:
            cols["flow_ctx"] = json.dumps(cols["flow_ctx"])
        cols["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in cols)
        sql = f"UPDATE zap_positions SET {sets} WHERE position_id=?"
        args: tuple[Any, ...] = (*cols.values(), position_id)
        if expect_state is not None:
            sql += " AND state=?"
            args += (expect_state,)
        with self._lock, self._conn:
            return self._conn.execute(sql, args).rowcount == 1

    def log_zap_event(self, position_id: str, kind: str, detail: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO zap_events (position_id,at,kind,detail_json) VALUES (?,?,?,?)",
                (position_id, time.time(), kind, json.dumps(detail, default=str)),
            )

    def zap_events(self, position_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT at,kind,detail_json FROM zap_events WHERE position_id=? ORDER BY id",
            (position_id,),
        ).fetchall()
        return [{"at": a, "kind": k, **json.loads(d)} for a, k, d in rows]

    def all_risk_params(self) -> list[dict[str, Any]]:
        """Every armed level, across every address -- what the watcher polls."""
        rows = self._conn.execute(
            "SELECT address,symbol,stop_loss,take_profit,last_triggered_at,"
            "last_trigger_status FROM risk_params "
            "WHERE stop_loss IS NOT NULL OR take_profit IS NOT NULL"
        ).fetchall()
        return [dict(zip(("address", "symbol", "stop_loss", "take_profit",
                          "last_triggered_at", "last_trigger_status"), r, strict=True))
                for r in rows]

    def mark_risk_triggered(self, address: str, symbol: str, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE risk_params SET last_triggered_at=?, last_trigger_status=? "
                "WHERE address=? AND symbol=?",
                (time.time(), status, address.lower(), symbol.upper()),
            )

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

    # ------------------------------------------------------------- deposits

    def record_deposit(self, *, burn_tx: str, address: str,
                       amount_usd: float | None) -> None:
        """Remember a burn the moment it is broadcast.

        INSERT OR IGNORE: the browser records it and may record it again on a
        retry, and a deposit is identified by its burn hash. Re-recording must
        never reset the status of one that has already been minted.
        """
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO deposits "
                "(burn_tx,address,amount_usd,created_at,updated_at,status) "
                "VALUES (?,?,?,?,?,'pending')",
                (burn_tx.lower(), address.lower(), amount_usd, now, now),
            )

    def pending_deposits(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT burn_tx,address,amount_usd,created_at,attempts FROM deposits "
            "WHERE status='pending' ORDER BY created_at LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(zip(("burn_tx", "address", "amount_usd", "created_at", "attempts"),
                         r, strict=True)) for r in rows]

    def settle_deposit(self, burn_tx: str, mint_tx: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deposits SET status='minted', mint_tx=?, updated_at=?, "
                "last_error=NULL WHERE burn_tx=?",
                (mint_tx, time.time(), burn_tx.lower()),
            )

    def note_deposit_attempt(self, burn_tx: str, error: str | None) -> int:
        """Count a try and record why it did not land. -> attempts so far.

        Failure is not final here: Circle attests when it attests, and an RPC
        that refused a minute ago will take the same transaction later. The
        count exists so a deposit that can never settle stops being retried
        forever, not so one bad minute buries it.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deposits SET attempts=attempts+1, updated_at=?, last_error=? "
                "WHERE burn_tx=?", (time.time(), (error or "")[:300], burn_tx.lower()),
            )
            r = self._conn.execute(
                "SELECT attempts FROM deposits WHERE burn_tx=?", (burn_tx.lower(),)
            ).fetchone()
        return int(r[0]) if r else 0

    def fail_deposit(self, burn_tx: str, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deposits SET status='failed', updated_at=?, last_error=? "
                "WHERE burn_tx=?", (time.time(), error[:300], burn_tx.lower()),
            )

    def deposits_for_address(self, address: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT burn_tx,amount_usd,created_at,status,mint_tx,last_error "
            "FROM deposits WHERE address=? ORDER BY created_at DESC LIMIT ?",
            (address.lower(), int(limit)),
        ).fetchall()
        return [dict(zip(("burn_tx", "amount_usd", "created_at", "status",
                          "mint_tx", "last_error"), r, strict=True)) for r in rows]

    def gas_given_since(self, address: str, since: float) -> int:
        """Wei sent to this address as gas since `since`. -> 0 when none."""
        rows = self._conn.execute(
            "SELECT wei FROM gas_drips WHERE address=? AND created_at>=?",
            (address.lower(), float(since)),
        ).fetchall()
        return sum(int(r[0]) for r in rows)

    def record_gas_drip(self, *, tx_hash: str, address: str, wei: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO gas_drips (tx_hash,address,wei,created_at) "
                "VALUES (?,?,?,?)",
                (tx_hash.lower(), address.lower(), str(int(wei)), time.time()),
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

    # ------------------------------------------------------------- receipts

    def record_trade_receipt(self, *, order_id: str, address: str,
                             status: str, receipt_id: str | None = None,
                             blob_id: str | None = None, view_url: str | None = None,
                             tx_digest: str | None = None, detail: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO trade_receipts (order_id,address,status,receipt_id,
                                               blob_id,view_url,tx_digest,detail,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(order_id) DO UPDATE SET
                       status=excluded.status, receipt_id=excluded.receipt_id,
                       blob_id=excluded.blob_id, view_url=excluded.view_url,
                       tx_digest=excluded.tx_digest, detail=excluded.detail""",
                (order_id, address.lower(), status, receipt_id, blob_id,
                 view_url, tx_digest, detail, time.time()),
            )

    def get_trade_receipt(self, order_id: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT order_id,status,receipt_id,blob_id,view_url,tx_digest,detail,created_at "
            "FROM trade_receipts WHERE order_id=?", (order_id,),
        ).fetchone()
        if not r:
            return None
        return {"order_id": r[0], "status": r[1], "receipt_id": r[2], "blob_id": r[3],
                "view_url": r[4], "tx_digest": r[5], "detail": r[6], "created_at": r[7]}

    # -------------------------------------------------------------- xpoints
    # v1, deliberately simple: 1 point per $10 of CONFIRMED trade volume (est_usd
    # at order time), plus a flat bonus per confirmed trade. Computed live from
    # `orders` -- the same rows get_status and the dashboard already show -- not
    # a separate ledger that could drift from what actually executed. Easy to
    # retune (see xlayer_rwa.py get_xpoints) or replace with a real ledger later.

    def xpoints_activity(self, address: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(est_usd),0) "
            "FROM orders WHERE address=? AND status='confirmed'",
            (address.lower(),),
        ).fetchone()
        return {
            "confirmed_trades": int(row[0] or 0),
            "confirmed_volume_usd": float(row[1] or 0),
        }

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

    # ---------------------------------------------------------------- admin
    # Read methods for the operator console. Every one of them is an
    # AGGREGATE or a recent-rows page: the console answers "how is the service
    # doing" and "what is stuck", not "what is in this person's wallet". No
    # method here returns a sealed key, a public key blob, a session token or
    # a proposal body, and that is a property to preserve when adding to this
    # section — the console is the one surface where one account's operator
    # looks at another account's rows, so what it CANNOT show matters as much
    # as what it can.

    def user_stats(self, now: float | None = None) -> dict[str, Any]:
        """Signup and activity counters."""
        t = time.time() if now is None else now
        row = self._conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END),
                      SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END),
                      SUM(CASE WHEN last_seen  >= ? THEN 1 ELSE 0 END),
                      SUM(CASE WHEN source = 'wallet' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN source = 'mcp'    THEN 1 ELSE 0 END)
               FROM users""",
            (t - 86400, t - 7 * 86400, t - 86400),
        ).fetchone()
        return {
            "total": int(row[0] or 0),
            "new_24h": int(row[1] or 0),
            "new_7d": int(row[2] or 0),
            "active_24h": int(row[3] or 0),
            "by_source": {"wallet": int(row[4] or 0), "mcp": int(row[5] or 0)},
        }

    def recent_users(self, limit: int = 50, offset: int = 0,
                     q: str | None = None) -> list[dict[str, Any]]:
        """Most recently active accounts, newest first.

        `q` is an address substring. It is passed as a bound LIKE parameter
        with the wildcards added here rather than interpolated, so a search
        box cannot become a query.
        """
        sql = ("SELECT address, source, first_seen, last_seen FROM users")
        args: list[Any] = []
        if q:
            sql += " WHERE address LIKE ?"
            args.append(f"%{q.strip().lower()}%")
        sql += " ORDER BY last_seen DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        rows = self._conn.execute(sql, args).fetchall()
        return [dict(zip(("address", "source", "first_seen", "last_seen"), r,
                         strict=True)) for r in rows]

    def session_stats(self, now: float | None = None) -> dict[str, Any]:
        """Live sessions, and which client each was minted for.

        Live means unexpired AND unrevoked — the same test session_address()
        applies, so this cannot report a session the auth layer would reject.
        """
        t = time.time() if now is None else now
        live = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT address) FROM sessions "
            "WHERE expires_at > ? AND revoked_at IS NULL", (t,),
        ).fetchone()
        by_client = self._conn.execute(
            "SELECT COALESCE(client_name,'(unnamed)'), COUNT(*) FROM sessions "
            "WHERE expires_at > ? AND revoked_at IS NULL "
            "GROUP BY 1 ORDER BY 2 DESC", (t,),
        ).fetchall()
        minted = self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE created_at >= ?", (t - 86400,),
        ).fetchone()
        revoked = self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE revoked_at >= ?", (t - 86400,),
        ).fetchone()
        return {
            "live": int(live[0] or 0),
            "live_accounts": int(live[1] or 0),
            "minted_24h": int(minted[0] or 0),
            "revoked_24h": int(revoked[0] or 0),
            "by_client": [{"client": r[0], "count": int(r[1])} for r in by_client],
        }

    def order_stats(self, now: float | None = None) -> dict[str, Any]:
        """Order counts, status mix and quoted volume.

        est_usd is the QUOTE at build time, not a settled amount — an order
        that was proposed and never signed still carries one. `volume_usd` is
        therefore restricted to orders that reached the chain, and the
        proposed-but-unsigned figure is reported separately rather than folded
        in, because the difference between "quoted" and "traded" is the whole
        question anyone asks this panel.
        """
        t = time.time() if now is None else now
        settled = ("submitted", "confirmed", "executed")
        marks = ",".join("?" * len(settled))
        total = self._conn.execute("SELECT COUNT(*) FROM orders").fetchone()
        by_status = self._conn.execute(
            "SELECT status, COUNT(*) FROM orders GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        vol = self._conn.execute(
            f"SELECT COALESCE(SUM(est_usd),0), COUNT(*) FROM orders "
            f"WHERE status IN ({marks})", settled,
        ).fetchone()
        vol24 = self._conn.execute(
            f"SELECT COALESCE(SUM(est_usd),0), COUNT(*) FROM orders "
            f"WHERE status IN ({marks}) AND created_at >= ?", (*settled, t - 86400),
        ).fetchone()
        unsigned = self._conn.execute(
            "SELECT COALESCE(SUM(est_usd),0), COUNT(*) FROM orders WHERE status='proposed'"
        ).fetchone()
        top = self._conn.execute(
            f"SELECT symbol, COUNT(*), COALESCE(SUM(est_usd),0) FROM orders "
            f"WHERE status IN ({marks}) GROUP BY 1 ORDER BY 2 DESC LIMIT 8", settled,
        ).fetchall()
        return {
            "total": int(total[0] or 0),
            "by_status": [{"status": r[0], "count": int(r[1])} for r in by_status],
            "settled_count": int(vol[1] or 0),
            "volume_usd": float(vol[0] or 0.0),
            "settled_count_24h": int(vol24[1] or 0),
            "volume_usd_24h": float(vol24[0] or 0.0),
            "unsigned_count": int(unsigned[1] or 0),
            "unsigned_usd": float(unsigned[0] or 0.0),
            "top_symbols": [
                {"symbol": r[0], "count": int(r[1]), "usd": float(r[2] or 0.0)} for r in top
            ],
        }

    def recent_orders(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT order_id,created_at,address,side,symbol,est_usd,status,tx_hash "
               "FROM orders")
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self._conn.execute(sql, args).fetchall()
        return [dict(zip(("order_id", "created_at", "address", "side", "symbol",
                          "est_usd", "status", "tx_hash"), r, strict=True)) for r in rows]

    def deposit_stats(self, now: float | None = None, stuck_after: float = 3600.0) -> dict[str, Any]:
        """Deposit health, including the number that need a human.

        "Stuck" is pending AND older than an hour. Circle's attestation is
        usually seconds and the sweeper retries for hours before giving up, so
        anything still pending after an hour is not slow, it is wrong — and it
        is the single number this console exists to surface, because until now
        nothing anywhere reported it.
        """
        t = time.time() if now is None else now
        by_status = self._conn.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(amount_usd),0) FROM deposits "
            "GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        stuck = self._conn.execute(
            "SELECT COUNT(*) FROM deposits WHERE status='pending' AND created_at < ?",
            (t - stuck_after,),
        ).fetchone()
        d24 = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_usd),0) FROM deposits WHERE created_at >= ?",
            (t - 86400,),
        ).fetchone()
        minted = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_usd),0) FROM deposits WHERE status='minted'"
        ).fetchone()
        return {
            "by_status": [
                {"status": r[0], "count": int(r[1]), "usd": float(r[2] or 0.0)}
                for r in by_status
            ],
            "stuck": int(stuck[0] or 0),
            "stuck_after_seconds": int(stuck_after),
            "count_24h": int(d24[0] or 0),
            "usd_24h": float(d24[1] or 0.0),
            "minted_count": int(minted[0] or 0),
            "minted_usd": float(minted[1] or 0.0),
        }

    def recent_deposits(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT burn_tx,address,amount_usd,created_at,updated_at,status,"
               "mint_tx,attempts,last_error FROM deposits")
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self._conn.execute(sql, args).fetchall()
        return [dict(zip(("burn_tx", "address", "amount_usd", "created_at", "updated_at",
                          "status", "mint_tx", "attempts", "last_error"), r,
                         strict=True)) for r in rows]

    def requeue_deposit(self, burn_tx: str) -> bool:
        """Put a deposit back in front of the sweeper. -> whether one moved.

        Resets attempts as well as status, because the attempt counter is what
        made it stop: leaving it at the cap means the next failure abandons it
        again immediately, which looks like the retry silently did nothing.

        Deliberately refuses to touch a MINTED row. That deposit is finished on
        chain, and re-queueing it would have the sweeper try to mint an already
        minted message — wasted gas at best, and a confusing failure loop in
        the log at worst.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE deposits SET status='pending', attempts=0, last_error=NULL, "
                "updated_at=? WHERE burn_tx=? AND status<>'minted'",
                (time.time(), burn_tx.strip().lower()),
            )
        return cur.rowcount > 0

    def gas_stats(self, now: float | None = None) -> dict[str, Any]:
        """What the relayer has given away on Base, as wei strings.

        Wei is summed in Python and returned as a decimal STRING: the totals
        exceed SQLite's 64-bit integer range in aggregate, and the column is
        already TEXT for that reason. Turning it into a float here for JSON
        would reintroduce exactly the precision loss the schema avoided.
        """
        t = time.time() if now is None else now
        allrows = self._conn.execute("SELECT wei, created_at FROM gas_drips").fetchall()
        total = sum(int(r[0]) for r in allrows)
        day = sum(int(r[0]) for r in allrows if r[1] >= t - 86400)
        week = sum(int(r[0]) for r in allrows if r[1] >= t - 7 * 86400)
        return {
            "drips": len(allrows),
            "drips_24h": sum(1 for r in allrows if r[1] >= t - 86400),
            "wei_total": str(total),
            "wei_24h": str(day),
            "wei_7d": str(week),
            "recipients": int(
                self._conn.execute("SELECT COUNT(DISTINCT address) FROM gas_drips")
                .fetchone()[0] or 0
            ),
        }

    def grant_stats(self, now: float | None = None) -> dict[str, Any]:
        """Session-key grants. Counts only — no key material, sealed or not."""
        t = time.time() if now is None else now
        row = self._conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN revoked_at IS NULL AND expiry > ? THEN 1 ELSE 0 END),
                      SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END)
               FROM grants""", (t,),
        ).fetchone()
        return {
            "total": int(row[0] or 0),
            "live": int(row[1] or 0),
            "revoked": int(row[2] or 0),
        }

    def live_grants(self, limit: int = 50, now: float | None = None) -> list[dict[str, Any]]:
        """Live grants for the console's revoke control.

        The SELECT names its columns rather than using *, because `grants`
        holds `sealed_key` and a widening select on this table is how an
        encrypted signing key ends up in a JSON response.
        """
        t = time.time() if now is None else now
        rows = self._conn.execute(
            "SELECT address,session_address,delegate,expiry,per_trade_cap,daily_cap,"
            "created_at,rotated_at,approval_mode,autonomous_limit FROM grants "
            "WHERE revoked_at IS NULL AND expiry > ? ORDER BY created_at DESC LIMIT ?",
            (t, int(limit)),
        ).fetchall()
        return [dict(zip(("address", "session_address", "delegate", "expiry",
                          "per_trade_cap", "daily_cap", "created_at", "rotated_at",
                          "approval_mode", "autonomous_limit"), r, strict=True))
                for r in rows]

    def passkey_stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT address) FROM passkeys"
        ).fetchone()
        users = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return {
            "credentials": int(row[0] or 0),
            "accounts": int(row[1] or 0),
            "accounts_total": int(users[0] or 0),
        }

    def oauth_client_stats(self, limit: int = 20) -> list[dict[str, Any]]:
        """Registered MCP clients, most recent first. Names are self-declared
        (RFC 7591) and authorise nothing — displayed as labels only."""
        rows = self._conn.execute(
            "SELECT client_id, COALESCE(client_name,'(unnamed)'), created_at "
            "FROM oauth_clients ORDER BY created_at DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [{"client_id": r[0], "client_name": r[1], "created_at": r[2]} for r in rows]

    def record_admin_action(self, *, actor_email: str, actor_address: str | None,
                            action: str, target: str | None = None,
                            detail: dict[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO admin_audit "
                "(created_at,actor_email,actor_address,action,target,detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), actor_email, actor_address, action, target,
                 json.dumps(detail) if detail else None),
            )

    def admin_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT created_at,actor_email,actor_address,action,target,detail_json "
            "FROM admin_audit ORDER BY created_at DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [{"created_at": r[0], "actor_email": r[1], "actor_address": r[2],
                 "action": r[3], "target": r[4],
                 "detail": json.loads(r[5]) if r[5] else None} for r in rows]
