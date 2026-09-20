import React, { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api.js';
import { txUrl } from '../wallet.js';
import { Fact } from '../zapui.jsx';

/**
 * A trade receipt, and the means to disbelieve it.
 *
 * The page is deliberately not a summary. It shows the signed message
 * verbatim, the signature, the digest and the transaction the digest was
 * written into, because the whole value of the thing is that a reader does
 * not have to take Sarf's word for any of it. Everything needed to recompute
 * the digest and recover the signer is on the page.
 *
 * Public, like the order it describes: a receipt nobody can fetch proves
 * nothing to anybody.
 */
export default function Receipt() {
  const { id } = useParams();
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.receipt(id).then(setD).catch((e) => setErr(e.message));
  }, [id]);

  if (err) {
    return (
      <section>
        <h1>Receipt</h1>
        <p className="error">{err}</p>
        <p className="muted small">
          A receipt is issued when a trade settles. If the order is still pending, come
          back once it has confirmed.
        </p>
        <p><Link to="/portfolio">Your portfolio</Link></p>
      </section>
    );
  }
  if (!d) return <section><p className="muted">Loading receipt…</p></section>;

  const r = d.receipt;
  const copy = () => {
    navigator.clipboard?.writeText(d.canonical_json);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <section>
      <div className="market-head">
        <div>
          <div className="eyebrow tick">Signed and anchored on X Layer</div>
          <h1>Trade receipt</h1>
          <p className="sub">
            What Sarf quoted, what the chain settled, and a signature over both. Nothing
            here has to be taken on trust — the checks are below.
          </p>
        </div>
        <div className="market-stats">
          <div><b>{d.anchored_at ? 'Anchored' : 'Signed'}</b><span>{d.anchored_at ? 'on-chain' : 'not yet anchored'}</span></div>
        </div>
      </div>

      <div className="dp-facts" style={{ marginTop: 22 }}>
        <Fact label="Action" value={`${r.action} ${r.symbol}`} />
        <Fact label="Paid" value={r.paid} />
        <Fact label="Received at least" value={r.receivedAtLeast}
              sub="the minimum the trade would accept" />
        <Fact label="Quoted value"
              value={`$${(r.quotedValueUsdCents / 100).toFixed(2)}`}
              sub="at the moment it was quoted" />
      </div>

      <div className="card">
        <h3>What settled</h3>
        <div className="kv kv-figures">
          <div><span>Account</span><b className="mono-ish">{r.account}</b></div>
          <div><span>Transaction</span>
            <b><a href={txUrl(r.txHash)} target="_blank" rel="noreferrer">{r.txHash.slice(0, 14)}… ↗</a></b>
          </div>
          <div><span>Block</span><b>{r.blockNumber.toLocaleString()}</b></div>
          <div><span>Quoted</span><b>{new Date(r.quotedAt * 1000).toLocaleString()}</b></div>
          <div><span>Settled</span><b>{new Date(r.settledAt * 1000).toLocaleString()}</b></div>
        </div>
      </div>

      <div className="card">
        <h3>How to check it yourself</h3>
        <ol className="claim-steps">
          {d.verify.how.map((h) => <li key={h}>{h}</li>)}
        </ol>
        <div className="kv">
          <div><span>Standard</span><b>{d.verify.standard}</b></div>
          <div><span>Signer</span><b className="mono-ish">{d.verify.signer}</b></div>
          <div><span>Digest</span><b className="mono-ish break">{d.digest}</b></div>
          <div><span>Signature</span><b className="mono-ish break">{d.signature}</b></div>
        </div>
        <div className="side-actions">
          <button className="btn ghost" onClick={copy}>{copied ? 'Copied' : 'Copy signed message'}</button>
          {d.anchor_explorer_url && (
            <a className="btn ghost" href={d.anchor_explorer_url} target="_blank" rel="noreferrer">
              Anchor transaction ↗
            </a>
          )}
        </div>
        <p className="muted small" style={{ marginTop: 12 }}>
          The anchor is an ordinary X Layer transaction whose input data is this digest.
          It fixes the receipt in time: Sarf can issue one late, but it cannot issue one
          into a block that has already passed.
        </p>
      </div>
    </section>
  );
}
