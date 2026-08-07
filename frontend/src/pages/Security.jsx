import React, { useEffect, useState } from 'react';
import { api, ensureSession, registerPasskey, verifyPasskey } from '../api.js';
import { connect, currentAccount } from '../wallet.js';

/**
 * Passkey management. The copy here matters as much as the buttons: users
 * assume a passkey is a second signer, and it is not — the wallet signature is
 * what authorizes funds. This page says what the passkey actually protects.
 */
export default function Security() {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      const addr = (await currentAccount()) || (await connect());
      await ensureSession(addr);
      setStatus(await api.passkeyStatus());
    } catch (e) {
      setErr(e.message);
    }
  };

  useEffect(() => { load(); }, []);

  const run = async (fn, okMsg) => {
    setBusy(true); setErr(null); setMsg(null);
    try {
      await fn();
      setMsg(okMsg);
      setStatus(await api.passkeyStatus());
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const supported = typeof window !== 'undefined' && window.PublicKeyCredential;

  return (
    <section>
      <h1>Security</h1>

      <div className="disclosure">
        <b>What a passkey does here.</b> It is <b>not</b> a second signer — your wallet
        signature is what authorizes funds, always. The passkey closes two other gaps:
        it binds your session to this device, so a stolen connector token alone is
        useless; and it requires a fresh check before unusually large orders, so a
        compromised session cannot slip one past you.
      </div>

      {!supported && (
        <p className="error">This browser does not support passkeys (WebAuthn).</p>
      )}

      {status && (
        <div className="kv">
          <div><span>Wallet</span><b>{status.address}</b></div>
          <div><span>Passkey registered</span>
            <b className={status.registered ? 'ok' : 'error'}>
              {status.registered ? `yes (${status.credential_count})` : 'no'}
            </b>
          </div>
          <div><span>Step-up threshold</span><b>${Number(status.stepup_threshold_usd).toLocaleString()}</b></div>
          <div><span>Verification valid for</span><b>{status.stepup_valid_for_seconds}s</b></div>
          <div><span>Last verified</span>
            <b>{status.last_verified_at ? new Date(status.last_verified_at * 1000).toLocaleString() : 'never'}</b>
          </div>
        </div>
      )}

      <div className="cta">
        <button className="primary" disabled={busy || !supported}
                onClick={() => run(registerPasskey, 'Passkey registered.')}>
          {status?.registered ? 'Add another passkey' : 'Register a passkey'}
        </button>
        {status?.registered && (
          <>
            <button disabled={busy}
                    onClick={() => run(verifyPasskey, 'Verified — large orders are unlocked briefly.')}>
              Verify now
            </button>
            <button className="danger" disabled={busy}
                    onClick={() => run(() => api.passkeyStatus().then(() =>
                      fetch('/api/passkey', {
                        method: 'DELETE',
                        headers: { authorization: `Bearer ${JSON.parse(sessionStorage.getItem('sarf.session')).token}` },
                      })), 'Passkeys removed.')}>
              Remove passkeys
            </button>
          </>
        )}
      </div>

      {msg && <p className="ok">{msg}</p>}
      {err && <p className="error">{err}</p>}
    </section>
  );
}
