import React, { useEffect, useState } from 'react';
import { api, ensureSession, registerPasskey, verifyPasskey } from '../api.js';
import { connect, currentAccount, sendTransaction, sendWithAuthorization } from '../wallet.js';

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

      <SessionGrant onMessage={setMsg} onError={setErr} passkey={status?.registered} />
    </section>
  );
}

/** Days the user can pick. The contract refuses anything over 30 regardless. */
const LIFETIMES = [
  { days: 1, label: '24 hours' },
  { days: 7, label: '7 days' },
  { days: 14, label: '14 days' },
  { days: 30, label: '30 days' },
];

/**
 * Session-grant panel: the opt-in that lets trades run inside Claude or
 * ChatGPT without a wallet prompt each time.
 *
 * The copy carries as much weight as the controls. Users reasonably ask "so
 * you can spend my money now?" and the honest answer has a shape: Sarf holds
 * a key that can only swap, only these tokens, only under limits you set,
 * only until it expires, and you can end it without asking us. Every one of
 * those clauses is enforced by the contract, not by this page, and the page
 * should not imply otherwise.
 */
function SessionGrant({ onMessage, onError, passkey }) {
  const [g, setG] = useState(null);
  const [busy, setBusy] = useState(false);
  const [days, setDays] = useState(7);
  const [perTrade, setPerTrade] = useState(500);
  const [daily, setDaily] = useState(2000);

  const load = () => api.grant().then(setG).catch(() => {});
  useEffect(() => { load(); }, []);

  const authorize = async () => {
    setBusy(true); onError(null); onMessage(null);
    try {
      const prep = await api.grantPrepare({
        days, per_trade_cap_usd: Number(perTrade), daily_cap_usd: Number(daily),
      });
      const addr = await currentAccount();
      const hash = await sendWithAuthorization(
        addr, prep.transaction, prep.authorization_required.delegate,
      );
      onMessage(`Grant authorized — ${hash}. In-chat execution is live.`);
      await load();
    } catch (e) {
      onError(e.unsupported ? e.message : (e.message || String(e)));
    } finally { setBusy(false); }
  };

  const revoke = async () => {
    setBusy(true); onError(null); onMessage(null);
    try {
      const { transaction } = await api.grantRevoke();
      const addr = await currentAccount();
      // Sarf has already stopped signing; this is what stops everyone else.
      const hash = await sendTransaction(addr, transaction);
      onMessage(`Revoked on-chain — ${hash}. The key is now useless to anyone.`);
      await load();
    } catch (e) {
      onError(e.message || String(e));
    } finally { setBusy(false); }
  };

  if (!g) return null;
  const live = g.grant && g.grant.active && g.delegation_installed;

  return (
    <>
      <h2 style={{ marginTop: 28 }}>Trading in chat</h2>

      <div className="disclosure">
        <b>Optional, and off unless you turn it on.</b> Normally Sarf builds a trade
        and you sign it in your wallet. If you grant a session key, small trades can
        settle inside the chat instead — no wallet prompt, no tab switch.
        <br /><br />
        <b>What it can do:</b> swap the listed tokenized stocks, under the limits you
        set below, until it expires.
        <br />
        <b>What it cannot do:</b> move funds to any other address, spend your OKB,
        call any contract but the DEX router, or raise its own limits. Those are
        enforced by the <a href="https://web3.okx.com/explorer/x-layer/address/0x30eeC302C6D98253dCcA7d970343dBb95c920D76"
          target="_blank" rel="noreferrer">contract on X Layer</a>, not by us — and your
        wallet key never leaves your wallet.
      </div>

      {live ? (
        <>
          <div className="kv">
            <div><span>Status</span><b className="ok">active</b></div>
            <div><span>Expires</span>
              <b>{new Date(g.grant.expires_at * 1000).toLocaleString()}</b></div>
            <div><span>Per trade</span><b>${Number(g.grant.per_trade_cap_usd).toLocaleString()}</b></div>
            <div><span>Per day</span><b>${Number(g.grant.daily_cap_usd).toLocaleString()}</b></div>
            <div><span>Runs in chat under</span>
              <b>${Number(g.auto_execute_under_usd).toLocaleString()}</b></div>
            <div><span>Above that</span><b>passkey required</b></div>
          </div>
          <div className="cta">
            <button className="danger" disabled={busy} onClick={revoke}>
              Revoke this grant
            </button>
          </div>
          <p className="muted small">
            Revoking sends a transaction from your own wallet. Sarf stops using the key
            the moment you click, but the on-chain revoke is what makes it unusable by
            anyone — including us.
          </p>
        </>
      ) : (
        <>
          {!passkey && (
            <p className="error">
              Register a passkey first. It is what gates trades above your threshold,
              and issuing a key that can trade before that protection exists would
              hand you the convenience without the control.
            </p>
          )}
          <div className="kv">
            <div><span>Lasts for</span>
              <b>
                <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
                  {LIFETIMES.map((l) => (
                    <option key={l.days} value={l.days}>{l.label}</option>
                  ))}
                </select>
              </b>
            </div>
            <div><span>Max per trade (USD)</span>
              <b><input type="number" min="1" value={perTrade}
                        onChange={(e) => setPerTrade(e.target.value)} /></b></div>
            <div><span>Max per day (USD)</span>
              <b><input type="number" min="1" value={daily}
                        onChange={(e) => setDaily(e.target.value)} /></b></div>
          </div>
          <div className="cta">
            <button className="primary" disabled={busy || !passkey} onClick={authorize}>
              {busy ? 'Waiting for your wallet…' : 'Authorize session key'}
            </button>
          </div>
          <p className="muted small">
            Your wallet will ask you to sign twice-in-one: an EIP-7702 authorization
            and the grant itself. Both are yours to sign — Sarf cannot do either, which
            is why the grant can only ever say what you told it to.
          </p>
        </>
      )}
    </>
  );
}
