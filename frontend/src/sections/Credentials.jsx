import React, { useEffect, useState } from 'react';
import { verifyPasskey } from '../api.js';
import { shortAddr } from '../wallet.js';
import { formatLeft } from '../grant.js';
import { onPrivyChange, privyContext, privyEnabled } from '../privy.jsx';
import useAccountData from './useAccountData.js';

/**
 * Export the embedded wallet's private key. Only shown for a Privy embedded
 * wallet, behind a passkey, a written warning and Privy's own secure window.
 */
function ExportKey() {
  const [ctx, setCtx] = useState(privyContext());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [warned, setWarned] = useState(false);

  useEffect(() => onPrivyChange(setCtx), []);

  // Nothing to export on an injected wallet: the user already holds that key,
  // and Privy would throw if asked for one it does not have.
  if (!privyEnabled() || !ctx.embedded || !ctx.exportWallet) return null;

  const run = async () => {
    setBusy(true); setErr(null);
    try {
      await verifyPasskey();
      await ctx.exportWallet();
    } catch (e) {
      const m = e?.message || String(e);
      // A cancelled passkey prompt or a closed modal is a decision, not a
      // fault — saying "error" to someone who changed their mind is noise.
      if (!/abort|cancel|denied|NotAllowed|timed out/i.test(m)) setErr(m);
    } finally { setBusy(false); }
  };

  return (
    <div className="export-key">
      <div className="label">Export private key</div>
      <p className="muted small">
        Your wallet is yours to take. Exporting shows the private key for{' '}
        <b>{shortAddr(ctx.address)}</b>, which you can import into MetaMask, OKX
        Wallet or any other client. Sarf keeps working either way — this copies
        the key out, it does not close the account.
      </p>
      {!warned ? (
        <div className="cta">
          <button onClick={() => setWarned(true)}>Export private key</button>
        </div>
      ) : (
        <>
          <div className="bar warn" style={{ marginTop: 12 }}>
            <span>
              Anyone who sees this key owns this wallet — completely, and with
              no way to undo it. Nobody legitimate will ever ask you for it: not
              Sarf, not support, not the assistant in your chat. Do this alone,
              off a shared screen, and store it somewhere only you can reach.
            </span>
          </div>
          <p className="muted small" style={{ marginTop: 10 }}>
            Your passkey is required first, then Privy shows the key in its own
            secure window — Sarf never sees it and cannot recover it for you.
          </p>
          {err && <p className="error">{err}</p>}
          <div className="cta">
            <button className="danger" disabled={busy} onClick={run}>
              {busy ? 'Waiting for your passkey…' : 'I understand — show the key'}
            </button>
            <button disabled={busy} onClick={() => { setWarned(false); setErr(null); }}>
              Cancel
            </button>
          </div>
        </>
      )}
    </div>
  );
}

/** Wallet, passkey, session key and export, in one place. */
export default function Credentials() {
  const { session, grant, passkey, live, left } = useAccountData();
  const previous = grant?.previous_grant;
  return (
    <>
      <div className="kv">
        <div><span>Wallet</span><b className="addr">{session?.address || '—'}</b></div>
        <div><span>Network</span><b>X Layer · 196</b></div>
        <div><span>Session expires</span><b>{session ? new Date(session.expiresAt).toLocaleString() : '—'}</b></div>
        <div><span>Passkey</span>
          <b className={passkey?.registered ? 'ok' : 'error'}>{passkey?.registered ? 'registered' : 'not registered'}</b></div>
        <div><span>Last verified</span>
          <b>{passkey?.last_verified_at ? new Date(passkey.last_verified_at * 1000).toLocaleString() : 'never'}</b></div>
        {live ? (
          <>
            <div><span>Session key</span><b className="addr">{shortAddr(grant.grant.session_key || '')}</b></div>
            <div><span>Key expires</span>
              <b>{new Date(grant.grant.expires_at * 1000).toLocaleString()} · {formatLeft(left)} left</b></div>
          </>
        ) : (
          <div><span>Session key</span>
            <b>{previous ? `none, ${previous.reason} ${new Date(previous.ended_at * 1000).toLocaleString()}` : 'none'}</b></div>
        )}
      </div>
      <ExportKey />
    </>
  );
}
