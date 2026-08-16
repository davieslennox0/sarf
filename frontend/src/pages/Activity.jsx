import React, { useEffect, useState } from 'react';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount, short, txUrl } from '../wallet.js';

const LABEL = {
  proposed: ['awaiting signature', 'amber'],
  awaiting_signature: ['awaiting signature', 'amber'],
  submitted: ['broadcast', 'green'],
  confirmed: ['confirmed ✓', 'green'],
  failed: ['reverted', 'red'],
  expired: ['expired unsigned', 'grey'],
};

export default function Activity() {
  const [orders, setOrders] = useState(null);
  const [err, setErr] = useState(null);
  const [address, setAddress] = useState(null);

  const load = async () => {
    setErr(null);
    try {
      const addr = (await currentAccount()) || (await connect());
      setAddress(addr);
      await ensureSession(addr);
      const d = await api.myOrders();
      setOrders(d.orders || []);
    } catch (e) {
      setErr(e.message);
    }
  };

  useEffect(() => { load(); }, []);

  // A failed load must not be a dead page. This effect runs once, so without a
  // way to re-run it the only escape from a transient error was a reload.
  if (err) {
    return (
      <section>
        <h1>My activity</h1>
        <p className="error">{err}</p>
        <div className="cta"><button onClick={load}>Try again</button></div>
      </section>
    );
  }
  if (!orders) return <section><h1>My activity</h1><p className="muted">Loading…</p></section>;

  return (
    <section>
      <h1>My activity</h1>
      <p className="muted small">
        Orders placed through Sarf by {short(address)}. Every settled trade links to
        the X Layer explorer — this is the on-chain proof, not a screenshot.
      </p>

      {orders.length === 0 ? (
        <p className="muted">
          No orders yet. Ask Claude to buy a tokenized stock and it will appear here.
        </p>
      ) : (
        <table className="orders">
          <thead>
            <tr>
              <th>When</th><th>Action</th><th>Value</th><th>Status</th><th>Transaction</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => {
              const [label, tone] = LABEL[o.status] || [o.status, 'grey'];
              return (
                <tr key={o.order_id}>
                  <td>{new Date(o.created_at * 1000).toLocaleString()}</td>
                  <td><b>{o.side}</b> {o.symbol}</td>
                  <td>{o.est_usd != null ? `$${Number(o.est_usd).toFixed(2)}` : '—'}</td>
                  <td><span className={`chip ${tone}`}>{label}</span></td>
                  <td>
                    {o.tx_hash ? (
                      <a href={txUrl(o.tx_hash)} target="_blank" rel="noreferrer">
                        {short(o.tx_hash)} ↗
                      </a>
                    ) : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
