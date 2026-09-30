import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { earnApi } from '../earn.js';
import { TokenMark } from '../market.jsx';

/**
 * The Earn landing page: a list of VAULTS, one per asset a user can deposit.
 * No pool names, no thresholds, no venue list — that machinery lives behind
 * /api/earn/vaults and stays there. This page only ever renders what that
 * endpoint returns, so it cannot drift from what the deposit flow actually
 * does.
 */

const CATEGORY_LABEL = { featured: 'Featured', rwa_equity: 'RWA equities', commodity: 'Commodities', all: 'All' };
const TABS = ['featured', 'rwa_equity', 'commodity', 'all'];

function AssetIcon({ symbol, small }) {
  return <TokenMark asset={{ symbol }} small={small} />;
}

function ApyValue({ vault }) {
  if (vault.apy_status === 'measured' && vault.apy_pct != null) {
    return <b className="earn-apy">{vault.apy_pct.toFixed(1)}%</b>;
  }
  if (vault.apy_status === 'measuring') {
    return <b className="earn-apy muted">Measuring</b>;
  }
  return <b className="earn-apy muted">New</b>;
}

/** Three curated cards up top. PAXGy is wider and carries the Flagship badge,
 *  per the design brief — it is the newest and most differentiated vault, not
 *  necessarily the highest-earning one, and the badge says which. */
function FeaturedStrip({ vaults }) {
  const featured = TABS_ORDER(vaults);
  return (
    <div className="earn-featured">
      {featured.map((v) => (
        <Link
          key={v.symbol}
          to={`/earn/${v.symbol}`}
          className={`card earn-feature-card${v.flagship ? ' flagship' : ''}`}
        >
          {v.flagship && <span className="chip accent earn-flagship-badge">Flagship</span>}
          <div className="earn-feature-head">
            <AssetIcon symbol={v.symbol} />
            <div>
              <div className="earn-feature-name">{v.name}</div>
              <div className="muted small">{v.symbol} earn</div>
            </div>
          </div>
          <div className="earn-feature-apy">
            <ApyValue vault={v} />
            <span className="muted small">{v.apy_label}</span>
          </div>
          <p className="earn-feature-desc">{v.description}</p>
        </Link>
      ))}
    </div>
  );
}

// SPCXx, NVDAx, then the flagship last so it visually anchors the strip.
function TABS_ORDER(vaults) {
  const bySymbol = Object.fromEntries(vaults.map((v) => [v.symbol, v]));
  return ['SPCXx', 'NVDAx', 'PAXGy'].map((s) => bySymbol[s]).filter(Boolean);
}

function VaultRow({ v }) {
  return (
    <Link className="mkt-row earn-row" to={`/earn/${v.symbol}`}>
      <span className="row-left">
        <AssetIcon symbol={v.symbol} small />
        <span className="row-id">
          <span className="sym">
            {v.symbol}
            {v.flagship && <span className="chip accent" style={{ marginLeft: 8 }}>Flagship</span>}
          </span>
          <span className="name">{v.name}</span>
        </span>
      </span>
      <span className="r">
        <ApyValue vault={v} />
        <span className="sub">{v.apy_status === 'measured' ? '30d blended' : v.apy_label}</span>
      </span>
      <span className="r hide-sm">
        {v.withdraw_label}
      </span>
      <span className="r hide-sm">{v.chain}</span>
    </Link>
  );
}

export default function EarnHome() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [tab, setTab] = useState('featured');

  useEffect(() => {
    earnApi.earnVaults().then(setData).catch((e) => setErr(e.message || String(e)));
  }, []);

  const vaults = data?.vaults || [];
  const filtered = useMemo(() => {
    if (tab === 'featured' || tab === 'all') return vaults;
    return vaults.filter((v) => v.category === tab);
  }, [vaults, tab]);

  return (
    <section className="earn-home">
      <div className="market-head">
        <div>
          <div className="eyebrow tick">X Layer · single-asset savings</div>
          <h1>Earn</h1>
          <p className="sub">
            Deposit one asset. Sarf spreads it across verified venues and hands back a blended
            rate — no pools to pick, no thresholds to set.
          </p>
        </div>
      </div>

      {err && <p className="error" style={{ marginTop: 18 }}>{err}</p>}

      {!data ? (
        <div className="mkt-empty" style={{ marginTop: 24 }}>Reading X Layer…</div>
      ) : (
        <>
          <FeaturedStrip vaults={vaults} />

          {!data.deposits_open && (
            <p className="fine" style={{ margin: '14px 2px 0', textAlign: 'left', maxWidth: 'none' }}>
              Earn is in preview on getsarf.xyz. Rates below are real, measured numbers; deposits
              open shortly.
            </p>
          )}

          <div className="toolbar" style={{ marginTop: 26 }}>
            <div className="seg" role="tablist">
              {TABS.map((t) => (
                <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'on' : ''}
                        onClick={() => setTab(t)}>
                  {CATEGORY_LABEL[t]}
                </button>
              ))}
            </div>
          </div>

          <div className="mkt earn-table" style={{ marginTop: 14 }} role="table">
            <div className="mkt-head earn-cols" role="row">
              <span>Vault</span>
              <span className="r">30d APY</span>
              <span className="r hide-sm">Withdraw</span>
              <span className="r hide-sm">Chain</span>
            </div>
            {filtered.length === 0 && <div className="mkt-empty">No vaults in this category yet.</div>}
            {filtered.map((v) => <VaultRow key={v.symbol} v={v} />)}
          </div>

          <p className="fine" style={{ textAlign: 'left', margin: '14px 2px 0', maxWidth: 'none' }}>
            {data.not_a_forecast} {data.protection_limit}
          </p>
        </>
      )}
    </section>
  );
}
