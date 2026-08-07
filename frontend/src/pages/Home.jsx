import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import { EXPLORER } from '../wallet.js';

const MCP_URL = 'https://sarf-mcp.managerx.xyz/mcp';

/** Live price chart driven by the server's SSE tick stream. */
function LiveChart({ symbol }) {
  const [points, setPoints] = useState([]);
  const [stale, setStale] = useState(false);
  const [err, setErr] = useState(null);
  const ref = useRef(null);

  useEffect(() => {
    setPoints([]);
    setErr(null);
    const es = new EventSource(`/api/rwa/stream/${encodeURIComponent(symbol)}`);
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        setStale(Boolean(d.stale));
        if (d.price_usdt != null) {
          setPoints((p) => [...p.slice(-179), { t: d.at, v: d.price_usdt }]);
        }
      } catch { /* ignore a malformed tick rather than kill the stream */ }
    };
    es.onerror = () => setErr('Price feed interrupted — retrying…');
    return () => es.close();
  }, [symbol]);

  const { path, min, max, last, change } = useMemo(() => {
    if (points.length < 2) return { path: '', min: 0, max: 0, last: points[0]?.v, change: 0 };
    const vs = points.map((p) => p.v);
    const lo = Math.min(...vs);
    const hi = Math.max(...vs);
    const span = hi - lo || 1;
    const d = points
      .map((p, i) => {
        const x = (i / (points.length - 1)) * 100;
        const y = 100 - ((p.v - lo) / span) * 100;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`;
      })
      .join(' ');
    return {
      path: d, min: lo, max: hi,
      last: vs[vs.length - 1],
      change: ((vs[vs.length - 1] - vs[0]) / vs[0]) * 100,
    };
  }, [points]);

  const up = change >= 0;
  return (
    <div className="chart-card">
      <div className="chart-head">
        <div>
          <span className="chart-sym">{symbol}</span>
          <span className={`chart-price ${up ? 'ok' : 'error'}`}>
            {last != null ? `$${last.toFixed(4)}` : '—'}
          </span>
          {points.length > 1 && (
            <span className={`chip ${up ? 'green' : 'red'}`}>
              {up ? '+' : ''}{change.toFixed(3)}% this session
            </span>
          )}
        </div>
        <span className={`chip ${stale ? 'amber' : 'green'}`}>
          {stale ? 'feed degraded' : 'live'}
        </span>
      </div>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="spark" ref={ref}>
        {path ? (
          <>
            <path d={`${path} L100,100 L0,100 Z`} className={up ? 'fill-up' : 'fill-down'} />
            <path d={path} className={up ? 'line-up' : 'line-down'} vectorEffect="non-scaling-stroke" />
          </>
        ) : null}
      </svg>
      <div className="chart-foot">
        <span>{points.length < 2 ? 'collecting ticks…' : `${points.length} ticks`}</span>
        {points.length > 1 && <span>range ${min.toFixed(4)} – ${max.toFixed(4)}</span>}
      </div>
      {err && <p className="muted small">{err}</p>}
    </div>
  );
}

export default function Home() {
  const [assets, setAssets] = useState([]);
  const [featured, setFeatured] = useState('SPYx');
  const [stats, setStats] = useState(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.list().then((d) => setAssets(d.assets || [])).catch(() => {});
    api.stats().then(setStats).catch(() => {});
  }, []);

  return (
    <section className="home">
      <header className="hero">
        <h1>Sarf — Your X Layer RWA Assistant</h1>
        <p className="lede">
          Trade tokenized stocks and ETFs on <b>X Layer</b> by talking to Claude or
          ChatGPT. Sarf prices and builds every trade; <b>you</b> sign it in your own
          wallet. Non-custodial by construction — the server holds no keys and cannot
          move your funds.
        </p>
        <div className="hero-stats">
          <div><b>{assets.length || '—'}</b><span>tokenized assets</span></div>
          <div><b>X Layer</b><span>chain 196</span></div>
          <div><b>${'0.10'}</b><span>flat fee per swap</span></div>
          <div><b>{stats?.total_users ?? '—'}</b><span>connected wallets</span></div>
        </div>
      </header>

      <div className="disclosure">
        <b>Synthetic exposure — read this.</b> xStocks track the price of the
        underlying share. Holding one gives you <b>no share ownership, no dividends
        and no voting rights</b>, and redemption depends on the issuer (Backed
        Assets). These are not equities and Sarf is not a broker.
      </div>

      <LiveChart symbol={featured} />

      <h2>Markets</h2>
      <p className="muted small">
        Trade using the on-chain symbol — the <b>x-suffix</b> form (AAPLx). OKX's
        centralized order book lists the same underlying with an <b>X-prefix</b>
        (XAAPL); that is a different identifier on a different venue and is not
        tradable here.
      </p>
      <div className="market-grid">
        {assets.map((a) => (
          <button
            key={a.symbol}
            className={`market ${featured === a.symbol ? 'active' : ''}`}
            onClick={() => setFeatured(a.symbol)}
          >
            <b>{a.symbol}</b>
            <span>{a.name.replace(' xStock', '')}</span>
            <a href={a.explorer_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
              contract ↗
            </a>
          </button>
        ))}
      </div>

      <h2>Connect it to Claude or ChatGPT</h2>
      <ol className="steps">
        <li>
          Open <b>Settings → Connectors → Add custom connector</b>.
          <div className="copy-row">
            <code>{MCP_URL}</code>
            <button
              onClick={() => {
                navigator.clipboard?.writeText(MCP_URL);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
            >
              {copied ? 'copied' : 'copy'}
            </button>
          </div>
        </li>
        <li>
          Claude discovers Sarf's OAuth server and opens the authorize page here. You
          approve with a single wallet signature — it authorizes no transaction and
          moves no funds.
        </li>
        <li>
          Ask naturally: <i>“what tokenized stocks can I buy?”</i>,
          <i> “price of NVDAx”</i>, <i> “buy $50 of SPYx”</i>.
        </li>
        <li>
          Sarf returns an <b>unsigned</b> transaction plus a signing link. You review
          the summary and risk notes, then sign in your own wallet — that is what
          produces the X Layer transaction hash.
        </li>
      </ol>

      <h2>Proof it settles on X Layer</h2>
      <p className="muted">
        Every trade is an ordinary on-chain swap. Orders you place appear under{' '}
        <a href="/dashboard/activity">My activity</a> with their transaction hash
        linked to the{' '}
        <a href={EXPLORER} target="_blank" rel="noreferrer">X Layer explorer</a> —
        real settlement, verifiable by anyone, not a screenshot.
      </p>

      <footer className="foot">
        <span>
          Built for the BuildX AI Season hackathon (AI-RWA track) on{' '}
          <a href="https://x.com/XLayerOfficial" target="_blank" rel="noreferrer">
            @XLayerOfficial
          </a>
          .
        </span>
        <span className="muted small">
          A flat $0.10 platform fee is charged per swap in the stablecoin leg, inside
          the same transaction you sign. Network gas is separate and paid in OKB.
        </span>
      </footer>
    </section>
  );
}
