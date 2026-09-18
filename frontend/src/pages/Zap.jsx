import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount } from '../wallet.js';
import { OpenInChat } from '../handoff.jsx';

/**
 * Zap: deposit one asset into an X Layer RWA incentive pool with an IL line.
 * Creating the position here builds nothing on-chain. The position page
 * (/zap/:id) walks the wallet through the steps, and it is the same page the
 * chat links to.
 */

const pct = (bps) => (Number(bps) / 100).toFixed(2);

export default function Zap() {
  const nav = useNavigate();
  const [pools, setPools] = useState(null);
  const [program, setProgram] = useState(null);
  const [mine, setMine] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ pool: '', asset: '', amount: '', exitPct: '8', reentryPct: '' });

  useEffect(() => {
    api.zapPools().then((r) => {
      setPools(r.pools);
      setProgram(r.incentive_program);
      const first = r.pools[0];
      if (first) setForm((f) => ({ ...f, pool: first.key, asset: first.zap_with[0] }));
    }).catch((e) => setErr(e.message));
    if (getSession()) api.zapMine().then((r) => setMine(r.positions)).catch(() => {});
  }, []);

  const pool = useMemo(() => pools?.find((p) => p.key === form.pool), [pools, form.pool]);
  const exitBps = Math.round(Number(form.exitPct) * 100);
  const reBps = form.reentryPct === '' ? Math.floor(exitBps / 2) : Math.round(Number(form.reentryPct) * 100);
  const cheapExit = pool && exitBps <= pool.exit_and_reentry_cost_bps;
  const valid = pool && Number(form.amount) > 0 && exitBps >= 1 && exitBps <= 5000 && reBps >= 0 && reBps < exitBps;

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const pickPool = (e) => {
    const p = pools.find((x) => x.key === e.target.value);
    setForm((f) => ({ ...f, pool: p.key, asset: p.zap_with[0] }));
  };

  const deposit = async () => {
    setErr(null);
    setBusy(true);
    try {
      const addr = (await currentAccount()) || (await connect());
      await ensureSession(addr);
      const v = await api.zapDeposit({
        pool: form.pool, asset: form.asset, amount: form.amount,
        il_threshold_bps: exitBps, reentry_threshold_bps: reBps,
      });
      nav(`/zap/${v.position_id}`);
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const chatText = pool
    ? `Using Sarf, zap-deposit ${form.amount || '<amount>'} ${form.asset} into the ${pool.key} pool `
      + `with an impermanent-loss exit threshold of ${exitBps} bps and re-entry at ${reBps} bps.`
    : 'Using Sarf, show me the zap pools.';

  return (
    <section className="zap">
      <div className="eyebrow">X Layer · RWA liquidity</div>
      <h1>Zap</h1>
      <p className="sub">
        Deposit one asset. Sarf splits it into an X Layer RWA incentive pool, watches
        impermanent loss against your entry price, moves you to Aave when it crosses
        your line, and brings you back when the price does. Every move is signed in
        your own wallet.
      </p>

      {err && <p className="error" style={{ marginTop: 18 }}>{err}</p>}

      <div className="section-label" style={{ marginTop: 36 }}>Incentivised pools</div>
      {!pools && !err && <p className="muted small">Reading X Layer…</p>}
      {pools && (
        <div className="grid g2 zap-pools">
          {pools.map((p) => (
            <button type="button" key={p.key}
                    className={`card zap-pool${p.key === form.pool ? ' accent' : ''}`}
                    onClick={() => pickPool({ target: { value: p.key } })}>
              <h3>{p.pair}</h3>
              <p>
                Zap with {p.zap_with.join(' or ')} · Uniswap V2<br />
                {p.price != null && <>1 {p.rwa.symbol} = {Number(p.price).toLocaleString(undefined, { maximumFractionDigits: 0 })} {p.other.symbol}<br /></>}
                {p.other.symbol} tax {p.buy_tax_pct}% out / {p.sell_tax_pct}% in ·
                exit + re-entry ≈ {pct(p.exit_and_reentry_cost_bps)}%
              </p>
            </button>
          ))}
        </div>
      )}
      {program && (
        <p className="muted small" style={{ marginTop: 10 }}>
          {program.name}, {program.window}. Rewards are paid by X Layer to LPs in these
          pools and claimed on OKX's side. Sarf doesn't handle them.
        </p>
      )}

      {pool && (
        <div className="card zap-form" style={{ marginTop: 28 }}>
          <h3>Deposit</h3>
          <div className="zap-fields">
            <label>Pool
              <select value={form.pool} onChange={pickPool}>
                {pools.map((p) => <option key={p.key} value={p.key}>{p.pair}</option>)}
              </select>
            </label>
            <label>Asset
              <select value={form.asset} onChange={set('asset')}>
                {pool.zap_with.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label>Amount
              <input inputMode="decimal" placeholder="0.5" value={form.amount} onChange={set('amount')} />
            </label>
            <label>Exit when IL exceeds (%)
              <input inputMode="decimal" value={form.exitPct} onChange={set('exitPct')} />
            </label>
            <label>Re-enter when IL is back under (%)
              <input inputMode="decimal" placeholder={pct(Math.floor(exitBps / 2))}
                     value={form.reentryPct} onChange={set('reentryPct')} />
            </label>
          </div>
          {cheapExit && (
            <p className="disclosure" style={{ marginTop: 14 }}>
              An exit and re-entry in this pool costs about {pct(pool.exit_and_reentry_cost_bps)}%
              ({pool.other.symbol}'s transfer tax plus the pool fee), which is at or above your
              exit line. The exit only pays off if the price keeps moving away afterwards.
            </p>
          )}
          <button className="primary big" style={{ marginTop: 16 }} disabled={!valid || busy} onClick={deposit}>
            {busy ? 'Creating…' : 'Create position and sign in wallet'}
          </button>
          <OpenInChat text={chatText} />
          <p className="muted small" style={{ marginTop: 10 }}>
            Creating the position moves nothing. The next page builds each transaction from
            live X Layer state for you to sign: wrap, swap, add liquidity.
          </p>
        </div>
      )}

      {mine && mine.length > 0 && (
        <>
          <div className="section-label" style={{ marginTop: 36 }}>Your positions</div>
          <div className="rowlist">
            {mine.map((p) => (
              <Link key={p.position_id} className="rowlink" to={`/zap/${p.position_id}`}>
                <span className="rowlink-main">
                  <b>{p.pool.pair}</b> · <span className="chip">{p.state.replace('_', ' ')}</span>
                  <span className="muted small" style={{ display: 'block', marginTop: 4 }}>{p.headline}</span>
                </span>
                <span className="rowlink-go">→</span>
              </Link>
            ))}
          </div>
        </>
      )}

      <p className="disclosure" style={{ marginTop: 36 }}>
        xStocks track a share price and convey no ownership, dividends or voting rights.
        Liquidity provision carries impermanent loss, and each pool's paired token is a
        volatile ecosystem token with a transfer tax.
      </p>
    </section>
  );
}
