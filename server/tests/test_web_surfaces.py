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
