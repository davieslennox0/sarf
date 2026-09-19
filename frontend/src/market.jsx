import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api.js';
import { chatLinks } from './handoff.jsx';

/**
 * Shared pieces of the market board: token marks, sparklines, the candle
 * overview, the top-asset cards and the markets table. Home and Markets both
 * render these, so an asset looks the same wherever it is listed.
 */

export const usd = (x, max = 2) =>
  x == null ? '—' : `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: Math.min(2, max), maximumFractionDigits: max })}`;

export function compactUsd(x) {
  if (x == null) return '—';
  const n = Number(x);
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

export const pct = (x) => {
  if (x == null) return '—';
  if (Math.abs(x) < 0.005) return '0.00%'; // rounds to zero: no sign to show
  return `${x > 0 ? '+' : '−'}${Math.abs(x).toFixed(2)}%`;
};
const dirOf = (x) => (x == null || Math.abs(x) < 0.005 ? 'flat' : x > 0 ? 'up' : 'down');

/** Deterministic steel shade per ticker, behind a logo that failed to load. */
export function markBg(symbol) {
  const base = String(symbol || '?').replace(/x$/, '');
  let h = 0;
  for (const c of base) h = (h * 31 + c.charCodeAt(0)) % 360;
  const l = 21 + (h % 17);
  return `linear-gradient(140deg, hsl(214,7%,${l + 9}%), hsl(214,8%,${l}%))`;
}

export function TokenMark({ asset, small }) {
  return (
    <span className={`tokenmark${small ? ' sm' : ''}`} style={{ background: markBg(asset.symbol) }}>
      {asset.logo_url
        ? <img src={asset.logo_url} alt="" loading="lazy" referrerPolicy="no-referrer"
               onError={(e) => { e.currentTarget.style.display = 'none'; }} />
        : null}
      <i>{asset.symbol.replace(/x$/, '').slice(0, 2).toUpperCase()}</i>
    </span>
  );
}

/**
 * Gradients the sparklines fill with. Defined once per page; each spark
 * references them by id, so twenty rows do not carry twenty copies.
 */
export function SparkDefs() {
  const stop = (c) => (
    <>
      <stop offset="0%" stopColor={c} stopOpacity=".28" />
      <stop offset="100%" stopColor={c} stopOpacity="0" />
    </>
  );
  return (
    <svg width="0" height="0" style={{ position: 'absolute' }} aria-hidden="true">
      <defs>
        <linearGradient id="spark-up" x1="0" y1="0" x2="0" y2="1">{stop('#6B9E7D')}</linearGradient>
        <linearGradient id="spark-down" x1="0" y1="0" x2="0" y2="1">{stop('#C2635A')}</linearGradient>
        <linearGradient id="spark-flat" x1="0" y1="0" x2="0" y2="1">{stop('#7C838C')}</linearGradient>
      </defs>
    </svg>
  );
}

/** Area sparkline of hourly closes, coloured by direction over the window. */
export function Sparkline({ points, change, className = '' }) {
  if (!points || points.length < 2) return <svg className={`spark flat ${className}`} />;
  const W = 100, H = 40, pad = 3;
  const lo = Math.min(...points), hi = Math.max(...points);
  const span = hi - lo || hi * 0.001 || 1;
  const xy = points.map((p, i) => [
    (i / (points.length - 1)) * W,
    pad + (1 - (p - lo) / span) * (H - pad * 2),
  ]);
  const line = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`).join('');
  return (
    <svg className={`spark ${dirOf(change)} ${className}`} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
      <path className="fill" d={`${line}L${W},${H}L0,${H}Z`} />
      <path className="line" d={line} />
    </svg>
  );
}

/**
 * Candle overview (sparkline, 24h change, 24h volume) for a set of symbols.
 * The server answers what it has cached and names the rest `pending`; those
 * are asked for again a few times, then left as unknown rather than zero.
 */
export function useOverview(symbols) {
  const [data, setData] = useState({});
  const key = symbols.join(',');
  useEffect(() => {
    if (!symbols.length) return undefined;
    let cancelled = false;
    let timer = null;
    const run = async (wanted, attempt) => {
      try {
        const d = await api.overview(wanted);
        if (cancelled) return;
        setData((prev) => ({ ...prev, ...d.assets }));
        if (d.pending?.length && attempt < 8) timer = setTimeout(() => run(d.pending, attempt + 1), 2500);
      } catch { /* leave what we have; the rows show dashes */ }
    };
    // In chunks, so the first rows fill in without waiting on the last.
    for (let i = 0; i < symbols.length; i += 20) run(symbols.slice(i, i + 20), 0);
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [key]);
  return data;
}

/** Buy or Sell, handed to the user's chat with the order already written. */
function TradeButton({ side, symbol }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    document.addEventListener('click', away);
    return () => document.removeEventListener('click', away);
  }, [open]);
  const text = side === 'buy'
    ? `Using Sarf, buy $50 of ${symbol} on X Layer.`
    : `Using Sarf, sell my ${symbol} on X Layer.`;
  const l = chatLinks(text);
  return (
    <span className="trade" ref={ref}>
      <button className={`btn small${side === 'buy' ? ' primary' : ''}`} onClick={() => setOpen((v) => !v)}>
        {side === 'buy' ? 'Buy' : 'Sell'}
      </button>
      {open && (
        <span className="trade-menu" role="menu">
          <Link className="trade-here" to={side === 'buy' ? `/swap?from=USDT&to=${symbol}` : `/swap?from=${symbol}&to=USDT`}
                onClick={() => setOpen(false)}>
            {side === 'buy' ? 'Buy' : 'Sell'} {symbol} here →
          </Link>
          <span className="muted small">or in your chat, with the order written:</span>
          <a href={l.claude} target="_blank" rel="noreferrer" onClick={() => setOpen(false)}>Open in Claude ↗</a>
          <a href={l.chatgpt} target="_blank" rel="noreferrer" onClick={() => setOpen(false)}>Open in ChatGPT ↗</a>
        </span>
      )}
    </span>
  );
}

const Ic = ({ children }) => <span className="ic" aria-hidden="true">{children}</span>;

/**
 * The markets table: asset, price, 24h change, 24h volume, a 24h sparkline and
 * the two actions. Price comes from the same warm cache every page reads; the
 * other columns from the candle overview.
 */
export function MarketTable({ assets, prices, overview, emptyText = 'No assets match.' }) {
  return (
    <div className="mkt" role="table">
      <div className="mkt-head" role="row">
        <span><Ic>◎</Ic>Asset</span>
        <span className="r"><Ic>$</Ic>Price</span>
        <span className="r hide-sm"><Ic>±</Ic>24h</span>
        <span className="r hide-md"><Ic>≋</Ic>24h volume</span>
        <span className="r hide-md">Last 24h</span>
        <span className="r hide-sm">Trade</span>
      </div>
      {assets.length === 0 && <div className="mkt-empty">{emptyText}</div>}
      {assets.map((a) => {
        const p = prices[a.symbol];
        const o = overview[a.symbol];
        const ch = o?.change_24h_pct;
        return (
          <div className="mkt-row" role="row" key={a.symbol}>
            <a className="row-left" href={a.explorer_url} target="_blank" rel="noreferrer" title="Contract on the X Layer explorer">
              <TokenMark asset={a} />
              <span className="row-id">
                <span className="sym">{a.symbol}</span>
                <span className="name">{a.name.replace(' xStock', '')}</span>
              </span>
            </a>
            <span className="r stack-sm">
              <span className="price">{p === undefined ? '···' : p === null ? '—' : usd(p)}</span>
              <span className={`chg ${dirOf(ch)} show-sm`}>{pct(ch)}</span>
            </span>
            <span className={`r chg ${dirOf(ch)} hide-sm`}>{pct(ch)}</span>
            <span className="r vol hide-md">{compactUsd(o?.volume_24h_usd)}</span>
            <span className="r hide-md"><Sparkline points={o?.spark} change={ch} /></span>
            <span className="mkt-actions hide-sm">
              <TradeButton side="buy" symbol={a.symbol} />
              <TradeButton side="sell" symbol={a.symbol} />
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** Four headline cards: the most traded assets over the last day. */
export function TopCards({ assets, prices, overview }) {
  return (
    <div className="topcards">
      {assets.map((a) => {
        const o = overview[a.symbol];
        const p = prices[a.symbol] ?? o?.last;
        return (
          <Link key={a.symbol} className="topcard" to="/markets">
            <span className="topcard-head">
              <TokenMark asset={a} small />
              <span className="sym">{a.symbol}</span>
              <span className="sep" />
              <span className="nm">{a.name.replace(' xStock', '')}</span>
            </span>
            <span className="big">{p == null ? '—' : usd(p)}</span>
            <span className={`chg ${dirOf(o?.change_24h_pct)}`}>
              {pct(o?.change_24h_pct)} <span className="muted">· {compactUsd(o?.volume_24h_usd)} 24h vol</span>
            </span>
            <Sparkline points={o?.spark} change={o?.change_24h_pct} />
          </Link>
        );
      })}
    </div>
  );
}

/** Magnifier for the search pill. */
export function SearchIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
    </svg>
  );
}

/** Rank assets by 24h volume, highest first; unknown volume sorts last. */
export function byVolume(assets, overview) {
  return [...assets].sort((a, b) =>
    (overview[b.symbol]?.volume_24h_usd ?? -1) - (overview[a.symbol]?.volume_24h_usd ?? -1));
}

export function useMemoFilter(assets, q) {
  return useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return assets;
    return assets.filter((a) =>
      a.symbol.toLowerCase().includes(s) || a.name.toLowerCase().includes(s)
      || (a.cex_ticker || '').toLowerCase().includes(s));
  }, [assets, q]);
}
