import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { usePrices } from './Home.jsx';
import {
  MarketTable, SearchIcon, SparkDefs, TopCards, byVolume, compactUsd, useMemoFilter, useOverview,
} from '../market.jsx';

const SORTS = [
  ['volume', 'Most traded'],
  ['gainers', 'Gainers'],
  ['losers', 'Losers'],
  ['az', 'A–Z'],
];

export default function Markets() {
  const [assets, setAssets] = useState([]);
  const [err, setErr] = useState(null);
  const [q, setQ] = useState('');
  const [sort, setSort] = useState('volume');

  useEffect(() => {
    api.list().then((d) => setAssets(d.assets || [])).catch((e) => setErr(e.message || String(e)));
  }, []);

  const symbols = useMemo(() => assets.map((a) => a.symbol), [assets]);
  const overview = useOverview(symbols);
  const prices = usePrices(symbols);

  const sorted = useMemo(() => {
    const ch = (a) => overview[a.symbol]?.change_24h_pct;
    if (sort === 'volume') return byVolume(assets, overview);
    if (sort === 'az') return [...assets].sort((a, b) => a.symbol.localeCompare(b.symbol));
    const sign = sort === 'gainers' ? -1 : 1;
    return [...assets].sort((a, b) => {
      const x = ch(a), y = ch(b);
      if (x == null) return 1;
      if (y == null) return -1;
      return sign * (x - y);
    });
  }, [assets, overview, sort]);
  const rows = useMemoFilter(sorted, q);

  const vals = Object.values(overview).filter(Boolean);
  const volume = vals.reduce((s, o) => s + (o.volume_24h_usd || 0), 0);
  const up = vals.filter((o) => (o.change_24h_pct || 0) > 0).length;

  return (
    <section>
      <SparkDefs />
      <div className="eyebrow tick">{assets.length || 43} tokenized assets on X Layer</div>
      <h1>Markets</h1>
      <p className="sub">
        Trade by the on-chain symbol, the x-suffix form (AAPLx). OKX's centralized order
        book lists the same underlying as XAAPL; that is a different venue and is not
        tradable here.
      </p>
      <div className="stats">
        <div><b>{volume ? compactUsd(volume) : '—'}</b><span>24h volume</span></div>
        <div><b>{vals.length ? `${up}/${vals.length}` : '—'}</b><span>up in 24h</span></div>
        <div><b>$0.01</b><span>per swap</span></div>
      </div>

      {err && <p className="error" style={{ marginTop: 18 }}>{err}</p>}

      <div className="section-label">Most traded today</div>
      <TopCards assets={byVolume(assets, overview).slice(0, 4)} prices={prices} overview={overview} />

      <div className="toolbar">
        <div className="seg" role="tablist">
          {SORTS.map(([k, label]) => (
            <button key={k} className={sort === k ? 'on' : ''} onClick={() => setSort(k)}>{label}</button>
          ))}
        </div>
        <label className="search">
          <SearchIcon />
          <input placeholder={`Search ${assets.length || 43} assets`} value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
      </div>
      {!assets.length && !err
        ? <div className="mkt-empty">Loading the registry…</div>
        : <MarketTable assets={rows} prices={prices} overview={overview} emptyText={`Nothing matches "${q}".`} />}

      <p className="fine" style={{ marginTop: 28 }}>
        Each asset links to its contract on the X Layer explorer. The registry is verified
        on-chain, and Sarf only ever resolves an asset from this list, never from an
        address a model hands it. Buy and Sell open your chat with the order written;
        you review and sign it in your wallet.
      </p>
    </section>
  );
}
