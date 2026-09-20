import React, { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, ensureSession, verifyPasskey } from '../api.js';
import {connect, currentAccount, sendTransaction, txUrl, waitForTx, shortAddr, shortHash} from '../wallet.js';
import { Fact } from '../zapui.jsx';

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
  return <span className="chip accent">expires in {m}m {s}s</span>;
}

export default function Sign() {
  const [params] = useSearchParams();
  const orderId = params.get('o');
  const [order, setOrder] = useState(null);
  const [account, setAccount] = useState(null);
  const [phase, setPhase] = useState('review'); // review | signing | done
  const [step, setStep] = useState(null);   // what the wallet is being asked for
  const [settled, setSettled] = useState(null); // true | false | null (still pending)
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

      // A token the router has never been allowed to pull needs one approval
      // first, or the swap reverts inside transferFrom: signed, paid for, and
      // nothing moved. Approve, wait for it to land, then send the trade.
      if (order._approval) {
        // The approval on the order is a snapshot from when it was built. On a
        // retry — or when an approve landed just after this page stopped
        // waiting — it is already in place, and sending a second one would
        // cost gas for nothing. Ask the chain, not the snapshot.
        setStep(`Checking the ${order._approval.symbol} allowance…`);
        const check = await api.orderApproval(orderId).catch(() => ({ needed: true }));
        if (check.needed) {
          setStep(`Approving ${order._approval.symbol}…`);
          const ah = await sendTransaction(addr, order._approval);
          const ok = await waitForTx(ah);
          if (ok === false) throw new Error(`The ${order._approval.symbol} approval reverted, so the trade was not sent.`);
          if (ok === null) {
            throw new Error('The approval has not confirmed yet. Wait a moment and press Sign again — '
              + 'it will pick up the approval rather than send a second one.');
          }
        }
      }

      setStep('Confirm the trade in your wallet…');
      const hash = await sendTransaction(addr, order.tx);

      // Past this line the transaction is on the network whatever else goes
      // wrong. Show it as sent BEFORE recording it: if the bookkeeping call
      // failed and we dropped back to the review screen, the user would be
      // looking at a Sign button for a trade that was already broadcast.
      setResult({ hash });
      setPhase('done');
      setStep(null);
      let recorded = true;
      try { await api.orderSubmitted(orderId, hash); } catch { recorded = false; }
      setResult({ hash, recorded });
      await watch(hash, recorded);
    } catch (e) {
      setErr(e.message || String(e));
      setPhase('review');
      setStep(null);
    }
  };

  /** Follow a broadcast to its end. Normally through the server, which reads
   *  the receipt; if it never took the hash, through the wallet's own node. */
  const watch = async (hash, recorded) => {
    setSettled(null);
    for (let i = 0; i < 40; i += 1) {
      if (recorded) {
        const s2 = await api.orderStatus(orderId).catch(() => null);
        if (s2?.state === 'confirmed') { setSettled(true); return; }
        if (s2?.state === 'failed') { setSettled(false); return; }
      } else {
        const ok = await waitForTx(hash, { timeoutMs: 2500, everyMs: 1200 });
        if (ok !== null) { setSettled(ok); return; }
      }
      await new Promise((r) => setTimeout(r, 3000));
    }
    // Two minutes without a receipt is unusual on X Layer, but "still
    // spinning" is not an answer. Say so and leave a way to look again.
    setSettled('slow');
  };

  if (phase === 'done' && result) {
    return (
      <section className="sign-card">
        <h1>
          {settled === true ? 'Settled on X Layer ✓'
            : settled === false ? 'The transaction reverted'
            : settled === 'slow' ? 'Still pending on X Layer'
            : 'Broadcast to X Layer'}
        </h1>
        <p>
          Transaction: <code>{shortHash(result.hash)}</code>{' '}
          <a href={txUrl(result.hash)} target="_blank" rel="noreferrer">view on explorer ↗</a>
        </p>
        {settled === null && <p className="muted">Waiting for it to be mined…</p>}
        {settled === 'slow' && (
          <p className="muted">
            It has not been mined after two minutes. The transaction is out there — nothing
            was lost and it has not been sent twice. Check the explorer, or look again.{' '}
            <button className="btn small" onClick={() => watch(result.hash, result.recorded)}>
              Check again
            </button>
          </p>
        )}
        {result.recorded === false && (
          <p className="muted small">
            Sarf could not file this against the order, so it may not show in your activity
            list. The transaction itself is unaffected.
          </p>
        )}
        {settled === false && (
          <p className="error">
            Nothing moved: your balances are unchanged and you paid only the gas. This
            usually means the price moved past your slippage while you were signing.
            Building the trade again gets a fresh quote.
          </p>
        )}
        {settled === true && (
          <p className="muted">
            Settlement is final. If this came from a chat, go back and ask for
            <i> settlement status</i> to see it there.
          </p>
        )}
        <div className="cta">
          {settled === true && (
            <Link className="btn primary" to={`/receipt/${orderId}`}>Receipt</Link>
          )}
          <Link className={`btn${settled === true ? '' : ' primary'}`} to="/portfolio">View portfolio</Link>
          <Link className="btn" to="/swap">{settled === false ? 'Try again' : 'Another swap'}</Link>
        </div>
      </section>
    );
  }

  const fee = order.platform_fee;
  const impact = order.price_impact_percent;
  const bigImpact = impact != null && Math.abs(impact) > 1;
  return (
    <section className="sign-card">
      <h1>Review &amp; sign</h1>
      <div className="summary">
        {order.side?.toUpperCase()} {order.symbol} on X Layer
      </div>

      {/* The numbers a signature turns on, before the full terms. Reading them
          out of a list of nine rows is how people sign what they did not
          mean to. */}
      <div className="dp-facts" style={{ marginTop: 18 }}>
        <Fact label="You pay" value={order.spending ?? order.amount_in} />
        {order.minimum_received && (
          <Fact label="You receive at least" value={order.minimum_received}
                sub="below this the trade reverts" />
        )}
        <Fact label="Order value"
              value={order.est_usd != null ? `$${Number(order.est_usd).toFixed(2)}` : 'n/a'} />
        {impact != null && (
          <Fact label="Price impact" value={`${Math.abs(impact).toFixed(2)}%`}
                tone={bigImpact ? 'warn' : undefined}
                sub={bigImpact ? 'your size moves this pool' : 'what your size costs'} />
        )}
      </div>

      {bigImpact && (
        <div className="callout warning" style={{ marginTop: 12 }}>
          <span className="callout-k">Price impact</span>
          <b className="callout-v">{Math.abs(impact).toFixed(2)}%</b>
          <span className="callout-s">
            Large for the pool behind this pair, and that cost is yours. A smaller size,
            or splitting it, usually fills closer to the quoted rate.
          </span>
        </div>
      )}

      <div className="kv">
        <div><span>Action</span><b>{order.side} {order.symbol}</b></div>
        <div><span>Spending</span><b>{order.spending ?? order.amount_in}</b></div>
        {/* Full, never truncated. 0x1edd…9110 is exactly the format in which a
            swapped character survives a glance, and a transfer cannot be undone. */}
        {order.recipient && (
          <div className="recipient">
            <span>Recipient</span><b>{order.recipient}</b>
          </div>
        )}
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
          Connected wallet {shortAddr(account)} does not match this order's wallet{' '}
          {shortAddr(order.address)}. Switch accounts to sign.
        </div>
      ) : (
        <div className="cta">
          {step && <p className="muted small" style={{ marginBottom: 10 }}>{step}</p>}
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
