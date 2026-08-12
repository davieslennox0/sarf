import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { currentAccount, short } from '../wallet.js';

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

/** Deterministic hue per ticker so an asset keeps one colour everywhere. */
function markBg(symbol) {
  const base = String(symbol || '?').replace(/x$/, '');
  let h = 0;
  for (const c of base) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `linear-gradient(140deg, hsl(${h},62%,42%), hsl(${(h + 38) % 360},58%,30%))`;
}

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
  const sorted = [...positions].sort(
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
            <div><b>${Number(data.positions_value_usd || 0).toLocaleString()}</b><span>positions</span></div>
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
                {visible.map((p) => (
                  <a
                    className="row"
                    key={p.symbol}
                    href={p.explorer_url}
                    target="_blank"
                    rel="noreferrer"
                  >
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
                        <span className="sym">{p.symbol}</span>
                        <span className="name">{(p.name || '').replace(' xStock', '')}</span>
                      </span>
                    </span>
                    <span className="row-right">
                      <span className="price">
                        {p.value_usd != null ? `$${Number(p.value_usd).toLocaleString()}` : '—'}
                      </span>
                      <span className="weight">{p.quantity}</span>
                    </span>
                  </a>
                ))}
              </div>
              {sorted.length > PAGE && (
                <button className="see-all" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? 'Show fewer' : `Show all ${sorted.length} positions →`}
                </button>
              )}
            </>
          )}

          {sorted.length === 0 && (
            <p className="muted small" style={{ marginTop: 20 }}>
              No tokenized stock positions at this address.
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
