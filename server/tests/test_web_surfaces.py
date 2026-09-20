"""The website's swap, xPoints and levels endpoints: they must call the MCP
tools' own functions under the caller's session (never a second copy of the
trading logic), refuse without a session, and turn tool errors into 400s."""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.types import TextContent

from sarf import auth
from sarf.db import Database
from sarf.validation import ValidationError
from sarf.xlayer.account_api import build_account_api
from sarf.xlayer.registry import registry
from sarf.xlayer.swap_api import build_swap_api

ADDR = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def _swap(self, **kw):
        self.calls.append(("swap", auth.current_address() if hasattr(auth, "current_address") else None, kw))
        if kw["amount"] == "999":
            raise ValueError("insufficient USDT: you hold 0 but this swap spends 999")
        return [TextContent(type="text", text=json.dumps({
            "order_id": "sarf_ord_x", "sign_url": "https://getsarf.xyz/sign?o=sarf_ord_x",
            "unsigned_transaction": {"to": "0x"}, "card": "..."}))]

    async def _get_xpoints(self):
        return {"xpoints": 12}

    async def _set_risk_params(self, **kw):
        if kw["stop_loss"] and kw["take_profit"] and kw["stop_loss"] >= kw["take_profit"]:
            raise ValidationError("stop_loss must be below take_profit")
        self.calls.append(("risk", kw))
        return {"symbol": kw["symbol"], "stop_loss": kw["stop_loss"]}

    async def display_prices(self, assets, budget=0):
        return {a.symbol: 100.0 for a in assets}


@pytest.fixture()
def env():
    db, p = Database(":memory:"), FakeProvider()
    app = FastAPI()
    app.include_router(build_swap_api(db, SimpleNamespace(), registry(), p))
    app.include_router(build_account_api(db, registry(), p))
    tok, _ = auth.mint_session(db, ADDR)
    return SimpleNamespace(c=TestClient(app), p=p, db=db, h={"authorization": f"Bearer {tok}"})


def test_swap_build_needs_a_session_and_uses_the_tool(env):
    body = {"from_symbol": "USDT", "to_symbol": "SPCXx", "amount": "10", "slippage_percent": 1}
    assert env.c.post("/api/swap/build", json=body).status_code == 401
    r = env.c.post("/api/swap/build", json=body, headers=env.h)
    assert r.status_code == 200
    out = r.json()
    assert out["order_id"] == "sarf_ord_x" and "unsigned_transaction" not in out and "card" not in out
    assert env.p.calls[0][2] == {"from_symbol": "USDT", "to_symbol": "SPCXx", "amount": "10",
                                 "slippage_percent": 1.0}


def test_swap_build_errors_are_400_with_the_tools_message(env):
    r = env.c.post("/api/swap/build", json={"from_symbol": "USDT", "to_symbol": "SPCXx",
                                            "amount": "999"}, headers=env.h)
    assert r.status_code == 400 and "insufficient USDT" in r.json()["detail"]


def test_swap_tokens_lists_base_assets_and_every_xstock(env):
    toks = env.c.get("/api/swap/tokens").json()["tokens"]
    syms = [t["symbol"] for t in toks]
    assert syms[:3] == ["USDT", "USDC", "OKB"] and "SPCXx" in syms
    assert len(syms) == 3 + len(registry().assets)


def test_levels_and_xpoints(env):
    assert env.c.get("/api/me/xpoints").status_code == 401
    assert env.c.get("/api/me/xpoints", headers=env.h).json() == {"xpoints": 12}
    r = env.c.post("/api/me/levels", json={"symbol": "NVDAx", "stop_loss": "200", "take_profit": ""},
                   headers=env.h)
    assert r.status_code == 200 and env.p.calls[-1][1] == {"symbol": "NVDAx", "stop_loss": 200.0, "take_profit": None}
    bad = env.c.post("/api/me/levels", json={"symbol": "NVDAx", "stop_loss": 300, "take_profit": 200},
                     headers=env.h)
    assert bad.status_code == 400
    env.db.put_risk_params(address=ADDR, symbol="NVDAx", stop_loss=200.0, take_profit=None)
    lv = env.c.get("/api/me/levels", headers=env.h).json()
    assert lv["levels"][0]["symbol"] == "NVDAx" and lv["levels"][0]["price"] == 100.0
    assert "auto_execute" in lv


def test_revoke_one_agent_and_all_agents_but_this_browser():
    from sarf.xlayer.api import build_xlayer_api
    db = Database(":memory:")
    app = FastAPI()
    app.include_router(build_xlayer_api(db, SimpleNamespace(transport="none"), registry(), None))
    c = TestClient(app)
    web, _ = auth.mint_session(db, ADDR, client_name="Sarf website")
    claude, _ = auth.mint_session(db, ADDR, client_name="Claude", client_id="cl_claude")
    gpt, _ = auth.mint_session(db, ADDR, client_name="ChatGPT", client_id="cl_gpt")
    db._conn.execute(
        "INSERT INTO oauth_refresh (token_id,family_id,address,client_id,created_at,expires_at) "
        "VALUES ('r1','f1',?,'cl_claude',0,9e12)", (ADDR,))
    H = {"authorization": f"Bearer {web}"}
    rows = c.get("/api/connections", headers=H).json()["connections"]
    assert {r["id"] for r in rows} >= {"cl_claude", "cl_gpt"}
    assert c.post("/api/connections/revoke", json={"id": "cl_claude"}, headers=H).json() == {"revoked": 1}
    assert auth.resolve_session(db, claude) is None
    assert db._conn.execute("SELECT revoked_at FROM oauth_refresh WHERE token_id='r1'").fetchone()[0]
    assert auth.resolve_session(db, gpt) == ADDR
    assert c.post("/api/connections/revoke", json={"id": "cl_nope"}, headers=H).status_code == 404
    assert c.post("/api/connections/revoke", json={"all": True}, headers=H).json() == {"revoked": 1}
    assert auth.resolve_session(db, gpt) is None
    assert auth.resolve_session(db, web) == ADDR  # the browser doing it stays signed in


def test_passkeys_count_only_on_the_domain_they_were_made_for():
    """After the move to getsarf.xyz a passkey made on sarf.managerx.xyz cannot
    be offered by the browser, so it must not count as registered, or the site
    never asks for a new one and every transaction fails."""
    db = Database(":memory:")
    db._conn.execute(
        "INSERT INTO passkeys (credential_id,address,public_key,sign_count,created_at,rp_id) "
        "VALUES ('old',?,x'00',0,1786000000,'sarf.managerx.xyz')", (ADDR,))
    db.passkey_rp_id = "getsarf.xyz"
    assert db.passkeys_for_address(ADDR) == []
    assert db.legacy_passkey_domains(ADDR) == ["sarf.managerx.xyz"]
    db.put_passkey(credential_id="new", address=ADDR, public_key=b"\x01", sign_count=0)
    assert [c["credential_id"] for c in db.passkeys_for_address(ADDR)] == ["new"]
    db.passkey_rp_id = None  # unset: no filtering, as before
    assert len(db.passkeys_for_address(ADDR)) == 2


def test_swap_gas_limit_uses_the_chain_not_the_aggregators_estimate(monkeypatch):
    """The aggregator under-quotes gas (550,258 quoted against 804,299 needed
    on a real native swap), and a wallet signing the quoted figure runs out of
    gas: the user pays for a reverted trade that looks like nothing happened."""
    import asyncio

    from sarf.xlayer import okx_dex, rpc as rpcmod

    c = okx_dex.OkxDexClient()

    async def estimate(**kw):
        return 804299
    monkeypatch.setattr(rpcmod, "estimate_gas", estimate)
    got = asyncio.run(c._gas_limit(user_address="0xab", to="0xcd", data="0x", value=0, quoted=550258))
    assert got == int(804299 * 1.25)

    # Never below what the aggregator asked for.
    async def small(**kw):
        return 21000
    monkeypatch.setattr(rpcmod, "estimate_gas", small)
    assert asyncio.run(c._gas_limit(user_address="0xab", to="0xcd", data="0x", value=0, quoted=550258)) == 550258

    # Un-estimatable (an unapproved ERC-20, say): widen, do not refuse.
    async def boom(**kw):
        raise rpcmod.RpcError("execution reverted: insufficient allowance")
    monkeypatch.setattr(rpcmod, "estimate_gas", boom)
    assert asyncio.run(c._gas_limit(user_address="0xab", to="0xcd", data="0x", value=0, quoted=100000)) == 180000


def test_a_broadcast_hash_is_recorded_even_after_the_order_expired(monkeypatch):
    """The wallet can broadcast and only then cross the order's TTL (approval
    leg, passkey prompt, slow confirmation). Refusing the hash there loses a
    swap that is already on-chain and invites the user to send it twice."""
    import time as _t

    from sarf.xlayer.api import build_xlayer_api

    db = Database(":memory:")
    app = FastAPI()
    app.include_router(build_xlayer_api(db, SimpleNamespace(transport="none"), registry(), None))
    c = TestClient(app)
    tok, _ = auth.mint_session(db, ADDR)
    oid = db.create_order(address=ADDR, side="swap", symbol="SPCXx", amount_in=1, quoted_out=1,
                          est_usd=1.0, tx={"to": "0x"}, ttl_seconds=-5)  # already expired
    assert db.get_order(oid)["expired"]
    h = "0x" + "ab" * 32
    r = c.post(f"/api/order/{oid}/submitted", json={"tx_hash": h},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert db.get_order(oid)["tx_hash"] == h and db.get_order(oid)["status"] == "submitted"


@pytest.mark.parametrize("path,target", [
    ("/dashboard", "/account"),
    ("/dashboard/security", "/account#agents"),
    ("/dashboard/deposit", "/portfolio?fund=1"),
    ("/dashboard/deposit?amount=50", "/portfolio?fund=1&amount=50"),
    ("/dashboard/authorize?client_id=abc", "/approve?client_id=abc"),
])
def test_old_dashboard_links_still_land_somewhere(path, target):
    """The site used to live under /dashboard. Those links are in old chat
    transcripts and bookmarks; none of them may 404 or lose their query."""
    from fastapi.testclient import TestClient as _TC

    import sarf.main as main

    if not main._FRONTEND_DIST.is_dir():
        pytest.skip("frontend not built in this checkout")
    r = _TC(main.app).get(path, follow_redirects=False)
    assert r.status_code in (307, 308), r.status_code
    assert r.headers["location"] == target


@pytest.mark.parametrize("path", ["/", "/zap", "/markets", "/zap/zap_abc"])
def test_pages_answer_head_as_well_as_get(path):
    """Link unfurls, uptime monitors and the browser's own COOP check send
    HEAD. A page that exists for GET and 404s for HEAD is a page that looks
    broken to everything except a browser tab."""
    from fastapi.testclient import TestClient as _TC

    import sarf.main as main

    if not main._FRONTEND_DIST.is_dir():
        pytest.skip("frontend not built in this checkout")
    c = _TC(main.app)
    assert c.get(path).status_code == 200
    assert c.head(path).status_code == 200
