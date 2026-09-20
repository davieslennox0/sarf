import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount } from '../wallet.js';
import { OpenInChat } from '../handoff.jsx';
import { Fact, PairMark, num, pct, usd } from '../zapui.jsx';

/**
 * Zap: deposit one asset into an X Layer RWA incentive pool with an IL line.
 *
 * Laid out the way a lending market is. The page used to open with four
 * explainer cards and put the pools underneath them as prose — which asks
 * someone to read a tutorial before they can see what is on offer. Aave's
 * markets page leads with the table and lets the numbers do the explaining;
 * the tutorial sits below, for whoever wants it.
 *
 * Creating a position here builds nothing on-chain. The position page
 * (/zap/:id) walks the wallet through the steps, and it is the same page the
 * chat links to.
 */

const EXIT_PRESETS = [5, 8, 12];

/** One pool, as a table row: identity, the numbers, and the way in. */
function PoolRow({ p, chosen, onPick }) {
  return (
    <div className={`mkt-row zap-cols${chosen ? ' on' : ''}`}>
      <span className="row-id mark">
        <PairMark a={p.other.symbol} b={p.rwa.symbol} small />
        <span className="row-id-text">
          <span className="sym">{p.pair}</span>
          <span className="name">{p.dex} · zap with {p.zap_with.join(' or ')}</span>
        </span>
      </span>
      <span className="r price">
        {p.price == null ? '—' : num(p.price)}
        <span className="sub">{p.other.symbol} per {p.rwa.symbol}</span>
      </span>
      <span className="r hide-sm">
        {p.sell_tax_pct}% / {p.buy_tax_pct}%
        <span className="sub">tax out / in</span>
      </span>
      <span className="r hide-md">
        {(p.exit_and_reentry_cost_bps / 100).toFixed(2)}%
        <span className="sub">exit + re-entry</span>
      </span>
      <span className="mkt-actions">
        <button className={`btn small${chosen ? ' primary' : ''}`} onClick={() => onPick(p)}>
          {chosen ? 'Selected' : 'Deposit'}
        </button>
      </span>
    </div>
  );
}

/** Positions you already hold, so the page opens on your own money first. */
function MyPositions({ rows }) {
  return (
    <div className="mkt">
      <div className="mkt-head pos-cols">
        <span>Position</span>
        <span className="r">Value</span>
        <span className="r">IL now</span>
        <span className="r hide-sm">Exit line</span>
        <span className="r">State</span>
      </div>
      {rows.map((v) => (
        <Link className="mkt-row pos-cols" key={v.position_id} to={`/zap/${v.position_id}`}>
          <span className="row-id mark">
            <PairMark a={v.pool.other.symbol} b={v.pool.rwa.symbol} small />
            <span className="row-id-text">
              <span className="sym">{v.pool.pair}</span>
              <span className="name">{v.deposit.amount} {v.deposit.asset} deposited</span>
            </span>
          </span>
          <span className="r price" data-k="Value">{usd(v.value.current_usd)}</span>
          <span className={`r price${v.il.current_bps > v.il.exit_threshold_bps ? ' bad' : ''}`} data-k="IL now">
            {pct(v.il.current_bps)}
          </span>
          <span className="r price hide-sm" data-k="Exit line">{pct(v.il.exit_threshold_bps)}</span>
          <span className="r" data-k="State">
            <span className={`chip${v.action_needed ? ' accent' : ''}`}>
              {v.action_needed ? 'needs you' : v.state.replace(/_/g, ' ')}
            </span>
          </span>
        </Link>
      ))}
    </div>
  );
}

export default function Zap() {
  const nav = useNavigate();
  const [pools, setPools] = useState(null);
  const [program, setProgram] = useState(null);
  const [mine, setMine] = useState(null);
  const [balances, setBalances] = useState({});
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);   // has the user chosen a pool yet
  const [form, setForm] = useState({ pool: '', asset: '', amount: '', exitPct: '8', reentryPct: '' });

  useEffect(() => {
    api.zapPools().then((r) => {
      setPools(r.pools);
      setProgram(r.incentive_program);
      const first = r.pools[0];
      if (first) setForm((f) => ({ ...f, pool: first.key, asset: first.zap_with[0] }));
    }).catch((e) => setErr(e.message));
    if (getSession()) {
      api.zapMine().then((r) => setMine(r.positions)).catch(() => {});
      api.portfolio().then((p) => {
        const b = {};
        for (const x of p.positions || []) b[x.symbol] = x.quantity;
        setBalances(b);
      }).catch(() => {});
    }
  }, []);

  const pool = useMemo(() => pools?.find((p) => p.key === form.pool), [pools, form.pool]);
  const exitBps = Math.round(Number(form.exitPct) * 100);
  const reBps = form.reentryPct === '' ? Math.floor(exitBps / 2) : Math.round(Number(form.reentryPct) * 100);
  const bal = balances[form.asset];
  const over = bal != null && Number(form.amount) > Number(bal);
  const cheapExit = pool && exitBps <= pool.exit_and_reentry_cost_bps;
  const valid = pool && Number(form.amount) > 0 && !over
    && exitBps >= 1 && exitBps <= 5000 && reBps >= 0 && reBps < exitBps;

  const pick = (p) => {
    setForm((f) => ({ ...f, pool: p.key, asset: p.zap_with[0] }));
    setOpen(true);
    requestAnimationFrame(() => document.getElementById('zap-deposit')?.scrollIntoView({
      behavior: 'smooth', block: 'center',
    }));
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

  const mineValue = (mine || []).reduce((s, v) => s + (v.value.current_usd || 0), 0);
  // The program window reads "2026-09-18 to 2026-09-25 (UTC+8)". A date in a
  // stat tile makes the reader do the subtraction; do it for them.
  const daysLeft = useMemo(() => {
    const end = program?.window?.split(' to ')[1]?.match(/\d{4}-\d{2}-\d{2}/)?.[0];
    if (!end) return null;
    const d = Math.ceil((new Date(`${end}T00:00:00+08:00`) - Date.now()) / 86400000);
    return d >= 0 ? d : null;
  }, [program]);

  return (
    <section className="zap">
      {/* Identity left, the numbers that describe the market right — the
          shape every lending market page uses. */}
      <div className="market-head">
        <div>
          <div className="eyebrow tick">X Layer · RWA liquidity</div>
          <h1>Zap</h1>
          <p className="sub">
            Put one asset into an incentivised pool and draw a line for impermanent loss.
            Past the line Sarf moves you to Aave; when the price comes back, so do you.
          </p>
        </div>
        <div className="market-stats">
          <div><b>{pools ? pools.length : '—'}</b><span>pools</span></div>
          {daysLeft != null && <div><b>{daysLeft}d</b><span>left in this round</span></div>}
          {mine?.length ? <div><b>{mine.length}</b><span>your positions</span></div> : null}
          {mine?.length ? <div><b>{usd(mineValue)}</b><span>your value</span></div> : null}
        </div>
      </div>

      {err && <p className="error" style={{ marginTop: 16 }}>{err}</p>}

      {mine?.length ? (
        <>
          <div className="section-label">Your positions</div>
          <MyPositions rows={mine} />
        </>
      ) : null}

      <div className="section-label">Incentivised pools</div>
      {!pools ? <div className="mkt-empty">Reading X Layer…</div> : (
        <div className="mkt">
          <div className="mkt-head zap-cols">
            <span>Pool</span>
            <span className="r">Pool price</span>
            <span className="r hide-sm">Token tax</span>
            <span className="r hide-md">Round trip</span>
            <span />
          </div>
          {pools.map((p) => (
            <PoolRow key={p.key} p={p} chosen={open && p.key === form.pool} onPick={pick} />
          ))}
        </div>
      )}
      {program && (
        <p className="fine" style={{ textAlign: 'left', margin: '10px 2px 0', maxWidth: 'none' }}>
          {program.name}, {program.window}. {program.rewards}. Rewards are claimed on OKX's
          side; Sarf does not handle them.
        </p>
      )}

      {/* The deposit panel. One pool at a time, with the consequences of the
          numbers spelled out underneath them rather than in a paragraph
          somewhere else on the page. */}
      <div className="card deposit-panel" id="zap-deposit">
        <div className="dp-head">
          <h3>Deposit</h3>
          {pool && (
            <span className="dp-pool">
              <PairMark a={pool.other.symbol} b={pool.rwa.symbol} small />
              {pool.pair}
            </span>
          )}
        </div>

        {!pool ? <p className="muted">Choose a pool above.</p> : (
          <>
            <div className="dp-row">
              <label className="dp-amount">
                <span className="dp-k">You deposit</span>
                <div className="dp-input">
                  <input inputMode="decimal" placeholder="0.0" value={form.amount}
                         onChange={(e) => setForm((f) => ({ ...f, amount: e.target.value }))} />
                  <div className="seg tiny">
                    {pool.zap_with.map((s) => (
                      <button key={s} className={form.asset === s ? 'on' : ''}
                              onClick={() => setForm((f) => ({ ...f, asset: s }))}>{s}</button>
                    ))}
                  </div>
                </div>
                <span className="dp-s">
                  {bal != null
                    ? <>Balance {bal} {form.asset}
                        <button className="linkish" onClick={() => setForm((f) => ({ ...f, amount: String(bal) }))}>MAX</button>
                      </>
                    : 'Connect to see your balance'}
                </span>
              </label>

              <label className="dp-amount">
                <span className="dp-k">Exit when IL passes</span>
                <div className="dp-input">
                  <input inputMode="decimal" value={form.exitPct}
                         onChange={(e) => setForm((f) => ({ ...f, exitPct: e.target.value }))} />
                  <span className="dp-unit">%</span>
                </div>
                <span className="dp-s">
                  {EXIT_PRESETS.map((n) => (
                    <button key={n} className={`chip pickable${Number(form.exitPct) === n ? ' on' : ''}`}
                            onClick={() => setForm((f) => ({ ...f, exitPct: String(n) }))}>{n}%</button>
                  ))}
                  <span className="muted">re-enter under {pct(reBps)}</span>
                </span>
              </label>
            </div>

            {/* What the deposit actually does, in the order it happens. */}
            <div className="dp-facts">
              <Fact label="Half is swapped to" value={pool.other.symbol}
                    sub={`through the same pool, so both sides match`} />
              <Fact label="You end up holding" value="LP tokens"
                    sub={`${pool.pair} on ${pool.dex}`} />
              <Fact label="Round trip out and back" value={`${(pool.exit_and_reentry_cost_bps / 100).toFixed(2)}%`}
                    sub={`${pool.other.symbol} charges ${pool.sell_tax_pct}% out / ${pool.buy_tax_pct}% in`}
                    tone={cheapExit ? 'warn' : undefined} />
              <Fact label="While parked" value="Aave USDT"
                    sub="supply yield until IL normalises" />
            </div>

            {cheapExit && (
              <p className="disclosure" style={{ marginTop: 14 }}>
                An exit line of {pct(exitBps)} sits at or below what one exit and re-entry
                costs ({(pool.exit_and_reentry_cost_bps / 100).toFixed(2)}%). Crossing it would
                spend more than the loss it avoids. Set the line above the round trip.
              </p>
            )}
            {over && <p className="error" style={{ marginTop: 12 }}>That is more {form.asset} than you hold.</p>}

            <button className="primary big" style={{ marginTop: 16 }} disabled={!valid || busy} onClick={deposit}>
              {busy ? 'Building…' : 'Create position and sign in wallet'}
            </button>
            <OpenInChat text={chatText} />
            <p className="muted small" style={{ marginTop: 12 }}>
              Creating the position moves nothing. The next page builds each transaction from
              live X Layer state for you to sign: wrap, swap, add liquidity.
            </p>
          </>
        )}
      </div>

      {/* The tutorial, demoted below the thing it explains. */}
      <div className="section-label">How zap works</div>
      <div className="steps grid g4">
        <div className="step">
          <span className="step-num">1</span>
          <div className="step-body">
            <h3>Deposit one asset</h3>
            <p>
              Bring SPCXx or NVDAx. Sarf wraps it into the token the pool lists, then swaps
              the right share into the pool's other token through that same pool, so both
              sides match and nothing is left over.
            </p>
          </div>
        </div>
        <div className="step">
          <span className="step-num">2</span>
          <div className="step-body">
            <h3>Earn in the pool</h3>
            <p>
              Your liquidity earns Uniswap trading fees, and X Layer pays incentive rewards
              to LPs in these pools during the program window.
            </p>
          </div>
        </div>
        <div className="step">
          <span className="step-num">3</span>
          <div className="step-body">
            <h3>Exit when IL crosses your line</h3>
            <p>
              Sarf checks impermanent loss against your entry price about once a minute. Past
              your line it lines up the way out: withdraw, convert to USDT, supply to Aave.
            </p>
          </div>
        </div>
        <div className="step">
          <span className="step-num">4</span>
          <div className="step-body">
            <h3>Re-enter when it recovers</h3>
            <p>
              Parked in Aave you earn supply yield. When IL falls back under your re-entry
              line, Sarf lines up the way back in and resets your entry price.
            </p>
          </div>
        </div>
      </div>

      <div className="grid g2" style={{ marginTop: 14 }}>
        <div className="card">
          <h3>What impermanent loss is</h3>
          <p>
            When one side of a pool moves against the other, the pool rebalances you into
            more of the side that fell. IL is how much less that leaves you with than simply
            holding the two amounts: 0 at your entry price, about 0.6% if the price moves
            25%, and 5.7% if it doubles or halves.
          </p>
        </div>
        <div className="card">
          <h3>What you sign, and what it costs</h3>
          <p>
            Sarf never holds your funds: every step, in and out, is signed in your own wallet,
            on this page or from the link Sarf gives you in chat. Each pool's paired token
            charges a transfer tax, so an exit and a re-entry cost a few percent. Each pool
            shows its round trip above; set your exit line above it.
          </p>
        </div>
      </div>

      <p className="disclosure" style={{ marginTop: 18 }}>
        xStocks track a share price and convey no ownership, dividends or voting rights.
        Liquidity provision carries impermanent loss, and each pool's paired token is a
        volatile ecosystem token with a transfer tax.
      </p>
    </section>
  );
}
