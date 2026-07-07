import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api.js';

// Real numbers only: user count is a COUNT(*) over authenticated identities,
// TVL is the background on-chain scan of Sarf-tracked positions. No mocks —
// zeros are shown as zeros, with freshness stated instead of faked density.

function ago(ts) {
  if (!ts) return null;
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function usd(x) {
  return x.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

export default function Stats() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['stats'],
    queryFn: api.stats,
    refetchInterval: 60_000,
  });

  return (
    <section>
      <h1>Sarf usage</h1>
      <p className="muted">
        Assets supplied through positions opened or tracked via Sarf — <b>not</b> Current
        Finance&apos;s protocol-wide TVL.
      </p>

      {error && <div className="error">Stats unavailable: {error.message}</div>}

      <div className="cards">
        <div className="card">
          <div className="card-label">Total users</div>
          <div className="card-value">{isLoading ? <span className="skel" /> : data.total_users}</div>
          <div className="card-sub">unique identities that connected</div>
        </div>

        <div className="card">
          <div className="card-label">TVL via Sarf</div>
          <div className="card-value">
            {isLoading ? (
              <span className="skel" />
            ) : data.tvl ? (
              usd(data.tvl.usd)
            ) : (
              <span className="muted">no snapshot yet</span>
            )}
          </div>
          <div className="card-sub">
            {data?.tvl
              ? `${data.tvl.positions} positions across ${data.tvl.addresses_tracked} addresses`
              : 'first background scan pending'}
          </div>
        </div>

        <div className="card">
          <div className="card-label">Snapshot freshness</div>
          <div className="card-value">
            {isLoading ? <span className="skel" /> : (ago(data.last_updated) ?? '—')}
          </div>
          <div className="card-sub">TVL recomputed from chain every ~90s</div>
        </div>
      </div>

      <p className="muted small">
        Prices via Pyth oracles at scan time. Positions are read live from Current Finance
        obligations; nothing here is hardcoded or estimated.
      </p>
    </section>
  );
}
