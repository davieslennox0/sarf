import React, { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { earnApi } from '../earn.js';
import { TokenMark } from '../market.jsx';
import { Fact } from '../zapui.jsx';

/**
 * The single-asset savings card: one asset, one rate, two buttons. No pools,
 * no venues, no thresholds — that is the whole point of Earn versus Zap, and
 * it holds on this page as much as it does on the list.
 *
 * Deposit/withdraw execution does not exist yet (EARN_ALLOCATOR_ENABLED is
 * false everywhere): the buttons are shown, honestly disabled, with the
 * reason stated rather than hidden.
 */

const FOLD_TITLE = 'How this earns / Risks';

export default function EarnAsset() {
  const { symbol } = useParams();
  const [v, setV] = useState(null);
  const [err, setErr] = useState(null);
  const [foldOpen, setFoldOpen] = useState(false);

  useEffect(() => {
    setV(null);
    setErr(null);
    earnApi.earnVault(symbol).then(setV).catch((e) => setErr(e.message || String(e)));
  }, [symbol]);

  if (err) {
    return (
      <section>
        <p className="error">{err}</p>
        <p><Link to="/earn">← All vaults</Link></p>
      </section>
    );
  }
  if (!v) return <section><div className="mkt-empty">Reading X Layer…</div></section>;

  const apyReady = v.apy_status === 'measured' && v.apy_pct != null;

  return (
    <section className="earn-asset">
      <Link className="backlink" to="/earn">← All vaults</Link>

      <div className="page-head" style={{ marginTop: 10 }}>
        <div className="earn-asset-title">
          <TokenMark asset={{ symbol: v.symbol }} />
          <div>
            <h1>{v.symbol}</h1>
            <p className="sub" style={{ margin: 0 }}>{v.name}</p>
          </div>
        </div>
      </div>

      <div className="dp-facts" style={{ marginTop: 22 }}>
        <Fact
          label="Blended APY"
          value={apyReady ? `${v.apy_pct.toFixed(1)}%` : (v.apy_status === 'measuring' ? 'Measuring' : 'New')}
          sub={v.apy_label}
        />
        <Fact label="Your balance" value={`0.00 ${v.symbol}`} sub="connect your wallet to see your deposit" />
        <Fact label="Earnings since deposit" value={`0.00 ${v.symbol}`} sub="accrues once you deposit" />
      </div>

      <div className="card" style={{ marginTop: 22 }}>
        <div className="cta" style={{ marginBottom: 0 }}>
          <button className="primary" disabled title={!v.deposits_open ? 'Earn is in preview — deposits are not open yet' : undefined}>
            Deposit
          </button>
          <button className="btn" disabled title={!v.deposits_open ? 'Earn is in preview — deposits are not open yet' : undefined}>
            Withdraw
          </button>
        </div>
        {!v.deposits_open && (
          <p className="muted small" style={{ marginTop: 12, marginBottom: 0 }}>
            Earn is in preview. The rate above is real and measured; deposits open shortly.
          </p>
        )}
      </div>

      <div className="folds" style={{ marginTop: 18 }}>
        <div className={`fold${foldOpen ? ' on' : ''}`}>
          <button className="fold-head" onClick={() => setFoldOpen((o) => !o)} aria-expanded={foldOpen}>
            <span className="fold-main"><b>{FOLD_TITLE}</b></span>
            <span className="fold-go" aria-hidden="true">{foldOpen ? '−' : '+'}</span>
          </button>
          {foldOpen && (
            <div className="fold-body">
              {(v.risk_faq || []).map((qa) => (
                <div key={qa.q} className="kv" style={{ marginBottom: 14 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{qa.q}</div>
                  <p className="muted small" style={{ margin: 0 }}>{qa.a}</p>
                </div>
              ))}
              <p className="fine" style={{ margin: '10px 0 0' }}>{v.protection_limit}</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
