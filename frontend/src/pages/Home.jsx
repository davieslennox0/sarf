import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import { Link } from 'react-router-dom';


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

/**
 * Deterministic shade per ticker, so an asset keeps one mark on every surface
 * — market row, holding, chat card. Exported because Markets and Portfolio
 * each carried their own byte-identical copy of it.
 *
 * It used to rotate a full-saturation HUE off the ticker hash, which meant
 * every list carried a row of coloured tiles — and for a good fraction of the
 * alphabet that colour was gold. Stripping the accent while leaving forty
 * rainbow squares behind would have missed the point, so the same hash now
 * picks a LIGHTNESS on one near-neutral steel hue: an asset still keeps its
 * own tile, and no tile is a colour.
 */
export function markBg(symbol) {
  const base = String(symbol || '?').replace(/x$/, '');
  let h = 0;
  for (const c of base) h = (h * 31 + c.charCodeAt(0)) % 360;
  const l = 21 + (h % 17);
  return `linear-gradient(140deg, hsl(214,7%,${l + 9}%), hsl(214,8%,${l}%))`;
}

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
export function Board({ symbol, name }) {
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

/**
 * Prices for the rows on screen, in one request.
 *
 * This used to fan out: one fetch per symbol, four at a time, each landing on
 * an endpoint that went to the aggregator live. Every one of those calls then
 * queued behind the same process-wide rate limiter on the server, so a list of
 * twelve took seconds to fill in and the page spent that time showing dashes —
 * for numbers the server was already keeping warm in memory the whole time.
 *
 * One call, answered from that cache. Symbols the server cannot price come
 * back absent and are rendered as "—", same as before.
 */
export function usePrices(symbols) {
  const [prices, setPrices] = useState({});
  const key = symbols.join(',');
  useEffect(() => {
    if (!symbols.length) return undefined;
    let cancelled = false;
    let timer = null;

    // The server answers with whatever is warm and keeps fetching the rest in
    // the background, so a cold entry comes back as pending rather than
    // holding the whole response. Ask again for just those, a couple of times.
    // Without this, the first cold load would leave those rows on "—" forever,
    // which is the one thing the old row-at-a-time version got right.
    const run = async (wanted, attempt) => {
      try {
        const d = await api.prices(wanted);
        if (cancelled) return;
        // Merge rather than replace: expanding the list must not blank the
        // rows that are already priced and on screen.
        setPrices((p) => ({
          ...p,
          ...Object.fromEntries(wanted.map((s) => [s, d.prices?.[s] ?? null])),
        }));
        const pending = d.pending || wanted.filter((s) => d.prices?.[s] == null);
        if (pending.length && attempt < 4) {
          timer = setTimeout(() => run(pending, attempt + 1), 2500);
        }
      } catch {
        if (!cancelled) {
          setPrices((p) => ({
            ...p, ...Object.fromEntries(wanted.map((s) => [s, null])),
          }));
        }
      }
    };

    run(symbols, 0);
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [key]);
  return prices;
}

export default function Home() {
  const [assets, setAssets] = useState([]);
  const [featured, setFeatured] = useState('SPYx');
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
        <div className="eyebrow tick">AI assistant for on-chain stock portfolios</div>
        <h1>Sarf, your AI-RWA Portfolio <Rotator words={ROLES} /></h1>
        <p className="sub">
          Ask for a position, a price, or a read on what you hold — in Claude or
          ChatGPT. Sarf prices and builds every trade; you sign it in your own
          wallet. The server holds no keys and cannot move your funds.
        </p>
        {/* The action, above the fold. It used to be the last thing on the
            page, under the board, the whole market list and three steps —
            a landing page whose only call to action is a screen and a half
            down is asking every visitor to go looking for it. */}
        <div className="hero-cta">
          <Link className="cta-btn" to="/how">Connect to Claude or ChatGPT</Link>
          <Link className="cta-btn ghost" to="/markets">Browse the markets</Link>
        </div>
        <div className="stats">
          <div><b>{assets.length || '\u2014'}</b><span>assets</span></div>
          <div><b>X Layer</b><span>chain 196</span></div>
          <div><b>$0.01</b><span>flat fee per swap</span></div>
          <div><b>Non-custodial</b><span>you hold the keys</span></div>
        </div>
      </section>

      <div className="section-label">Live price</div>
      <Board symbol={featured} name={featuredAsset?.name?.replace(' xStock', '')} />

      <div className="section-label">Markets</div>
      <div className="ledger">
        {visible.map((a) => {
          const p = prices[a.symbol];
          return (
            <button
              key={a.symbol}
              className={`row${featured === a.symbol ? ' on' : ''}`}
              onClick={() => setFeatured(a.symbol)}
            >
              {/* Same row anatomy as Markets and Portfolio: mark, then a
                  stacked id. The three pages listed assets three different
                  ways, and .row-left only laid this one out as a column by
                  accident of stylesheet order. */}
              <span className="row-left">
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
                  {p === undefined ? '\u00b7\u00b7\u00b7' : p === null ? '\u2014' : `$${p.toFixed(2)}`}
                </span>
                <span className="chip">{a.cex_ticker}</span>
              </span>
            </button>
          );
        })}
      </div>
      <Link className="see-all" to="/markets">
        View all {assets.length || 40} tokenized assets →
      </Link>

      {/*
        No walkthrough here.

        This slot held an animated phone mock playing six screens of the setup
        flow. It was a lot of chrome for a landing page whose other sections are
        a live price board and a ledger, and it read as a marketing device
        rather than as part of the product. "How it works" has its own page, it
        is the first call to action in the hero, and it is the last thing on
        this page — three routes to it is enough.
      */}
      <div className="section-label">What you are trusting</div>
      {/* The two claims a first-time visitor actually weighs, side by side.
          Both are about THIS service and both are checkable — custody in the
          contract, pricing against the venue that fills the order. The
          instrument disclosure that used to be the second card was removed at
          the owner's instruction, along with every other copy of it on the
          site; a card whose only job was to repeat it went with it rather than
          being left half-said. */}
      <div className="grid g2">
        <div className="card green">
          <h3>Non-custodial by construction</h3>
          <p>
            Sarf can price and build a transaction; it cannot move your funds. Every
            trade is signed by you, and the session-key path that runs small trades
            in chat is capped in the contract itself — transfers can never be
            delegated at all.
          </p>
        </div>
        <div className="card accent">
          <h3>Priced where it fills</h3>
          <p>
            Every quote is a live route from the same aggregator the order executes
            against, so the number you are shown and the number you get are the same
            question asked once. An asset that cannot be routed is shown as
            unpriced — never as zero, and never as a guess.
          </p>
        </div>
      </div>

      <div className="connect-cta">
        <h2 style={{ textTransform: 'none', letterSpacing: 0, fontSize: 19, color: 'var(--paper)', margin: '0 0 10px' }}>
          Connect Sarf to Claude or ChatGPT
        </h2>
        <p>Add the MCP server once, then trade and read your portfolio from any chat.</p>
        <Link className="cta-btn" to="/how">View setup instructions</Link>
      </div>
    </>
  );
}
