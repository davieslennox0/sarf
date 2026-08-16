import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { markBg, usePrices } from './Home.jsx';

/**
 * The full tradable universe, read from the on-chain-verified registry.
 *
 * Priced in pages rather than all at once: every row is its own aggregator
 * call, so quoting 40 assets on mount is both slow and a rate-limit risk. Rows
 * beyond the visible window show their symbol and venue immediately and fill in
 * their price when expanded.
 */
const PAGE = 12;

export default function Markets() {
  const [assets, setAssets] = useState([]);
  const [err, setErr] = useState(null);
  const [limit, setLimit] = useState(PAGE);

  useEffect(() => {
    api.list()
      .then((d) => setAssets(d.assets || []))
      .catch((e) => setErr(e.message || String(e)));
  }, []);

  const visible = useMemo(() => assets.slice(0, limit), [assets, limit]);
  const symbols = useMemo(() => visible.map((a) => a.symbol), [visible]);
  const prices = usePrices(symbols);

  return (
    <section>
      <div className="eyebrow tick">
        {assets.length || 40} tokenized assets on X Layer
      </div>
      <h1>Markets</h1>
      <p className="sub">
        Trade using the on-chain symbol — the x-suffix form (AAPLx). OKX's
        centralized order book lists the same underlying with an X-prefix
        (XAAPL); that is a different identifier on a different venue and is not
        tradable here.
      </p>

      {err && <p className="error" style={{ marginTop: 18 }}>{err}</p>}

      <div className="section-label">All assets</div>
      <div className="ledger">
        {visible.map((a) => {
          const p = prices[a.symbol];
          return (
            <a
              className="row"
              key={a.symbol}
              href={a.explorer_url}
              target="_blank"
              rel="noreferrer"
            >
              <span className="row-left">
                {/* Logo over a generated monogram, same as the chat cards: the
                    mark is painted first so a blocked or 404 image leaves a
                    filled square rather than a hole in the row. */}
                <span className="tokenmark" style={{ background: markBg(a.symbol) }}>
                  {a.logo_url
                    ? <img src={a.logo_url} alt="" loading="lazy" referrerPolicy="no-referrer"
                           onError={(e) => { e.currentTarget.style.display = 'none'; }} />
                    : null}
                  <i>{a.symbol.replace(/x$/, '').slice(0, 2).toUpperCase()}</i>
                </span>
                <span className="row-id">
                  <span className="sym">{a.symbol}</span>
                  <span className="name">{a.name.replace(' xStock', '')}</span>
                </span>
              </span>
              <span className="row-right">
                <span className="price">
                  {p === undefined ? '···' : p === null ? '—' : `$${p.toFixed(2)}`}
                </span>
                <span className="chip">{a.cex_ticker}</span>
              </span>
            </a>
          );
        })}
      </div>

      {limit < assets.length && (
        <button className="see-all" onClick={() => setLimit((n) => n + PAGE)}>
          Show {Math.min(PAGE, assets.length - limit)} more →
        </button>
      )}

      {!assets.length && !err && (
        <p className="muted small" style={{ marginTop: 18 }}>Loading the registry…</p>
      )}

      <p className="fine" style={{ marginTop: 24 }}>
        Every symbol links to its contract on the X Layer explorer. The registry is
        verified on-chain — Sarf will not resolve an asset from an address handed to
        it by a model, only from this list.
      </p>
    </section>
  );
}
