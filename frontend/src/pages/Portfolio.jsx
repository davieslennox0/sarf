import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { currentAccount, short } from '../wallet.js';
import { markBg } from './Home.jsx';

/**
 * Portfolio: what is held, and what it is worth.
 *
 * Two sources feed the same view. An address in `?a=` goes through the public
 * read-only endpoint — no session, no signature, nothing granted. Your own
 * address, once signed in, goes through the session-bound one. Same renderer
 * either way, because they are the same numbers read from the same chain.
 *
 * Deliberately just the assets. The page used to lead with a headline, a
 * pitch, an address form and a block of analysis findings before it got to a
 * single holding — so the one thing someone opens a portfolio to see was the
 * last thing on it. The analysis still exists and is still served by the API;
 * it is simply not what this page is for.
 */

const PAGE = 8;

export default function Portfolio() {
  const [params] = useSearchParams();
  const queried = params.get('a') || '';

  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showAll, setShowAll] = useState(false);

  const loadMine = async () => {
    setErr(null); setBusy(true);
    try {
      const addr = await currentAccount();
      if (!addr) throw new Error('Sign in to see your holdings.');
      await ensureSession(addr);
      setData(await api.portfolio());
    } catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(false); }
  };

  const loadPublic = async (addr) => {
    setErr(null); setBusy(true); setData(null);
    try {
      setData(await api.publicPortfolio(addr));
    } catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    if (queried) loadPublic(queried);
    else if (getSession()) loadMine();
    else { setData(null); setErr(null); }
  }, [queried]);

  // Read from positions, not from the analysis weights. A public read carries
  // no analysis, so a weights-only ledger showed an empty portfolio for any
  // address that was not your own — the holdings were there the whole time.
  const positions = data?.positions || [];
  const unpriced = data?.unpriced_positions || [];

  /**
   * USDT and OKB are holdings, and this page used to leave them out of the
   * only list it had — a wallet holding $900 of USDT and one share token read
   * as a one-asset portfolio, with the stablecoin demoted to a figure in the
   * stat strip. They are listed here with everything else and tagged for what
   * they are, so the ledger adds up to the total above it.
   *
   * They stay out of `positions` server-side on purpose: that list is the
   * equity sleeve the concentration analysis is computed against, and cash is
   * not a single-name exposure. Being shown together is a display decision;
   * being measured together would be a methodology error.
   */
  const cashAndGas = [];
  if (data?.usdt && Number(data.usdt.quantity) > 0) {
    cashAndGas.push({ ...data.usdt, tag: 'CASH' });
  }
  if (data?.okb && Number(data.okb.quantity) > 0) {
    cashAndGas.push({ ...data.okb, tag: 'GAS' });
  }

  const sorted = [...positions, ...cashAndGas].sort(
    (a, b) => (b.value_usd || 0) - (a.value_usd || 0));
  const visible = showAll ? sorted : sorted.slice(0, PAGE);

  return (
    <section>
      <h1>Portfolio</h1>

      {err && <p className="error" style={{ marginTop: 18 }}>{err}</p>}
      {busy && !data && <p className="muted small" style={{ marginTop: 18 }}>Reading X Layer…</p>}

      {data && (
        <>
          <p className="muted small" style={{ marginTop: 4 }}>
            {short(data.address)} · read live from X Layer
          </p>

          <div className="stats">
            <div>
              <b>{data.total_value_usd != null ? `$${Number(data.total_value_usd).toLocaleString()}` : '—'}</b>
              <span>total value</span>
            </div>
            {/* "positions" read as the whole ledger; it is only the equity
                sleeve, which is now one part of a list that also holds cash. */}
            <div><b>${Number(data.positions_value_usd || 0).toLocaleString()}</b><span>tokenized stocks</span></div>
            <div><b>{data.usdt_balance}</b><span>USDT</span></div>
            <div><b>{Number(data.gas_balance_okb || 0).toFixed(5)}</b><span>OKB gas</span></div>
          </div>

          {unpriced.length > 0 && (
            // Never let a pricing outage read as "these are worth nothing".
            <div className="disclosure">
              <b>{unpriced.join(', ')}</b> could not be priced right now, so the total
              above excludes them. They are still held — this is a quote outage, not a
              zero balance.
            </div>
          )}

          {sorted.length > 0 && (
            <>
              <div className="section-label">Assets</div>
              <div className="ledger">
                {visible.map((p) => {
                  const body = (
                    <>
                      <span className="row-left">
                        {/* Logo over a generated monogram, same as the chat cards:
                            the mark is painted first so a blocked or 404 image
                            leaves a filled square rather than a hole in the row. */}
                        <span className="tokenmark" style={{ background: markBg(p.symbol) }}>
                          {p.logo_url
                            ? <img src={p.logo_url} alt="" loading="lazy" referrerPolicy="no-referrer"
                                   onError={(e) => { e.currentTarget.style.display = 'none'; }} />
                            : null}
                          <i>{String(p.symbol).replace(/x$/, '').slice(0, 2).toUpperCase()}</i>
                        </span>
                        <span className="row-id">
                          <span className="sym">
                            {p.symbol}
                            {/* Cash and gas are labelled rather than left to be
                                mistaken for equities sitting in the same list. */}
                            {p.tag && <span className="chip" style={{ marginLeft: 8 }}>{p.tag}</span>}
                          </span>
                          <span className="name">{(p.name || '').replace(' xStock', '')}</span>
                        </span>
                      </span>
                      <span className="row-right">
                        <span className="price">
                          {p.value_usd != null ? `$${Number(p.value_usd).toLocaleString()}` : '—'}
                        </span>
                        <span className="weight">{p.quantity}</span>
                      </span>
                    </>
                  );
                  // OKB is the native coin, so it has no token page to link to.
                  // A row that looks clickable and goes nowhere is worse than a
                  // row that plainly does not.
                  return p.explorer_url ? (
                    <a className="row" key={p.symbol} href={p.explorer_url}
                       target="_blank" rel="noreferrer">{body}</a>
                  ) : (
                    <div className="row static" key={p.symbol}>{body}</div>
                  );
                })}
              </div>
              {sorted.length > PAGE && (
                <button className="see-all" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? 'Show fewer' : `Show all ${sorted.length} holdings →`}
                </button>
              )}
            </>
          )}

          {sorted.length === 0 && (
            <p className="muted small" style={{ marginTop: 20 }}>
              Nothing held at this address — no tokenized stocks, no USDT, no OKB.
            </p>
          )}

          {/* Stays regardless of what else is stripped: these are synthetic
              instruments, and someone reading a dollar total is exactly who
              needs to know that. */}
          <p className="fine" style={{ marginTop: 24 }}>
            <strong style={{ color: 'var(--paper)' }}>Informational only.</strong>{' '}
            xStocks track a share price and convey no ownership, dividends or
            voting rights.
          </p>
        </>
      )}

      {!data && !busy && !err && (
        <p className="muted small" style={{ marginTop: 24 }}>
          Sign in to read your holdings.
        </p>
      )}
    </section>
  );
}
