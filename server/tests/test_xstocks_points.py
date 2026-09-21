"""Official xStocks xPoints client: off by default, and every failure of an
undocumented third-party API degrades to a labelled status, never an
exception and never a made-up number. No test here touches the network."""

import asyncio
import dataclasses

import httpx
import pytest

from sarf.config import settings
from sarf.xlayer import xstocks_points as xp

ADDR = "0x69FAE60F34DD9454770671E6D9F9105E2ED7DBEF"

DASH = {"success": True, "data": {
    "totalPoints": "32637.2100", "todayPoints": "412.5", "nextSnapshotDate": "2026-09-22T00:05:24.000Z",
    "currentSeason": {"name": "Season 1"}, "dailySpinMultiplier": "1.5", "xboostMultiplier": "1.2",
    "referralCount": 0}}
BRK = {"success": True, "data": {
    "holdersPoints": "11851.45", "lendingPoints": "0", "lpsPoints": "20785.76", "referralPoints": "0",
    "questPoints": "0", "lpsPointsBySource": [{"marketSource": "uniswap-v3", "points": "20785.76"}],
    "lendingPointsBySource": []}}


@pytest.fixture()
def api(monkeypatch):
    """Route httpx to a handler table; record which URLs were asked for."""
    routes, seen = {}, []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        for suffix, resp in routes.items():
            if str(req.url).endswith(suffix):
                return resp(req) if callable(resp) else resp
        return httpx.Response(404, json={"error": "User not found"})

    real = httpx.AsyncClient
    monkeypatch.setattr(xp.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(xp, "settings", dataclasses.replace(settings, xstocks_points_enabled=True))
    monkeypatch.setattr(xp, "_cache", {})
    monkeypatch.setattr(xp, "_backoff_until", 0.0)
    return routes, seen


def run(addr=ADDR):
    return asyncio.run(xp.fetch(addr))


def test_off_by_default_sends_nothing(api, monkeypatch):
    routes, seen = api
    monkeypatch.setattr(xp, "settings", dataclasses.replace(settings, xstocks_points_enabled=False))
    assert run()["status"] == "disabled"
    assert seen == []


def test_registered_wallet(api):
    routes, seen = api
    routes["/dashboard"] = httpx.Response(200, json=DASH)
    routes["/points-breakdown"] = httpx.Response(200, json=BRK)
    out = run()
    assert out["status"] == "ok"
    assert out["total_points"] == 32637.21
    assert out["breakdown"]["holding"] == 11851.45
    assert out["breakdown"]["liquidity_by_source"] == {"uniswap-v3": 20785.76}
    assert all(ADDR.lower() in u for u in seen)  # lowercased, and only this wallet
    run()
    assert len(seen) == 2  # second call served from cache


def test_unregistered_wallet_gets_signup_link_not_zero(api):
    out = run()
    assert out["status"] == "not_registered"
    assert out["signup_url"] == xp.SIGNUP_URL
    assert "total_points" not in out


def test_rate_limit_backs_off(api):
    routes, seen = api
    routes["/dashboard"] = httpx.Response(429, json={"retryAfter": 30})
    assert run()["status"] == "unavailable"
    n = len(seen)
    assert run()["status"] == "unavailable"
    assert len(seen) == n  # did not hit the API again inside the window


def test_captcha_and_garbage_degrade(api):
    routes, _ = api
    routes["/dashboard"] = httpx.Response(403, json={"error": "captcha_required"})
    assert "captcha" in run()["reason"]
    xp._cache.clear()
    routes["/dashboard"] = httpx.Response(200, text="<html>")
    assert run()["status"] == "unavailable"
    xp._cache.clear()
    routes["/dashboard"] = httpx.Response(200, json={"success": False})
    assert run()["status"] == "unavailable"


def test_network_error_does_not_raise(api):
    routes, _ = api

    def boom(req):
        raise httpx.ConnectError("down")
    routes["/dashboard"] = boom
    assert run()["reason"] == "xStocks points API unreachable"


# ---- registration relay ---------------------------------------------------

import json as _json
import time as _time
from types import SimpleNamespace

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sarf import auth
from sarf.db import Database
from sarf.xlayer import account_api
from sarf.xlayer.registry import registry

USER = Account.create()
OTHER = Account.create()


def signed(acct, ts=None):
    ts = ts or int(_time.time())
    sig = acct.sign_message(encode_defunct(text=xp.registration_message(ts))).signature.hex()
    return (sig if sig.startswith("0x") else "0x" + sig), ts


def test_message_is_xstocks_exact_text():
    assert xp.registration_message(1700000000) == (
        "By signing this message, I confirm wallet ownership and register for xPoints | 1700000000")


def test_register_relays_the_users_own_signature(api):
    routes, seen = api
    routes["/xdrop-user"] = httpx.Response(201, json={"success": True, "data": {}})
    sig, ts = signed(USER)
    out = asyncio.run(xp.register(USER.address, sig, ts))
    assert out["status"] == "registered"
    assert seen[-1].endswith("/xdrop-user")


def test_register_refuses_before_calling_xstocks(api):
    routes, seen = api
    sig, ts = signed(OTHER)
    assert asyncio.run(xp.register(USER.address, sig, ts))["status"] == "rejected"
    sig, ts = signed(USER, ts=int(_time.time()) - 3600)
    assert "too old" in asyncio.run(xp.register(USER.address, sig, ts))["reason"]
    assert asyncio.run(xp.register(USER.address, "0xdead", int(_time.time())))["status"] == "rejected"
    assert seen == []


def test_referral_code_only_when_configured(api, monkeypatch):
    routes, _ = api
    bodies = []

    def handler(req):
        bodies.append(_json.loads(req.content or b"{}"))
        return httpx.Response(201, json={"success": True, "data": {}})
    routes["/xdrop-user"] = handler
    sig, ts = signed(USER)
    asyncio.run(xp.register(USER.address, sig, ts))
    assert "referredBy" not in bodies[-1]
    monkeypatch.setattr(xp, "settings", dataclasses.replace(
        settings, xstocks_points_enabled=True, xstocks_referral_code="SARF1"))
    asyncio.run(xp.register(USER.address, sig, ts))
    assert bodies[-1]["referredBy"] == "SARF1"


@pytest.fixture()
def web(api, monkeypatch):
    routes, seen = api
    monkeypatch.setattr(account_api, "settings", xp.settings)
    db = Database(":memory:")
    app = FastAPI()
    app.include_router(account_api.build_account_api(db, registry(), SimpleNamespace()))
    tok, _ = auth.mint_session(db, USER.address.lower())
    return SimpleNamespace(c=TestClient(app), db=db, routes=routes,
                           h={"authorization": f"Bearer {tok}"})


def test_register_endpoint_links_on_success(web):
    web.routes["/xdrop-user"] = httpx.Response(201, json={"success": True, "data": {}})
    m = web.c.get("/api/me/xpoints/register-message", headers=web.h).json()
    sig = USER.sign_message(encode_defunct(text=m["message"])).signature.hex()
    r = web.c.post("/api/me/xpoints/register", headers=web.h,
                   json={"signature": "0x" + sig.removeprefix("0x"), "timestamp": m["timestamp"]})
    assert r.status_code == 200 and r.json()["status"] == "registered"
    assert web.db.xpoints_link(USER.address)["via"] == "registered"
    assert web.c.delete("/api/me/xpoints/link", headers=web.h).json() == {"unlinked": True}
    assert web.db.xpoints_link(USER.address) is None


def test_register_endpoint_rejects_someone_elses_signature(web):
    sig, ts = signed(OTHER)
    r = web.c.post("/api/me/xpoints/register", headers=web.h,
                   json={"signature": sig, "timestamp": ts})
    assert r.status_code == 400
    assert web.db.xpoints_link(USER.address) is None


def test_link_existing_account_needs_it_to_exist(web):
    assert web.c.post("/api/me/xpoints/link", headers=web.h).status_code == 404
    web.routes[f"/xdrop-user/{USER.address.lower()}"] = httpx.Response(200, json={"success": True})
    assert web.c.post("/api/me/xpoints/link", headers=web.h).json() == {"status": "linked"}


def test_endpoints_off_when_flag_off(web, monkeypatch):
    monkeypatch.setattr(account_api, "settings", dataclasses.replace(settings, xstocks_points_enabled=False))
    assert web.c.get("/api/me/xpoints/register-message", headers=web.h).status_code == 404
    assert web.c.post("/api/me/xpoints/link", headers=web.h).status_code == 404
