import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api.js';
import { CLIENTS, MCP_URL, openClient } from '../guide.jsx';
import {
  MarketTable, SearchIcon, SparkDefs, TopCards, byVolume, compactUsd, useMemoFilter, useOverview,
} from '../market.jsx';

export { markBg } from '../market.jsx';

// What the assistant is, one facet at a time. "Advisor" and "Broker" are left
// out on purpose: both are regulated activities Sarf does not perform.
const ROLES = ['Agent', 'Manager', 'Analyzer', 'Strategist', 'Copilot', 'Tracker', 'Desk'];

// Shown until the overview has volumes to rank by: the deepest pools.
const SHORTLIST = ['SPYx', 'QQQx', 'NVDAx', 'AAPLx', 'TSLAx', 'SPCXx', 'GLDx', 'IWMx'];
const TABLE_ROWS = 8;

/** Cycles a word every `every` ms, sliding the next one up into place. */
function Rotator({ words, every = 4500 }) {
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setI((n) => (n + 1) % words.length), every);
    return () => clearInterval(t);
  }, [words.length, every]);
  return (
    <span className="rotator">
      <span className="rotator-word" key={i}>{words[i]}</span>
      <span className="rotator-ghost" aria-hidden="true">
        {words.reduce((a, b) => (b.length > a.length ? b : a), '')}
      </span>
    </span>
  );
}

/**
 * Prices for the rows on screen, in one request, answered from the server's
 * warm cache. Symbols still being fetched come back as `pending` and are asked
 * for again a few times; an unpriceable asset stays null and shows as "—".
 */
export function usePrices(symbols) {
  const [prices, setPrices] = useState({});
  const key = symbols.join(',');
  useEffect(() => {
    if (!symbols.length) return undefined;
    let cancelled = false;
    let timer = null;
    const run = async (wanted, attempt) => {
      try {
        const d = await api.prices(wanted);
        if (cancelled) return;
        setPrices((p) => ({ ...p, ...Object.fromEntries(wanted.map((s) => [s, d.prices?.[s] ?? null])) }));
        const pending = d.pending || wanted.filter((s) => d.prices?.[s] == null);
        if (pending.length && attempt < 4) timer = setTimeout(() => run(pending, attempt + 1), 2500);
      } catch {
        if (!cancelled) setPrices((p) => ({ ...p, ...Object.fromEntries(wanted.map((s) => [s, null])) }));
      }
    };
    run(symbols, 0);
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [key]);
  return prices;
}

function ZapPools() {
  const [pools, setPools] = useState(null);
  useEffect(() => { api.zapPools().then((r) => setPools(r.pools)).catch(() => setPools([])); }, []);
  if (!pools) return <div className="mkt-empty">Reading X Layer…</div>;
  return (
    <div className="mkt">
      <div className="mkt-head zap-cols">
        <span>Pool</span>
        <span className="r">Pool price</span>
        <span className="r hide-sm">Token tax</span>
        <span className="r hide-md">Exit + re-entry cost</span>
        <span className="r">Deposit</span>
      </div>
      {pools.map((p) => (
        <div className="mkt-row zap-cols" key={p.key}>
          <span className="row-id">
            <span className="sym">{p.pair}</span>
            <span className="name">Zap with {p.zap_with.join(' or ')} · Uniswap V2</span>
          </span>
          <span className="r price">
            {p.price == null ? '—' : `${Number(p.price).toLocaleString(undefined, { maximumFractionDigits: 0 })} ${p.other.symbol}`}
          </span>
          <span className="r hide-sm muted">{p.buy_tax_pct}% / {p.sell_tax_pct}%</span>
          <span className="r hide-md">≈ {(p.exit_and_reentry_cost_bps / 100).toFixed(2)}%</span>
          <span className="mkt-actions"><Link className="btn small primary" to="/zap">Zap</Link></span>
        </div>
      ))}
    </div>
  );
}

function ConnectPanel() {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(MCP_URL);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="connect-cta">
      <h2>Bring Sarf into your chat</h2>
      <p>Add the connector once. Then ask for prices, positions and trades in Claude or ChatGPT, and sign each one in your own wallet.</p>
      <div className="copy-row">
        <code>{MCP_URL}</code>
        <button className="btn small" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>
      </div>
      <div className="cta" style={{ justifyContent: 'center', margin: 0 }}>
        {CLIENTS.map((c) => (
          <button key={c.id} className={c.id === 'claude' ? 'primary' : ''} onClick={() => openClient(c)}>
            {c.label}
          </button>
        ))}
        <Link className="btn ghost" to="/how">Setup guide</Link>
      </div>
    </div>
  );
}

export default function Home() {
  const [assets, setAssets] = useState([]);
  const [tab, setTab] = useState('markets');
  const [q, setQ] = useState('');

  useEffect(() => {
    api.list().then((d) => setAssets(d.assets || [])).catch(() => {});
  }, []);

  const allSymbols = useMemo(() => assets.map((a) => a.symbol), [assets]);
  const overview = useOverview(allSymbols);
  const haveVolumes = Object.values(overview).some((o) => o?.volume_24h_usd != null);

  const ranked = useMemo(() => {
    if (haveVolumes) return byVolume(assets, overview);
    const rank = (s) => { const i = SHORTLIST.indexOf(s); return i === -1 ? 99 : i; };
    return [...assets].sort((a, b) => rank(a.symbol) - rank(b.symbol));
  }, [assets, overview, haveVolumes]);

  const filtered = useMemoFilter(ranked, q);
  const rows = q ? filtered : filtered.slice(0, TABLE_ROWS);
  const top = ranked.slice(0, 4);
  const onScreen = useMemo(() => [...new Set([...top, ...rows].map((a) => a.symbol))], [top, rows]);
  const prices = usePrices(onScreen);

  const volume = Object.values(overview).reduce((s, o) => s + (o?.volume_24h_usd || 0), 0);

  return (
    <>
      <SparkDefs />
      <section className="hero">
        <div className="eyebrow tick">Live on X Layer · {assets.length || 43} tokenized stocks and ETFs</div>
        <h1>Sarf, your AI-RWA Portfolio <Rotator words={ROLES} /></h1>
        <p className="sub">
          Trade tokenized stocks on X Layer two ways: ask in Claude or ChatGPT, or do it
          right here. Sarf prices and builds every trade; you sign it in your own wallet.
          The server holds no keys and cannot move your funds.
        </p>
        <div className="hero-cta">
          <Link className="cta-btn" to="/how">Connect to Claude or ChatGPT</Link>
          <Link className="cta-btn ghost" to="/swap">Trade on the website</Link>
        </div>
        <div className="stats">
          <div><b>{assets.length || '—'}</b><span>assets</span></div>
          <div><b>{volume ? compactUsd(volume) : '—'}</b><span>24h volume</span></div>
          <div><b>$0.01</b><span>per swap</span></div>
          <div><b>Non-custodial</b><span>you sign every trade</span></div>
        </div>
      </section>

      <div className="section-label" style={{ justifyContent: 'space-between' }}>
        <span>Most traded today</span>
        <Link className="see-all" style={{ width: 'auto', padding: 0, margin: 0 }} to="/markets">All markets →</Link>
      </div>
      <TopCards assets={top} prices={prices} overview={overview} />

      <div className="toolbar">
        <div className="seg" role="tablist">
          <button className={tab === 'markets' ? 'on' : ''} onClick={() => setTab('markets')}>Markets</button>
          <button className={tab === 'zap' ? 'on' : ''} onClick={() => setTab('zap')}>Zap pools</button>
        </div>
        {tab === 'markets' && (
          <label className="search">
            <SearchIcon />
            <input placeholder="Search 43 assets" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
        )}
      </div>
      {tab === 'markets' ? (
        <>
          <MarketTable assets={rows} prices={prices} overview={overview} />
          {!q && (
            <Link className="see-all" to="/markets">View all {assets.length || 43} assets →</Link>
          )}
        </>
      ) : (
        <>
          <ZapPools />
          <p className="fine" style={{ textAlign: 'left', margin: '12px 0 0', maxWidth: 'none' }}>
            Deposit one asset into an X Layer RWA incentive pool. Sarf watches impermanent loss and
            moves you to Aave when it crosses your line, then back when the price recovers.
          </p>
        </>
      )}

      <div className="section-label">Two ways to use Sarf</div>
      <div className="surfaces">
        <div className="surface">
          <h3>In Claude or ChatGPT</h3>
          <p>Ask in plain words. Sarf quotes it live, builds it, and hands you a link to sign.</p>
          <ul>
            <li>"buy $50 of NVDAx"</li>
            <li>"zap 0.5 SPCXx, exit at 8% IL"</li>
            <li>"split $100 across SPYx and QQQx"</li>
            <li>"how is my portfolio doing?"</li>
          </ul>
          <Link className="btn primary" to="/how">Connect in a minute</Link>
        </div>
        <div className="surface">
          <h3>On the website</h3>
          <p>No chat needed: the same prices, checks and fee, signed in your own wallet.</p>
          <ul>
            <li>Swap <em>· any stock against USDT, USDC, OKB or another stock</em></li>
            <li>Zap <em>· liquidity with an impermanent-loss line</em></li>
            <li>Portfolio <em>· holdings, xPoints, send, stop-loss levels</em></li>
            <li>Portfolio → Fund <em>· add money by card</em></li>
            <li>Account <em>· connected agents, session key, revoke</em></li>
          </ul>
          <Link className="btn primary" to="/swap">Open Swap</Link>
        </div>
      </div>

      <div className="section-label">How it works</div>
      <div className="steps grid g3">
        <div className="step">
          <span className="step-num">1</span>
          <div className="step-body">
            <h3>Sign in with your wallet</h3>
            <p>Here, or once in Claude or ChatGPT by adding Sarf as a connector.</p>
          </div>
        </div>
        <div className="step">
          <span className="step-num">2</span>
          <div className="step-body">
            <h3>Pick a trade</h3>
            <p>On the Swap or Zap page, or in words: "buy $50 of NVDAx". Sarf quotes it live.</p>
          </div>
        </div>
        <div className="step">
          <span className="step-num">3</span>
          <div className="step-body">
            <h3>Sign in your wallet</h3>
            <p>Every trade settles on X Layer from your own wallet. Nothing moves without your signature.</p>
          </div>
        </div>
      </div>

      <div className="grid g2" style={{ marginTop: 14 }}>
        <div className="card green">
          <h3>Non-custodial by construction</h3>
          <p>
            Sarf can price and build a transaction; it cannot move your funds. Every
            trade is signed by you, and the session-key path that runs small trades
            in chat is capped in the contract itself. Transfers can never be
            delegated at all.
          </p>
        </div>
        <div className="card accent">
          <h3>Priced where it fills</h3>
          <p>
            Every quote is a live route from the same aggregator the order executes
            against, so the number you see and the number you get come from the same
            question. An asset that cannot be routed shows as unpriced, never as
            zero and never as a guess.
          </p>
        </div>
      </div>

      <ConnectPanel />
    </>
  );
}
