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
"""


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
    ) -> Proposal:
        now = time.time()
        pid = f"sfp_{uuid.uuid4().hex}"
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO proposals
                   (proposal_id, created_at, expires_at, user_address, tool,
                    params_json, ptb_base64, simulation_json, risk_json, status)
                   VALUES (?,?,?,?,?,?,?,?,?,'proposed')""",
                (
                    pid, now, now + ttl_seconds, user_address, tool,
                    json.dumps(params), ptb_base64,
                    json.dumps(simulation) if simulation is not None else None,
                    json.dumps(risk) if risk is not None else None,
                ),
            )
        return Proposal(pid, now, now + ttl_seconds, user_address, tool, params, ptb_base64, "proposed")

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

    def audit_trail(self, user_address: str, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """SELECT proposal_id, created_at, tool, status, tx_digest
               FROM proposals WHERE user_address=? ORDER BY created_at DESC LIMIT ?""",
            (user_address, limit),
        )
        return [
            {"proposal_id": r[0], "created_at": r[1], "tool": r[2], "status": r[3], "tx_digest": r[4]}
            for r in cur.fetchall()
        ]
