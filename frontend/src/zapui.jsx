import React from 'react';
import { markBg } from './market.jsx';

/**
 * The pieces the two zap surfaces share: the pool's identity mark, and the
 * ring that answers the only question a liquidity provider actually has —
 * "how close am I to the line I drew?"
 *
 * Both pages were a wall of same-weight key/value rows before this. Lending
 * and LP interfaces that read well (Aave's reserve page, Uniswap's position
 * page) do the opposite: one number is made large and graphic, the rest are
 * ranked underneath it.
 */

export const pct = (bps) => (bps == null ? '—' : `${(Number(bps) / 100).toFixed(2)}%`);
export const usd = (x) => (x == null ? '—' : `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
export const num = (x, d = 0) => (x == null ? '—' : Number(x).toLocaleString(undefined, { maximumFractionDigits: d }));

/**
 * How far along the exit line we are, and what to call that.
 *
 * One function so the ring, the status pill and anything added later cannot
 * drift apart on where "approaching" starts. The bands are proportions of
 * the user's OWN threshold, not absolute IL: 8% is alarming against a 10%
 * line and unremarkable against a 40% one.
 */
export const ilFraction = (currentBps, exitBps) => (
  exitBps > 0 ? Math.min(1, Math.max(0, (currentBps || 0) / exitBps)) : 0);

export function ilTone(currentBps, exitBps) {
  const f = ilFraction(currentBps, exitBps);
  return f >= 0.9 ? 'near' : f >= 0.6 ? 'approaching' : 'safe';
}

export const IL_TONE_LABEL = { safe: 'Safe', approaching: 'Approaching exit', near: 'Near exit line' };

/** Two token marks, overlapped, the way every DEX draws a pair. */
export function PairMark({ a, b, small }) {
  const one = (sym, i) => (
    <span key={i} className={`tokenmark${small ? ' sm' : ''}`} style={{ background: markBg(sym) }}>
      <i>{String(sym).replace(/^w/, '').replace(/x$/, '').slice(0, 2).toUpperCase()}</i>
    </span>
  );
  return <span className={`pairmark${small ? ' sm' : ''}`}>{one(a, 0)}{one(b, 1)}</span>;
}

/**
 * How much of the exit line the current IL has used up, as a ring.
 *
 * Deliberately NOT "IL as a share of 100%": nobody sets an 8% line and then
 * wants to read 3.2% as "3.2% of the way round". The ring is scaled to the
 * user's own threshold, so full means "we are exiting", and the colour turns
 * with it well before that.
 */
export function IlRing({ currentBps, exitBps, size = 132 }) {
  const frac = ilFraction(currentBps, exitBps);
  const r = (size - 14) / 2;
  const circ = 2 * Math.PI * r;
  const tone = ilTone(currentBps, exitBps);
  return (
    <div className={`il-ring ${tone}`} style={{ width: size, height: size }}>
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} aria-hidden="true">
        <circle className="track" cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth="9" />
        <circle
          className="arc" cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth="9"
          strokeLinecap="round" strokeDasharray={`${circ * frac} ${circ}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <div className="il-ring-mid">
        <b>{pct(currentBps)}</b>
        <span>of {pct(exitBps)}</span>
      </div>
    </div>
  );
}

/** A labelled fact, the way Aave lays out "Max LTV / 75.00%" boxes. */
export function Fact({ label, value, sub, tone }) {
  return (
    <div className="fact">
      <span className="fact-k">{label}</span>
      <b className={`fact-v${tone ? ` ${tone}` : ''}`}>{value}</b>
      {sub && <span className="fact-s">{sub}</span>}
    </div>
  );
}
