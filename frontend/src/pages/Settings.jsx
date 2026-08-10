import React, { useEffect, useState } from 'react';
import { api, ensureSession, verifyPasskey } from '../api.js';
import { connect, currentAccount, sendTransaction, sendWithAuthorization } from '../wallet.js';

/**
 * Session-key settings: caps, expiry, revoke.
 *
 * Passkey management deliberately does NOT live here — see the note in the
 * render below. The passkey is the gate on every transaction, registered at
 * sign-in, not a preference to toggle beside a spending limit.
 */
export default function Settings() {
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

  const supported = typeof window !== 'undefined' && window.PublicKeyCredential;

  return (
    <section>
      <h1>Security</h1>

      {/*
        Passkey management used to live here: register, verify, remove, plus a
        step-up threshold readout. It is gone because the passkey stopped being
        a setting. You register it when you sign in, and it gates every
        transaction from then on — there is no threshold to read and nothing to
        turn on. A page offering to "remove passkeys" also invited people to
        disable the only gate on their session key, which is not a preference
        worth exposing next to a spending limit.

        Status still surfaces where it is actionable: at sign-in if none is
        registered, and in the chat prompt when one needs verifying.
      */}

      {!supported && (
        <p className="error">This browser does not support passkeys (WebAuthn).</p>
      )}

      {status && (
        <div className="kv">
          <div><span>Wallet</span><b>{status.address}</b></div>
          <div><span>Passkey</span>
            <b className={status.registered ? 'ok' : 'error'}>
              {status.registered ? 'registered — gates every transaction' : 'not registered'}
            </b>
          </div>
        </div>
      )}

      {/*
        The recovery path, and the reason it has to exist.

        Registration normally happens at sign-in, so removing passkey
        MANAGEMENT from this page was right. Removing registration entirely was
        not: onboarding lets you skip the prompt ("a passkey prompt that cannot
        be dismissed is a dead end on any device where the ceremony fails"), and
        the skip handler's own comment pointed here as the way back. Anyone who
        signed up before the passkey became mandatory, or who skipped once, was
        left holding an account that could not transact and could not register —
        locked out by a gate with no door.

        Shown ONLY when none is registered. Once one exists there is nothing to
        manage here, which is the state this page was trimmed down to.
      */}
      {status && (
        <>
          <p className="muted small">
            {status.registered
              ? 'Your passkey gates every transaction. One verification covers the '
                + 'session; after it expires the next trade asks again.'
              : 'Your passkey gates every transaction, so nothing can be signed until '
                + 'one is registered. One touch of Face ID, Touch ID, or your device PIN.'}
          </p>
          <div className="cta">
            <button className="primary" disabled={busy || !supported}
                    onClick={async () => {
                      setBusy(true); setErr(null); setMsg(null);
                      try {
                        // Verify when one exists, register when it does not.
                        // Both controls have to stay reachable: an assertion
                        // expires with the session, so a page that only offers
                        // registration strands every user an hour later with a
                        // passkey they cannot use.
                        // Verify only. Registration lives in one place —
                        // App.jsx's blocking prompt — so there is no second
                        // path that can create a credential, and no button
                        // here that only makes sense in a state the app no
                        // longer lets you reach.
                        await verifyPasskey();
                        setMsg('Verified — trades are unlocked for this session.');
                        setStatus(await api.passkeyStatus());
                      } catch (e) {
                        setErr(e.message || String(e));
                      } finally { setBusy(false); }
                    }}>
              Verify with passkey
            </button>
          </div>
        </>
      )}

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
  // Mode is an explicit choice made here at setup, not a default buried in a
  // settings page nobody opens. Always Ask is preselected because the safe
  // option should be the one you keep by doing nothing.
  const [mode, setMode] = useState('always_ask');
  const [autoLimit, setAutoLimit] = useState(50);

  const load = () => api.grant().then(setG).catch(() => {});
  useEffect(() => { load(); }, []);

  const authorize = async () => {
    setBusy(true); onError(null); onMessage(null);
    try {
      const prep = await api.grantPrepare({
        days,
        per_trade_cap_usd: Number(perTrade),
        daily_cap_usd: Number(daily),
        approval_mode: mode,
        autonomous_limit_usd: mode === 'autonomous' ? Number(autoLimit) : 0,
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
            <div><span>Mode</span>
              <b>{g.approval_mode === 'autonomous' ? 'Autonomous' : 'Always Ask'}</b></div>
            <div><span>Passkey</span>
              <b>{g.approval_mode === 'autonomous'
                ? `required above $${Number(g.autonomous_limit_usd || 0).toLocaleString()}`
                : 'required on every trade'}</b></div>
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
              Register a passkey first — you will be prompted at sign-in. It gates
              every trade this key makes, and it is checked again before the key is
              issued, so a session token alone can never mint one.
            </p>
          )}
          <div className="modes">
            <button type="button"
                    className={`mode${mode === 'always_ask' ? ' on' : ''}`}
                    onClick={() => setMode('always_ask')}>
              <b>Always Ask</b>
              <span>Every trade needs your passkey. No exceptions, whatever the size.</span>
            </button>
            <button type="button"
                    className={`mode${mode === 'autonomous' ? ' on' : ''}`}
                    onClick={() => setMode('autonomous')}>
              <b>Autonomous</b>
              <span>
                Trades up to a limit you set go through without a prompt. Anything
                above it still asks.
              </span>
            </button>
          </div>

          {mode === 'autonomous' && (
            <>
              <div className="kv">
                <div><span>Without asking, up to</span>
                  <b>$<input type="number" min="1" value={autoLimit}
                             onChange={(e) => setAutoLimit(e.target.value)} /></b></div>
              </div>
              <p className="muted small">
                Changing this later needs your passkey again — the agent can never
                raise it on its own.
              </p>
            </>
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
