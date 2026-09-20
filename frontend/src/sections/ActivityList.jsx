import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api.js';
import { shortHash, txUrl } from '../wallet.js';

const LABEL = {
  proposed: ['awaiting signature', 'accent'],
  awaiting_signature: ['awaiting signature', 'accent'],
  submitted: ['broadcast', 'green'],
  confirmed: ['confirmed ✓', 'green'],
  failed: ['reverted', 'red'],
  expired: ['expired unsigned', 'grey'],
};

/** Orders this wallet placed through Sarf, from chat or from the site. Mounted
 *  only when the Activity tab is first opened, so Holdings never pays for it. */
export default function ActivityList() {
  const [orders, setOrders] = useState(null);
  const [err, setErr] = useState(null);
  const load = () => api.myOrders().then((d) => setOrders(d.orders || [])).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);

  if (err) return <><p className="error">{err}</p><div className="cta"><button onClick={load}>Try again</button></div></>;
  if (!orders) return <p className="muted small">Loading…</p>;
  if (!orders.length) {
    return (
      <p className="muted" style={{ marginTop: 12 }}>
        No orders yet. <Link to="/swap">Make a swap</Link> here, or ask Sarf in Claude or ChatGPT.
      </p>
    );
  }
  return (
    <>
      <p className="muted small">Every settled trade links to the X Layer explorer: the on-chain proof, not a screenshot.</p>
      <table className="orders">
        <thead><tr><th>When</th><th>Action</th><th>Value</th><th>Status</th><th>Transaction</th></tr></thead>
        <tbody>
          {orders.map((o) => {
            const [label, tone] = LABEL[o.status] || [o.status, 'grey'];
            return (
              <tr key={o.order_id}>
                <td>{new Date(o.created_at * 1000).toLocaleString()}</td>
                <td><b>{o.side}</b> {o.symbol}</td>
                <td>{o.est_usd != null ? `$${Number(o.est_usd).toFixed(2)}` : '—'}</td>
                <td><span className={`chip ${tone}`}>{label}</span></td>
                <td>{o.tx_hash ? <a href={txUrl(o.tx_hash)} target="_blank" rel="noreferrer">{shortHash(o.tx_hash)} ↗</a> : '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
