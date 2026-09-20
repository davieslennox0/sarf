import React, { Suspense, lazy, useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { shortAddr } from '../wallet.js';
import { Fact } from '../zapui.jsx';
import { useWallet } from '../walletctx.jsx';
import { TokenMark } from '../market.jsx';
import Sheet from '../Sheet.jsx';
import { LevelsPanel, SendPanel, useXPoints } from '../account.jsx';

// Mounted only when opened: the deposit flow is heavy (card on-ramp, Base
// bridge), and Activity has its own fetch.
const Deposit = lazy(() => import('./Deposit.jsx'));
const ActivityList = lazy(() => import('../sections/ActivityList.jsx'));

/**
 * Portfolio: what is held and what it is worth (Holdings), and every order
 * placed through Sarf (Activity). The tab is in the URL (?tab=activity) so
 * it can be linked; ?fund=1 opens the deposit flow.
 *
 * An address in `?a=` shows that address's holdings through the public
 * read-only endpoint instead: no session, nothing granted, no tabs.
 *
 * USDT, USDC and OKB are listed with the stocks and tagged, so the list adds
 * up to the total. Server-side they stay out of `positions`, which is the
 * equity sleeve the concentration analysis measures. A balance worth under a
 * cent is rounding left by a full sell, not a holding.
 */

const PAGE = 8;

function Holdings({ data, mine, onFund }) {
  const [showAll, setShowAll] = useState(false);
  const positions = data?.positions || [];
  const held = (p) => Boolean(p) && Number(p.quantity) > 0 && (p.value_usd == null || Number(p.value_usd) >= 0.01);
  const extra = [];
  if (held(data?.usdt)) extra.push({ ...data.usdt, tag: 'Cash' });
  if (held(data?.usdc)) extra.push({ ...data.usdc, tag: 'Cash' });
  if (held(data?.okb)) extra.push({ ...data.okb, tag: 'Gas' });
  const sorted = [...positions, ...extra].sort((a, b) => (b.value_usd || 0) - (a.value_usd || 0));
  const visible = showAll ? sorted : sorted.slice(0, PAGE);
  const unpriced = data?.unpriced_positions || [];

  if (!sorted.length) {
    return (
      <div className="card empty-state">
        <h3>{mine ? 'Nothing here yet' : 'Nothing held at this address'}</h3>
        <p>{mine
          ? 'Add dollars by card and they land in your wallet on X Layer, ready to trade.'
          : 'No tokenized stocks, no USDT, no OKB.'}</p>
        {mine && <div className="cta"><button className="primary" onClick={onFund}>Fund your wallet</button></div>}
      </div>
    );
  }
  return (
    <>
      {unpriced.length > 0 && (
        <div className="disclosure">
          <b>{unpriced.join(', ')}</b> could not be priced right now, so the total above
          excludes them. They are still held; this is a quote outage, not a zero balance.
        </div>
      )}
      <div className="ledger">
        {visible.map((p) => {
          const body = (
            <>
              <span className="row-left">
                <TokenMark asset={p} />
                <span className="row-id">
                  <span className="sym">{p.symbol}{p.tag && <span className="chip" style={{ marginLeft: 8 }}>{p.tag}</span>}</span>
                  <span className="name">{(p.name || '').replace(' xStock', '')}</span>
                </span>
              </span>
              <span className="row-right">
                <span className="price">{p.value_usd != null ? `$${Number(p.value_usd).toLocaleString()}` : '—'}</span>
                <span className="weight">{p.quantity}</span>
              </span>
            </>
          );
          return p.explorer_url
            ? <a className="row" key={p.symbol} href={p.explorer_url} target="_blank" rel="noreferrer">{body}</a>
            : <div className="row static" key={p.symbol}>{body}</div>;
        })}
      </div>
      {sorted.length > PAGE && (
        <button className="see-all" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Show fewer' : `Show all ${sorted.length} holdings →`}
        </button>
      )}
      {mine && (
        <div className="grid g2" style={{ marginTop: 22 }}>
          <SendPanel holdings={sorted.filter((h) => Number(h.quantity) > 0)} />
          <LevelsPanel symbols={positions.map((p) => p.symbol)} />
        </div>
      )}
    </>
  );
}

export default function Portfolio() {
  const [params, setParams] = useSearchParams();
  const queried = params.get('a') || '';
  const tab = params.get('tab') === 'activity' ? 'activity' : 'holdings';
  const fundOpen = params.get('fund') === '1';
  const { address, signedIn } = useWallet();
  const mine = !queried && signedIn;

  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  // Activity mounts on first open and then stays mounted, so switching back
  // and forth does not refetch it.
  const [activitySeen, setActivitySeen] = useState(tab === 'activity');
  const xp = useXPoints();
  // Roughly a handful of swaps at X Layer gas prices; below it, warn.
  const LOW_GAS_OKB = 0.002;

  const load = async () => {
    setErr(null); setBusy(true);
    try {
      setData(queried ? await api.publicPortfolio(queried) : await api.portfolio());
    } catch (e) { setErr(e.message || String(e)); } finally { setBusy(false); }
  };
  // Clearing first matters: without it, following a shared ?a=… link (or
  // leaving one) leaves the previous account's holdings on screen, labelled
  // as the new one's, until the fetch returns.
  useEffect(() => { setData(null); if (queried || signedIn) load(); }, [queried, address]);

  const lowGas = data != null && Number(data.gas_balance_okb || 0) < LOW_GAS_OKB;

  const setQuery = (patch) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) { if (v == null) next.delete(k); else next.set(k, v); }
    setParams(next, { replace: true });
  };
  const openTab = (t) => { if (t === 'activity') setActivitySeen(true); setQuery({ tab: t === 'activity' ? 'activity' : null }); };
  const setFund = (on) => setQuery({ fund: on ? '1' : null });

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Portfolio</h1>
          {data && <p className="muted small" style={{ marginTop: 6 }}>{shortAddr(data.address)} · read live from X Layer</p>}
        </div>
        {mine && <button className="primary" onClick={() => setFund(true)}>Fund</button>}
      </div>

      {err && <p className="error" style={{ marginTop: 18 }}>{err}</p>}
      {busy && !data && <p className="muted small" style={{ marginTop: 18 }}>Reading X Layer…</p>}

      {data && (
        <div className="dp-facts" style={{ marginTop: 22 }}>
          <Fact label="Total value"
                value={data.total_value_usd != null
                  ? `$${Number(data.total_value_usd).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                  : '—'}
                sub="everything this wallet holds" />
          <Fact label="Tokenized stocks"
                value={`$${Number(data.positions_value_usd || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
                sub={`${(data.positions || []).length} position${(data.positions || []).length === 1 ? '' : 's'}`} />
          <Fact label="Cash"
                value={`$${(Number(data.usdt?.quantity || 0) + Number(data.usdc?.quantity || 0))
                  .toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
                sub="USDT and USDC, ready to trade" />
          <Fact label="Gas"
                value={`${Number(data.gas_balance_okb || 0).toFixed(4)} OKB`}
                tone={lowGas ? 'warn' : undefined}
                sub={lowGas ? 'too low to sign much' : 'pays for your own signatures'} />
          {mine && xp && (
            <Fact label="xPoints" value={Number(xp.xpoints).toLocaleString()}
                  sub="from confirmed trades" />
          )}
        </div>
      )}

      {/* Gas is the one balance that stops everything else working, and it is
          not the one anybody watches. A wallet full of stocks and empty of OKB
          cannot sign a thing. */}
      {mine && data && lowGas && (
        <div className="callout warning" style={{ marginTop: 18 }}>
          <span className="callout-k">Gas</span>
          <b className="callout-v">{Number(data.gas_balance_okb || 0).toFixed(5)} OKB</b>
          <span className="callout-s">
            Every trade is signed from this wallet and pays its own gas. Top up OKB before
            the next one, or a signature will fail for want of a fraction of a cent.
            <button className="linkish" onClick={() => setFund(true)}>Fund</button>
          </span>
        </div>
      )}

      {mine && (
        <div className="toolbar" style={{ marginTop: 28 }}>
          <div className="seg" role="tablist">
            <button role="tab" aria-selected={tab === 'holdings'} className={tab === 'holdings' ? 'on' : ''} onClick={() => openTab('holdings')}>Holdings</button>
            <button role="tab" aria-selected={tab === 'activity'} className={tab === 'activity' ? 'on' : ''} onClick={() => openTab('activity')}>Activity</button>
          </div>
          <div className="cta" style={{ margin: 0 }}>
            <Link className="btn small" to="/swap">Swap</Link>
            <Link className="btn small" to="/zap">Zap</Link>
          </div>
        </div>
      )}

      <div hidden={mine && tab !== 'holdings'}>
        {data && <Holdings data={data} mine={mine} onFund={() => setFund(true)} />}
      </div>
      {mine && activitySeen && (
        <div hidden={tab !== 'activity'}>
          <Suspense fallback={<p className="muted small">Loading…</p>}><ActivityList /></Suspense>
        </div>
      )}

      {!data && !busy && !err && !mine && !queried && (
        <p className="muted small" style={{ marginTop: 24 }}>Sign in to read your holdings.</p>
      )}

      <Sheet open={mine && fundOpen} title="Fund your wallet" onClose={() => { setFund(false); load(); }}>
        <Suspense fallback={<p className="muted small">Loading…</p>}><Deposit embedded /></Suspense>
      </Sheet>
    </section>
  );
}
