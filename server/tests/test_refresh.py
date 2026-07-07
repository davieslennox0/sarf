"""Sign-time proposal refresh tests.

Pyth attestations are embedded in the PTB when a proposal is BUILT, and the
protocol's assert_price_not_stale rejects them within ~a minute — less than
a human review-and-sign takes (observed: four consecutive mainnet withdraws
aborted at 56–89 s build-to-execution). The fix under test: the signer page
rebuilds the proposal's bytes from its stored, already-validated params
immediately before the wallet prompt.

Invariants pinned here: same proposal_id/params/expiry, fresh bytes; every
proposal-time check re-runs (owner session, live cap ownership, dry-run,
USD cap); byte-match on submit binds to the refreshed bytes so a stale
signature can never broadcast; expired/consumed proposals are never
resurrected.
"""

from __future__ import annotations

import base64
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sarf import auth
from sarf.api import build_api
from sarf.db import Database
from sarf.providers.current_finance import CurrentFinanceProvider
from sarf.validation import AssetInfo

from test_auth import ADDR_A, ADDR_B, FakeTx

CAP_ID = "0x" + "c" * 64
OLD_PTB = base64.b64encode(b"stale bytes with old pyth vaas").decode()
SIG = base64.b64encode(b"x" * 97).decode()


class BuildTx(FakeTx):
    """Sidecar stand-in that can also build: each build returns new bytes,
    as the real sidecar does (fresh VAAs + object versions)."""

    def __init__(self):
        super().__init__()
        self.builds = 0
        self.cap_owner = ADDR_A
        self.sim_status = "success"
        self.last_build_kwargs: dict | None = None

    async def cap(self, cap_id):
        return {
            "owner": self.cap_owner,
            "marketType": "0xabc::market::Main",
            "obligationId": "0x" + "1" * 64,
        }

    def _build_result(self):
        self.builds += 1
        return {
            "txBytesBase64": base64.b64encode(f"fresh ptb v{self.builds}".encode()).decode(),
            "simulation": {
                "status": self.sim_status,
                "error": None if self.sim_status == "success" else "MoveAbort: boom",
                "gasUsedSui": 0.01,
            },
            "risk": None,
            "estUsd": 42.0,
            "capOwner": self.cap_owner,
        }

    async def build(self, **kwargs):
        self.last_build_kwargs = kwargs
        return self._build_result()

    async def build_leverage(self, **kwargs):
        self.last_build_kwargs = kwargs
        return {**self._build_result(), "currentPriceUsd": 3.5}

    async def broadcast(self, tx_bytes_base64, signatures):
        return {"status": "success", "digest": "DIG", "error": None,
                "balanceChanges": [], "createdObjects": []}


class FakeRegistry:
    _assets = {
        ("MainMarket", "USDC"): AssetInfo("USDC", "0x2::usdc::USDC", 6, "MainMarket"),
        ("MainMarket", "HASUI"): AssetInfo("HASUI", "0x3::hasui::HASUI", 9, "MainMarket"),
    }

    async def ensure_loaded(self):
        pass

    @property
    def assets(self):
        return self._assets

    @property
    def markets(self):
        return {"MainMarket"}

    def market_name_for_type(self, market_type):
        return "MainMarket"


@pytest.fixture()
def db():
    return Database(":memory:")


@pytest.fixture()
def buildtx():
    return BuildTx()


@pytest.fixture()
def client(db, buildtx):
    import sarf.api as api_module

    api_module._challenges.clear()
    provider = CurrentFinanceProvider(db, buildtx, FakeRegistry())
    app = FastAPI()
    app.include_router(build_api(db, buildtx, provider))
    return TestClient(app)


def _proposal(db, tool="propose_withdraw", ttl=600, address=ADDR_A, **extra_params):
    params = {"market": "MainMarket", "asset": "USDC", "amount": "1.5", "cap": CAP_ID}
    if tool == "propose_enter_market":
        params = {"market": "MainMarket", "asset": "USDC", "amount": "1.5"}
    if tool == "propose_leverage_position":
        params = {"asset": "HASUI", "principal": "10", "multiplier": 2.0}
    params.update(extra_params)
    return db.create_proposal(
        user_address=address, tool=tool, params=params, ptb_base64=OLD_PTB,
        simulation={"status": "success", "gasUsedSui": 0.02}, risk=None,
        ttl_seconds=ttl, summary_text="s", risk_notes=["old note"],
    )


def _bearer(db, address=ADDR_A):
    token, _ = auth.mint_session(db, address)
    return {"Authorization": f"Bearer {token}"}


def _refresh(client, pid, headers):
    return client.post(f"/api/proposal/{pid}/refresh", headers=headers)


# ------------------------------------------------------------- the happy path

def test_refresh_replaces_bytes_keeps_identity(client, db):
    prop = _proposal(db)
    res = _refresh(client, prop.proposal_id, _bearer(db))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["proposal_id"] == prop.proposal_id
    assert body["ptb_base64"] != OLD_PTB           # fresh VAAs = fresh bytes
    assert body["status"] == "proposed"
    assert body["expires_at"] == prop.expires_at   # refresh never extends life
    assert body["params"] == prop.params
    stored = db.get_proposal(prop.proposal_id)
    assert stored.ptb_base64 == body["ptb_base64"]  # submit binds to these now


def test_refresh_rebuilds_with_original_params(client, db, buildtx):
    prop = _proposal(db)
    _refresh(client, prop.proposal_id, _bearer(db))
    kw = buildtx.last_build_kwargs
    assert kw["action"] == "withdraw"
    assert kw["sender"] == ADDR_A
    assert kw["coinType"] == "0x2::usdc::USDC"
    assert kw["amountMinUnits"] == "1500000"  # 1.5 USDC re-validated, 6 dp
    assert kw["obligationCapId"] == CAP_ID


def test_refreshed_bytes_submit_and_stale_bytes_do_not(client, db):
    # The whole point: after refresh, only the fresh bytes broadcast.
    stale = _proposal(db)
    headers = _bearer(db)
    fresh_b64 = _refresh(client, stale.proposal_id, headers).json()["ptb_base64"]
    ok = client.post("/api/submit", headers=headers, json={
        "proposal_id": stale.proposal_id,
        "signed_tx_bytes_base64": fresh_b64,
        "signatures": [SIG],
    })
    assert ok.status_code == 200 and ok.json()["status"] == "success"

    # And a signature over the pre-refresh bytes is refused by byte-match.
    p2 = _proposal(db)
    _refresh(client, p2.proposal_id, headers)
    res = client.post("/api/submit", headers=headers, json={
        "proposal_id": p2.proposal_id,
        "signed_tx_bytes_base64": OLD_PTB,
        "signatures": [SIG],
    })
    assert res.status_code == 400 and "do not match" in res.json()["detail"]


def test_refresh_enter_market_and_leverage_paths(client, db, buildtx):
    for tool in ("propose_enter_market", "propose_leverage_position"):
        prop = _proposal(db, tool=tool)
        res = _refresh(client, prop.proposal_id, _bearer(db))
        assert res.status_code == 200, (tool, res.text)
        assert res.json()["ptb_base64"] != OLD_PTB
    # leverage keeps its stored quote-specific risk notes verbatim
    assert res.json()["risk_notes"] == ["old note"]


# ------------------------------------------------------------------ refusals

def test_refresh_requires_owner_session(client, db):
    prop = _proposal(db, address=ADDR_A)
    res = _refresh(client, prop.proposal_id, _bearer(db, ADDR_B))
    assert res.status_code == 400
    assert "does not belong" in res.json()["detail"]
    assert db.get_proposal(prop.proposal_id).ptb_base64 == OLD_PTB  # untouched


def test_refresh_requires_a_session_at_all(client, db):
    prop = _proposal(db)
    assert client.post(f"/api/proposal/{prop.proposal_id}/refresh").status_code == 401


def test_refresh_never_resurrects_expired(client, db):
    prop = _proposal(db, ttl=-1)
    res = _refresh(client, prop.proposal_id, _bearer(db))
    assert res.status_code == 400 and "expired" in res.json()["detail"]
    assert db.get_proposal(prop.proposal_id).status == "expired"


def test_refresh_never_resurrects_consumed(client, db):
    prop = _proposal(db)
    db.mark_proposal(prop.proposal_id, "submitted")
    res = _refresh(client, prop.proposal_id, _bearer(db))
    assert res.status_code == 400 and "not refreshable" in res.json()["detail"]


def test_refresh_rechecks_live_cap_ownership(client, db, buildtx):
    prop = _proposal(db)
    buildtx.cap_owner = ADDR_B  # cap changed hands since the proposal
    res = _refresh(client, prop.proposal_id, _bearer(db))
    assert res.status_code == 400
    assert db.get_proposal(prop.proposal_id).ptb_base64 == OLD_PTB


def test_refresh_failing_dry_run_fails_closed(client, db, buildtx):
    # If the market moved so far the rebuild no longer simulates, neither
    # the old nor the new bytes may be signed.
    prop = _proposal(db)
    buildtx.sim_status = "failure"
    headers = _bearer(db)
    res = _refresh(client, prop.proposal_id, headers)
    assert res.status_code == 400 and "dry-run failed" in res.json()["detail"]
    assert db.get_proposal(prop.proposal_id).status == "simulation_failed"
    sub = client.post("/api/submit", headers=headers, json={
        "proposal_id": prop.proposal_id,
        "signed_tx_bytes_base64": OLD_PTB,
        "signatures": [SIG],
    })
    assert sub.status_code == 400 and "not executable" in sub.json()["detail"]
