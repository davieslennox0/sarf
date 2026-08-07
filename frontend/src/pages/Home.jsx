import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import { EXPLORER } from '../wallet.js';

const MCP_URL = 'https://sarf-mcp.managerx.xyz/mcp';

// The short list shown by default. Ordered by on-chain pool depth rather than
// alphabetically — the registry sorts A-Z, which would open the page on
// ADBEx/ASMLx and put the deepest, most recognisable markets out of sight.
const SHORTLIST = ['SPYx', 'QQQx', 'NVDAx', 'AAPLx', 'TSLAx'];

// What the assistant actually is, one facet at a time. Deliberately excludes
// "Advisor" and "Broker": both are regulated terms for activities Sarf does
// not perform, and a rotating headline is a bad place to imply otherwise.
const ROLES = [
  'Agent',
  'Manager',
  'Analyzer',
  'Strategist',
  'Copilot',
  'Tracker',
  'Desk',
];

/** Cycles a word every `every` ms, sliding the next one up into place. */
function Rotator({ words, every = 5000 }) {
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setI((n) => (n + 1) % words.length), every);
    return () => clearInterval(t);
  }, [words.length, every]);
  return (
    <span className="rotator">
      {/* keyed so React remounts the span and the enter animation re-runs */}
      <span className="rotator-word" key={i}>{words[i]}</span>
      {/* Reserves the width of the longest word so the headline never reflows
          mid-sentence as the word changes. */}
      <span className="rotator-ghost" aria-hidden="true">
        {words.reduce((a, b) => (b.length > a.length ? b : a), '')}
      </span>
    </span>
  );
}

/** One flap. Animates only when its own character changes. */
function Flap({ ch }) {
  const [flipping, setFlipping] = useState(false);
  const prev = useRef(ch);
  useEffect(() => {
    if (prev.current !== ch) {
      prev.current = ch;
      setFlipping(true);
      const t = setTimeout(() => setFlipping(false), 300);
      return () => clearTimeout(t);
    }
  }, [ch]);
  if (ch === '.') return <div className="flap dot"><span className="digit">.</span></div>;
  return (
    <div className="flap">
      <span className={`digit${flipping ? ' flipping' : ''}`}>{ch}</span>
    </div>
  );
}

/**
 * Split-flap price board fed by the server's SSE tick stream.
 *
 * Digit count is fixed per session from the first tick, so the board doesn't
 * reflow between $99 and $100 — a departure board has a fixed number of flaps.
 */
function Board({ symbol, name }) {
  const [price, setPrice] = useState(null);
  const [first, setFirst] = useState(null);
  const [stale, setStale] = useState(false);
  const [width, setWidth] = useState(3);

  useEffect(() => {
    setPrice(null); setFirst(null); setStale(false);
    const es = new EventSource(`/api/rwa/stream/${encodeURIComponent(symbol)}`);
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        setStale(Boolean(d.stale));
        if (d.price_usdt == null) return;
        setPrice(d.price_usdt);
        setFirst((f) => (f == null ? d.price_usdt : f));
        setWidth((w) => Math.max(w, String(Math.floor(d.price_usdt)).length));
      } catch { /* one bad tick shouldn't kill the stream */ }
    };
    es.onerror = () => setStale(true);
    return () => es.close();
  }, [symbol]);

  const chars = useMemo(() => {
    if (price == null) return Array(width + 3).fill('–');
    const [i, f = '00'] = price.toFixed(2).split('.');
    return [...i.padStart(width, '0'), '.', ...f];
  }, [price, width]);

  const change = first != null && price != null && first !== 0
    ? ((price - first) / first) * 100 : null;
  const dir = change == null ? '' : change > 0 ? 'up' : change < 0 ? 'down' : '';

  return (
    <div className="board">
      <div className="board-head">
        <span className="ticker-name">{symbol}{name ? ` · ${name}` : ''}</span>
        <span className={`live-dot${stale ? ' stale' : ''}`}>
          {stale ? 'DELAYED' : 'LIVE'}
        </span>
      </div>
      <div className="board-row">
        <div className="flap-row">
          {chars.map((c, i) => <Flap key={i} ch={c} />)}
        </div>
        {change != null && (
          <span className={`delta ${dir}`}>
            {change >= 0 ? '+' : '−'}{Math.abs(change).toFixed(3)}% this session
          </span>
        )}
      </div>
      <div className="board-foot">
        <span>USDT · quoted from X Layer pools</span>
        <span>{price == null ? 'awaiting first tick' : 'updates every ~2s'}</span>
      </div>
    </div>
  );
}

/** Fetch prices a few at a time so 40 aggregator calls don't stampede. */
function usePrices(symbols) {
  const [prices, setPrices] = useState({});
  useEffect(() => {
    if (!symbols.length) return;
    let cancelled = false;
    const queue = [...symbols];
    const worker = async () => {
      while (!cancelled && queue.length) {
        const s = queue.shift();
        try {
          const d = await api.price(s);
          if (!cancelled) setPrices((p) => ({ ...p, [s]: d.price_usdt }));
        } catch {
          if (!cancelled) setPrices((p) => ({ ...p, [s]: null }));
        }
      }
    };
    const workers = Array.from({ length: 4 }, worker);
    return () => { cancelled = true; void workers; };
  }, [symbols.join(',')]);
  return prices;
}

export default function Home() {
  const [assets, setAssets] = useState([]);
  const [featured, setFeatured] = useState('SPYx');
  const [copied, setCopied] = useState(false);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    api.list().then((d) => setAssets(d.assets || [])).catch(() => {});
  }, []);

  const visible = useMemo(() => {
    if (showAll) return assets;
    const rank = (s) => {
      const i = SHORTLIST.indexOf(s);
      return i === -1 ? SHORTLIST.length : i;
    };
    const top = [...assets].sort((a, b) => rank(a.symbol) - rank(b.symbol)).slice(0, 5);
    // Keep whatever is on the board in the list, so collapsing never hides the
    // row the user just selected.
    if (!top.some((a) => a.symbol === featured)) {
      const sel = assets.find((a) => a.symbol === featured);
      if (sel) return [...top.slice(0, 4), sel];
    }
    return top;
  }, [assets, showAll, featured]);

  // Only price what is on screen: each row is its own aggregator call, so
  // fetching all 40 up front is both slow and a rate-limit risk.
  const symbols = useMemo(() => visible.map((a) => a.symbol), [visible]);
  const prices = usePrices(symbols);
  const featuredAsset = assets.find((a) => a.symbol === featured);

  return (
    <>
      <section className="hero">
        <div className="eyebrow">Tokenized equities on X Layer</div>
        <h1>Sarf, your AI-RWA Portfolio <Rotator words={ROLES} /></h1>
        <p className="sub">
          Ask for a position, a price, or a read on what you hold — in Claude or
          ChatGPT. Sarf prices and builds every trade; you sign it in your own
          wallet. The server holds no keys and cannot move your funds.
        </p>
        <div className="stats">
          <div><b>{assets.length || '—'}</b><span>assets</span></div>
          <div><b>X Layer</b><span>chain 196</span></div>
        </div>
      </section>

      <Board symbol={featured} name={featuredAsset?.name?.replace(' xStock', '')} />

      <h2>Markets</h2>
      <div className="ledger">
        {visible.map((a) => {
          const p = prices[a.symbol];
          return (
            <button
              key={a.symbol}
              className={`row${featured === a.symbol ? ' on' : ''}`}
              onClick={() => setFeatured(a.symbol)}
            >
              <span className="row-left">
                <span className="sym">{a.symbol}</span>
                <span className="name">{a.name.replace(' xStock', '')}</span>
              </span>
              <span className="row-right">
                <span className="price">
                  {p === undefined ? '···' : p === null ? '—' : `$${p.toFixed(2)}`}
                </span>
                <span className="chip">{a.cex_ticker}</span>
              </span>
            </button>
          );
        })}
      </div>
      {assets.length > visible.length || showAll ? (
        <div className="cta">
          <button onClick={() => setShowAll((v) => !v)}>
            {showAll ? 'Show fewer' : `List all ${assets.length} markets`}
          </button>
        </div>
      ) : null}

      <p className="muted small" style={{ marginTop: 10 }}>
        Trade using the on-chain symbol — the x-suffix form (AAPLx). The chip shows
        OKX's centralized order-book ticker for the same underlying; that is a
        different identifier on a different venue and is not tradable here.
      </p>

      <h2>Connect to Claude or ChatGPT</h2>
      <ol className="steps">
        <li>
          Open <b>Settings → Connectors → Add custom connector</b>.
          <div className="copy-row">
            <code>{MCP_URL}</code>
            <button onClick={() => {
              navigator.clipboard?.writeText(MCP_URL);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}>{copied ? 'copied' : 'copy'}</button>
          </div>
        </li>
        <li>
          Approve with one wallet signature. It <b>authorizes no transaction</b> and
          moves no funds.
        </li>
        <li>
          Ask naturally — <b>“what can I buy?”</b>, <b>“price of NVDAx”</b>,
          <b> “buy $50 of SPYx”</b>.
        </li>
        <li>
          Sarf returns an <b>unsigned</b> transaction and a signing link. You review it
          and sign in your own wallet; that is what produces the X Layer transaction hash.
        </li>
      </ol>

      <h2>Settlement</h2>
      <p className="muted small" style={{ maxWidth: '58ch', lineHeight: 1.65 }}>
        Every trade is an ordinary on-chain swap. Orders appear under{' '}
        <a href="/dashboard/activity">activity</a> with their hash linked to the{' '}
        <a href={EXPLORER} target="_blank" rel="noreferrer">X Layer explorer</a> —
        verifiable by anyone, not a screenshot.
      </p>

      <footer>
        <p className="fine">
          Synthetic exposure. xStocks track the underlying share price only — no
          ownership, dividends, or voting rights. Redemption depends on the issuer.
          Sarf is not a broker.
        </p>
        <p className="fine">
          A flat $0.10 platform fee is charged per swap in the stablecoin leg, inside
          the same transaction you sign. Network gas is separate and paid in OKB.
        </p>
      </footer>
    </>
  );
}
