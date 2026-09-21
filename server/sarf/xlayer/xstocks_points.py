"""Read-only client for the official xStocks xPoints program.

xPoints are issued by xStocks (Backed/Kraken), not by Sarf and not by X Layer.
They are computed off-chain by xStocks from daily snapshots of on-chain
activity: holding xStocks, lending them, and providing liquidity. There is no
points token or contract to read, so the only source is xStocks' own API: the
one their site at defi.xstocks.fi/points calls. It is undocumented, which is
why every field is parsed defensively and anything unexpected degrades to
"unavailable" rather than a wrong number.

What was verified on 2026-09-21 (by hand, against public holder addresses):
- GET /xdrop-user/{addr}                  -> 404 if the wallet never signed up
- GET /xdrop-user/{addr}/dashboard        -> totalPoints, todayPoints, snapshot
                                             numbers, nextSnapshotDate, season
- GET /xdrop-user/{addr}/points-breakdown -> holders/lending/LP points, with
                                             LP and lending split by source
- Wallets that hold xStocks on X Layer earn holdersPoints, and Uniswap V3 LPs
  show lpsPointsBySource "uniswap-v3". So X Layer activity does count.
- No API key. The site retries on 403 {"error":"captcha_required"} with a
  Turnstile token; a server cannot solve that, so it is reported, not worked
  around. 429 carries retryAfter (seconds).

Points only accrue to a wallet that has registered on xStocks. Registration
is one EIP-191 signature over xStocks' fixed text plus a unix timestamp,
POSTed to /xdrop-user (verified 2026-09-21 on the Sarf test wallet: 201).
Only the wallet's own key can produce it: xStocks recovers the signer, and
Sarf's session key is a different key with swap-only authority. So Sarf
never registers anyone; the user signs, and register() only relays it. The
relay is needed because the API's CORS allows defi.xstocks.fi only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from ..config import settings

log = logging.getLogger("sarf.xstocks_points")

SIGNUP_URL = "https://defi.xstocks.fi/points"

# xStocks snapshots once a day, so a 15-minute cache (the same staleTime their
# own site uses) loses nothing and keeps us far from their rate limit.
_TTL = 900
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_backoff_until = 0.0


# xStocks' own registration text (XDROP_SIGN_MESSAGES.REGISTRATION on their
# site). Changing a character makes every signature invalid on their side.
REGISTRATION_TEXT = "By signing this message, I confirm wallet ownership and register for xPoints"
_SIG_MAX_AGE = 600


def registration_message(ts: int) -> str:
    return f"{REGISTRATION_TEXT} | {ts}"


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "source": SIGNUP_URL}


async def fetch(address: str) -> dict[str, Any]:
    """This wallet's official xPoints. Never raises: the points are an add-on
    to the answer, so an xStocks outage must not break get_xpoints."""
    global _backoff_until
    if not settings.xstocks_points_enabled:
        return {"status": "disabled",
                "detail": "XSTOCKS_POINTS_ENABLED is off; the official balance is on " + SIGNUP_URL}
    addr = address.lower()
    hit = _cache.get(addr)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    if time.time() < _backoff_until:
        return _unavailable("xStocks points API rate limit; try again shortly")

    base = f"{settings.xstocks_points_api}/xdrop-user/{addr}"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            dash, brk = await asyncio.gather(c.get(f"{base}/dashboard"),
                                             c.get(f"{base}/points-breakdown"))
    except httpx.HTTPError as e:
        log.warning("xstocks points fetch failed: %s", e)
        return _unavailable("xStocks points API unreachable")

    for r in (dash, brk):
        if r.status_code == 429:
            try:
                wait = int(r.json().get("retryAfter", 60))
            except Exception:
                wait = 60
            _backoff_until = time.time() + wait
            return _unavailable("xStocks points API rate limit; try again shortly")
        if r.status_code == 403:
            return _unavailable("xStocks points API asked for a captcha")

    if dash.status_code == 404:
        out = {
            "status": "not_registered",
            "detail": ("This wallet is not registered for xPoints, so it is not earning "
                       "them. Holding or providing liquidity for xStocks earns points only "
                       "after you sign up with this wallet on the xStocks site."),
            "signup_url": SIGNUP_URL,
        }
        _cache[addr] = (time.time(), out)
        return out
    if dash.status_code != 200:
        return _unavailable(f"xStocks points API returned HTTP {dash.status_code}")

    try:
        d = dash.json()
        b = brk.json() if brk.status_code == 200 else {}
    except ValueError:
        return _unavailable("xStocks points API returned something that is not JSON")
    if not d.get("success") or not isinstance(d.get("data"), dict):
        return _unavailable("xStocks points API returned an unexpected shape")
    d = d["data"]
    b = b.get("data") if isinstance(b, dict) and b.get("success") else None

    season = d.get("currentSeason") if isinstance(d.get("currentSeason"), dict) else {}
    out: dict[str, Any] = {
        "status": "ok",
        "total_points": _num(d.get("totalPoints")),
        "points_last_snapshot": _num(d.get("todayPoints")),
        "season": season.get("name"),
        "next_snapshot": d.get("nextSnapshotDate"),
        "multipliers": {
            "daily_spin": _num(d.get("dailySpinMultiplier")),
            "xboost": _num(d.get("xboostMultiplier")),
        },
        "referrals": d.get("referralCount"),
        "source": SIGNUP_URL,
    }
    if b:
        # Category split as xStocks reports it. It does not add up to
        # total_points (their own totals differ between endpoints: multipliers
        # and snapshot timing), so it is labelled rather than reconciled.
        out["breakdown_note"] = ("base points by category as xStocks reports them; "
                                 "not expected to sum to total_points")
        out["breakdown"] = {
            "holding": _num(b.get("holdersPoints")),
            "lending": _num(b.get("lendingPoints")),
            "liquidity": _num(b.get("lpsPoints")),
            "referral": _num(b.get("referralPoints")),
            "quests": _num(b.get("questPoints")),
            "liquidity_by_source": {s.get("marketSource"): _num(s.get("points"))
                                    for s in b.get("lpsPointsBySource") or []
                                    if isinstance(s, dict)},
            "lending_by_source": {s.get("marketSource"): _num(s.get("points"))
                                  for s in b.get("lendingPointsBySource") or []
                                  if isinstance(s, dict)},
        }
    _cache[addr] = (time.time(), out)
    return out


async def is_registered(address: str) -> bool | None:
    """True/False from xStocks, None if it could not be asked."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{settings.xstocks_points_api}/xdrop-user/{address.lower()}")
    except httpx.HTTPError:
        return None
    return True if r.status_code == 200 else False if r.status_code == 404 else None


async def register(address: str, signature: str, ts: int) -> dict[str, Any]:
    """Relay the user's own signed registration to xStocks.

    The signature is checked here first, so a bad or stale one never reaches
    xStocks under Sarf's IP, and nobody can register a wallet they do not
    control through this endpoint."""
    if abs(time.time() - ts) > _SIG_MAX_AGE:
        return {"status": "rejected", "reason": "signature too old; sign again"}
    try:
        signer = Account.recover_message(encode_defunct(text=registration_message(ts)),
                                         signature=signature)
    except Exception:
        return {"status": "rejected", "reason": "signature does not verify"}
    if signer.lower() != address.lower():
        return {"status": "rejected", "reason": "signature is not from this wallet"}

    body: dict[str, Any] = {"walletAddress": signer, "walletType": "Evm",
                            "signature": signature, "signMethod": "message",
                            "signTimestamp": ts}
    if settings.xstocks_referral_code:
        body["referredBy"] = settings.xstocks_referral_code
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{settings.xstocks_points_api}/xdrop-user", json=body)
    except httpx.HTTPError as e:
        log.warning("xstocks registration relay failed: %s", e)
        return {"status": "unavailable", "reason": "xStocks points API unreachable"}
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code in (200, 201) and payload.get("success"):
        _cache.pop(address.lower(), None)
        return {"status": "registered", "referred_by": body.get("referredBy")}
    if r.status_code == 403:
        return {"status": "unavailable", "reason": "xStocks asked for a captcha; "
                "register on " + SIGNUP_URL + " instead"}
    # Already-registered wallets come back as an error; treat that as success
    # for linking, since the account exists either way.
    if await is_registered(address):
        _cache.pop(address.lower(), None)
        return {"status": "already_registered"}
    return {"status": "rejected",
            "reason": str(payload.get("error") or f"xStocks returned HTTP {r.status_code}")}
