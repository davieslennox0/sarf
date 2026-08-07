import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, ensureSession, verifyPasskey } from '../api.js';
import { connect, currentAccount, sendTransaction, short, txUrl } from '../wallet.js';

/**
 * The order signer. Claude links here (sign_url on every order). The page
 * shows what the server actually quoted — amounts, fee, price impact, risk
 * notes — and only then lets the user sign the exact transaction the server
 * built. Sarf cannot execute it; the wallet broadcasts and returns the hash,
 * which we record against the order for the audit trail.
 */

function Countdown({ until }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const ms = until * 1000 - now;
  if (ms <= 0) return <span className="chip red">expired</span>;
  const m = Math.floor(ms / 60000);
  const s = String(Math.floor((ms % 60000) / 1000)).padStart(2, '0');
  return <span className="chip amber">expires in {m}m {s}s</span>;
}

export default function Sign() {
  const [params] = useSearchParams();
  const orderId = params.get('o');
  const [order, setOrder] = useState(null);
  const [account, setAccount] = useState(null);
  const [phase, setPhase] = useState('review'); // review | signing | done
  const [result, setResult] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    currentAccount().then(setAccount).catch(() => {});
  }, []);

  useEffect(() => {
    if (!orderId) return;
    api.order(orderId).then(setOrder).catch((e) => setErr(e.message));
  }, [orderId]);

  if (!orderId) return <section><h1>Sign</h1><p className="error">Missing order id (?o=sarf_ord_…)</p></section>;
  if (err && !order) return <section><p className="error">{err}</p></section>;
  if (!order) return <section><p className="muted">Loading order…</p></section>;

  const expired = order.expired;
  const notSignable = !['proposed', 'awaiting_signature'].includes(order.status) || expired;
  const wrongAccount = account && order.address && account !== order.address.toLowerCase();

  const approve = async () => {
    setErr(null);
    try {
      const addr = account || (await connect());
      setAccount(addr);
      setPhase('signing');
      // Session first: recording the hash afterwards is an authenticated call,
      // and asking for the wallet signature after the transaction would be a
      // confusing second prompt.
      await ensureSession(addr);

      // Step-up if the server says this order needs it. Doing it here rather
      // than at order-build time means the assertion is fresh at signing.
      const st = await api.passkeyStatus().catch(() => null);
      const needsStepUp =
        st?.registered && order.est_usd != null && order.est_usd > (st.stepup_threshold_usd ?? Infinity);
      if (needsStepUp) await verifyPasskey();

      const hash = await sendTransaction(addr, order.tx);
      await api.orderSubmitted(orderId, hash);
      setResult({ hash });
      setPhase('done');
    } catch (e) {
      setErr(e.message || String(e));
      setPhase('review');
    }
  };

  if (phase === 'done' && result) {
    return (
      <section className="sign-card">
        <h1>Broadcast to X Layer ✓</h1>
        <p>
          Transaction: <code>{short(result.hash)}</code>{' '}
          <a href={txUrl(result.hash)} target="_blank" rel="noreferrer">view on explorer ↗</a>
        </p>
        <p className="muted">
          Settlement is final once mined. You can close this tab and return to the chat —
          ask for <i>settlement status</i> to confirm it there.
        </p>
      </section>
    );
  }

  const fee = order.platform_fee;
  return (
    <section className="sign-card">
      <h1>Review &amp; sign</h1>
      <div className="summary">
        {order.side?.toUpperCase()} {order.symbol} on X Layer
      </div>

      <div className="kv">
        <div><span>Action</span><b>{order.side} {order.symbol}</b></div>
        <div><span>Spending</span><b>{order.spending ?? order.amount_in}</b></div>
        {order.receiving_estimated && (
          <div><span>You receive (est.)</span><b>{order.receiving_estimated}</b></div>
        )}
        {order.minimum_received && (
          <div><span>Minimum received</span><b>{order.minimum_received}</b></div>
        )}
        <div><span>Order value</span><b>{order.est_usd != null ? `$${Number(order.est_usd).toFixed(2)}` : 'n/a'}</b></div>
        {/* Always rendered, including when nothing is charged: a missing fee
            row reads as "there is no fee", which is a claim we should make
            explicitly rather than by omission. */}
        <div>
          <span>Platform fee</span>
          <b>
            {fee?.charged
              ? `$${Number(fee.usd).toFixed(2)} ${fee.denominated_in || ''}`.trim()
              : 'none'}
          </b>
        </div>
        <div><span>Network gas</span><b>paid by you in OKB</b></div>
        <div><span>Validity</span><Countdown until={order.expires_at} /></div>
      </div>

      {order.risk_notes?.length > 0 && (
        <div className="risk">
          <div className="risk-title">Risk notes — read before signing</div>
          <ul>{order.risk_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </div>
      )}

      {notSignable ? (
        <div className="error">
          This order is no longer signable ({expired ? 'expired' : order.status}). Ask the
          assistant for a fresh quote — prices move.
        </div>
      ) : wrongAccount ? (
        <div className="error">
          Connected wallet {short(account)} does not match this order's wallet{' '}
          {short(order.address)}. Switch accounts to sign.
        </div>
      ) : (
        <div className="cta">
          <button className="primary big" disabled={phase !== 'review'} onClick={approve}>
            {phase === 'signing' ? 'Confirm in your wallet…' : account ? 'Sign & broadcast' : 'Connect wallet & sign'}
          </button>
        </div>
      )}

      {err && <div className="error">{err}</div>}

      <p className="muted small">
        Sarf built and priced this transaction but cannot execute it — only your wallet
        can. The bytes you sign are exactly what is shown above.
      </p>
    </section>
  );
}
