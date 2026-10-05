import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount } from '../wallet.js';
import { OpenInChat } from '../handoff.jsx';

/**
 * Earn's real landing page: the RWA Fixed Yield venue, with a working
 * deposit form. Replaces the earlier "coming soon" placeholder now that
 * there is a tested, mainnet-proven deposit/withdraw path behind it
 * (xlayer/earn_pendle.py + pendle_rwa.py) — showing a form with nothing
 * behind it was exactly the mistake the original Earn vault-list page made
 * and got taken down for; this one only exists because the form works.
 *
 * Deposit -> /earn-rwa/:id to sign the steps, same handoff Zap's own
 * deposit form uses (see Zap.jsx's `deposit()`).
 */
export default function EarnHome() {
  const nav = useNavigate();
  const [markets, setMarkets] = useState(null);
  const [assets, setAssets] = useState(null);
  const [mine, setMine] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ depositAsset: '', amount: '', designatedRwa: '' });

  useEffect(() => {
    api.earnRwaMarkets().then((r) => {
      setMarkets(r.markets);
      const first = r.markets[0];
      if (first) setForm((f) => ({ ...f, depositAsset: first.deposit_tokens[0] }));
    }).catch((e) => setErr(e.message));
    api.list().then((r) => setAssets(r.assets)).catch(() => {});
    if (getSession()) {
      api.earnRwaMine().then((r) => setMine(r.positions)).catch(() => {});
    }
  }, []);

  const market = markets?.[0];
  const valid = market && Number(form.amount) > 0 && form.designatedRwa;

  const deposit = async () => {
    setErr(null);
    setBusy(true);
    try {
      const addr = (await currentAccount()) || (await connect());
      await ensureSession(addr);
      const v = await api.earnRwaDeposit({
        deposit_asset: form.depositAsset, amount: form.amount,
        designated_rwa: form.designatedRwa,
      });
      nav(`/earn-rwa/${v.position_id}`);
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const chatText = market
    ? `Using Sarf, deposit ${form.amount || '<amount>'} ${form.depositAsset || market.deposit_tokens[0]} `
      + `into Earn's RWA Fixed Yield venue, with proceeds buying ${form.designatedRwa || '<asset>'}.`
    : 'Using Sarf, show me the Earn RWA Fixed Yield markets.';

  const live = (mine || []).filter((p) => p.state !== 'closed');

  return (
    <section className="zap">
      <div className="market-head">
        <div>
          <div className="eyebrow tick">X Layer · RWA Fixed Yield</div>
          <h1>Earn</h1>
          <p className="sub">
            {market ? `${market.implied_apy_pct}% fixed, maturing ${market.maturity.slice(0, 10)}` : 'Loading rate…'}
          </p>
        </div>
      </div>

      {err && <p className="error" style={{ marginTop: 14 }}>{err}</p>}

      <div className="pos-grid">
        <div className="pos-main">
          <div className="card">
            <h3>Principal-protected by construction</h3>
            <p className="small" style={{ marginTop: 0 }}>
              Deposit a stablecoin and it buys a fixed-yield Principal Token — the rate is
              locked in today, not a forecast. At withdrawal, your principal always comes back
              as the same stablecoin. Only the proceeds actually earned (never the principal,
              never negative) buy the xStock you choose below.
            </p>
          </div>

          {live.length > 0 && (
            <>
              <div className="section-label">Your positions</div>
              <ol className="zap-history">
                {live.map((p) => (
                  <li key={p.position_id} className="is-event">
                    <span className="what">
                      <Link to={`/earn-rwa/${p.position_id}`}>
                        <b>{p.principal_units ? (Number(p.principal_units) / 1e6).toFixed(2) : '—'} {p.deposit_symbol}</b>
                      </Link>
                      <span className="muted"> {p.state.replace(/_/g, ' ')} · proceeds buy {p.designated_rwa_symbol}</span>
                    </span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>

        <aside className="pos-side">
          <div className="card accent">
            <h3>Start a deposit</h3>
            <div className="zap-fields">
              <label>Deposit asset
                <select value={form.depositAsset}
                        onChange={(e) => setForm({ ...form, depositAsset: e.target.value })}>
                  {(market?.deposit_tokens || []).map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </label>
              <label>Amount
                <input inputMode="decimal" placeholder="10" value={form.amount}
                       onChange={(e) => setForm({ ...form, amount: e.target.value })} />
              </label>
              <label>Proceeds buy
                <select value={form.designatedRwa}
                        onChange={(e) => setForm({ ...form, designatedRwa: e.target.value })}>
                  <option value="">Choose an asset…</option>
                  {(assets || []).map((a) => <option key={a.symbol} value={a.symbol}>{a.symbol}</option>)}
                </select>
              </label>
            </div>
            <button className="primary big" style={{ marginTop: 10 }} disabled={!valid || busy} onClick={deposit}>
              {busy ? 'Signing…' : 'Start deposit'}
            </button>
            <OpenInChat text={chatText} />
          </div>
          <div className="card">
            <h3>This venue</h3>
            <p className="small">
              Backed by USDG — Paxos' cash and short-term US Treasury bills, attested monthly.
              No leverage, no borrowing: a borrow-and-loop version of this costs more in Aave
              interest than the fixed rate pays, before counting any incentive.
            </p>
            <p className="muted small" style={{ marginBottom: 0 }}>
              <Link to="/zap">Zap</Link> is the other live venue if you'd rather provide
              liquidity directly.
            </p>
          </div>
        </aside>
      </div>
    </section>
  );
}
