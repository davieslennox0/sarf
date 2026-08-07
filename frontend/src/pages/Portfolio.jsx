import React, { useEffect, useState } from 'react';
import { api, ensureSession } from '../api.js';
import { connect, currentAccount, short } from '../wallet.js';

export default function Portfolio() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  const load = async () => {
    setErr(null);
    try {
      const addr = (await currentAccount()) || (await connect());
      await ensureSession(addr);
      setData(await api.portfolio());
    } catch (e) {
      setErr(e.message);
    }
  };

  useEffect(() => { load(); }, []);

  if (err) {
    return (
      <section>
        <h1>Holdings</h1>
        <p className="error">{err}</p>
        <div className="cta"><button onClick={load}>Retry</button></div>
      </section>
    );
  }
  if (!data) return <section><h1>Holdings</h1><p className="muted small">Reading X Layer…</p></section>;

  const { positions = [], unpriced_positions: unpriced = [] } = data;

  return (
    <section>
      <h1>Holdings</h1>
      <p className="muted small">{short(data.address)} · read live from X Layer</p>

      <div className="stats">
        <div>
          <b>{data.total_value_usd != null ? `$${data.total_value_usd.toLocaleString()}` : '—'}</b>
          <span>total value</span>
        </div>
        <div><b>${Number(data.positions_value_usd || 0).toLocaleString()}</b><span>positions</span></div>
        <div><b>{data.usdt_balance}</b><span>USDT</span></div>
        <div><b>{Number(data.gas_balance_okb).toFixed(5)}</b><span>OKB gas</span></div>
      </div>

      {unpriced.length > 0 && (
        // Never let a pricing outage read as "these are worth nothing".
        <div className="disclosure">
          <b>{unpriced.join(', ')}</b> could not be priced right now, so the total above
          excludes them. They are still held — this is a quote outage, not a zero balance.
        </div>
      )}

      <h2>Positions</h2>
      {positions.length === 0 ? (
        <p className="muted small">
          No tokenized stocks held yet. Ask Claude to buy one and it will appear here.
        </p>
      ) : (
        <div className="ledger">
          {positions.map((p) => (
            <div className="row" key={p.symbol} style={{ cursor: 'default' }}>
              <span className="row-left">
                <span className="sym">{p.symbol}</span>
                <span className="name">{p.name.replace(' xStock', '')}</span>
              </span>
              <span className="row-right">
                <span className="price">{p.quantity}</span>
                <span className="chg">
                  {p.value_usd != null ? `$${p.value_usd.toLocaleString()}` : '—'}
                </span>
              </span>
            </div>
          ))}
        </div>
      )}

      <p className="fine" style={{ marginTop: 22 }}>
        Balances are read directly from chain state, not from a cached index — what you
        see here is what the contracts say you hold.
      </p>
    </section>
  );
}
