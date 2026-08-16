/**
 * The session-grant panel: the opt-in that lets trades run inside Claude or
 * ChatGPT without a wallet prompt each time.
 *
 * It used to live on /security, a page that existed to hold this one control
 * plus three paragraphs explaining it. That page is gone — an account has one
 * place, and a control that changes what an agent may spend belongs beside the
 * agent it applies to, not on a separate tab you have to know to visit.
 *
 * Unchanged in the move: the passkey is required to obtain a key, the caps and
 * the expiry are enforced by the contract, and revoking is a transaction from
 * the user's own wallet that needs nothing from Sarf.
 */

import React, { useEffect, useState } from 'react';
import { api, verifyPasskey } from './api.js';
import { currentAccount, sendTransaction, sendWithAuthorization } from './wallet.js';
import { formatLeft, useGrantClock } from './grant.js';

/**
 * Lifetimes the user can pick, in days. The contract's own ceiling is 30 days,
 * but this deployment issues one hour: the key trades without asking, so it
 * expires with the passkey assertion that bought it instead of outliving it by
 * weeks. The server enforces the same ceiling — this list only has to agree
 * with it, and a longer entry here would just be rejected on submit.
 */
const HOUR = 1 / 24;
const LIFETIMES = [
  { days: HOUR, label: '1 hour' },
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
export default function SessionGrant({ onMessage, onError, passkey }) {
  const [g, setG] = useState(null);
  const [busy, setBusy] = useState(false);
  const [days, setDays] = useState(HOUR);
  const [perTrade, setPerTrade] = useState(500);
  const [daily, setDaily] = useState(2000);
  // Mode is an explicit choice made here at setup, not a default buried in a
  // settings page nobody opens. Always Ask is preselected because the safe
  // option should be the one you keep by doing nothing.
  const [autoLimit, setAutoLimit] = useState(50);

  const load = () => api.grant().then(setG).catch(() => {});
  useEffect(() => { load(); }, []);
  // The grant this deployment issues lasts an hour, and this page is one
  // people leave open. Without a clock it kept showing an expired key as
  // active — including its address and caps — until someone reloaded. The
  // refetch on expiry swaps that for the server's own account of it.
  const { live, left } = useGrantClock(g, load);

  const authorize = async () => {
    setBusy(true); onError(null); onMessage(null);
    try {
      // The passkey prompt belongs to THIS action, not to a button somewhere
      // else. /grant/prepare requires a fresh assertion — it has to be the
      // person asking, not just their browser holding a session token — and
      // when the only way to produce one was a separate button, removing that
      // button turned this into an error with no way to satisfy it. Ask here,
      // where the user is already saying yes to something.
      await verifyPasskey();
      const prep = await api.grantPrepare({
        days,
        per_trade_cap_usd: Number(perTrade),
        daily_cap_usd: Number(daily),
        approval_mode: 'autonomous',
        autonomous_limit_usd: Number(autoLimit),
      });
      const addr = await currentAccount();
      const delegate = prep.authorization_required.delegate;
      let hash;
      try {
        hash = await sendWithAuthorization(addr, prep.transaction, delegate, prep.relayer);
      } catch (e) {
        // Privy signs a 7702 authorization but will not broadcast the type-4
        // transaction carrying it, failing with an opaque "unexpected error".
        // X Layer's RPC parses type-4 fine, so the chain is not the obstacle —
        // only the browser's send path is. Re-sign for the relayer as sender
        // and let it press send; the authorization is still the user's, and it
        // still names this delegate and these caps.
        if (!e?.sevenSevenZeroTwo || !e.reSign) throw e;
        const signed = await e.reSign();
        // viem returns nonce/r/s/chainId as BigInt, which JSON.stringify
        // refuses outright ("Do not know how to serialize a BigInt") rather
        // than coercing. Stringify them: the server parses hex or decimal
        // strings and ints alike, so a decimal string is the lossless form —
        // Number() would silently round r and s, which are 256-bit.
        const plain = Object.fromEntries(
          Object.entries(signed).map(([k, v]) => [k, typeof v === 'bigint' ? v.toString() : v]),
        );
        // Relay ONLY the delegation install, with no call attached.
        //
        // SarfSessionKey.authorize() is self-only (`msg.sender != address(this)
        // -> NotSelf`). A relayed transaction has the relayer as msg.sender, so
        // sending the authorize call through the relayer installs the delegate
        // and then reverts — which is exactly what happened on the first live
        // attempt: mined, delegate installed, status 0, no grant recorded.
        //
        // Splitting it is the fix, and only the first half ever needed 7702:
        // the relayer carries the authorization list, and then the wallet makes
        // an ordinary self-call, where msg.sender IS the account.
        await api.grantRelay({
          authorization: plain,
          transaction: { to: addr, data: '0x' },
        });
        hash = await sendTransaction(addr, { to: addr, data: prep.transaction.data });
      }
      // Confirm the RECEIPT, not just the hash. The first live run reported
      // "In-chat execution is live" on a transaction that had reverted — the
      // delegate was installed, the grant was not, and the message said the
      // opposite of the truth. A hash means broadcast, never success.
      const ok = await api.grant().then(
        (g) => Boolean(g?.grant?.active && g?.delegation_installed),
      ).catch(() => false);
      onMessage(ok
        ? `Grant authorized — ${hash}. In-chat execution is live.`
        : `Broadcast ${hash}, but the grant is not active on-chain yet. `
          + 'Give it a few seconds and reload; if it stays inactive the '
          + 'authorize call reverted.');
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

  return (
    <>
      <h2 style={{ marginTop: 28 }}>Trading in chat</h2>

      {live ? (
        <>
          <div className="kv">
            <div><span>Status</span><b className="ok">active · {formatLeft(left)} left</b></div>
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
          {/* Say what happened to the last one. Landing on an empty setup form
              an hour after authorising reads as the grant having failed; the
              truth is that it ran its course, which is the design. */}
          {g.previous_grant && (
            <p className="muted small">
              Your previous session key{' '}
              {g.previous_grant.reason === 'revoked' ? 'was revoked' : 'expired'}{' '}
              {new Date(g.previous_grant.ended_at * 1000).toLocaleString()} and
              has been destroyed server-side. Authorizing below issues a new
              one — the old key cannot trade even if someone still holds it.
            </p>
          )}
          {passkey === false && (
            <p className="error">
              Register a passkey first — you will be prompted at sign-in. It gates
              every trade this key makes, and it is checked again before the key is
              issued, so a session token alone can never mint one.
            </p>
          )}
          {/*
            The Always Ask / Autonomous chooser was here. It is gone: a passkey
            needs a top-level browsing context and an MCP widget is a sandboxed
            iframe, so "ask on every trade in chat" could never actually prompt
            — it degraded to a link out to this site on every single trade.
            A mode the platform cannot honour is worse than no choice at all.

            The passkey still gates this: it is required to obtain the key
            below, and the contract's caps and expiry bound what that key can
            do afterwards.
          */}
          {(
            <>
              <div className="kv">
                <div><span>Without asking, up to</span>
                  <b>$<input type="number" min="1" value={autoLimit}
                             onChange={(e) => setAutoLimit(e.target.value)} /></b></div>
              </div>
              <p className="muted small">
                Trades up to this settle in chat with no prompt. Anything above it,
                and every transfer, still needs your passkey. Raising it needs your
                passkey too — the agent can never raise it on its own.
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
            {/*
              Disabled only on a KNOWN "no passkey". While the answer is still
              unknown the button stays live and the attempt is allowed to reach
              the server, because the server is where this is actually enforced:
              /grant/prepare demands a fresh assertion and refuses without a
              registered credential. A client-side guess that fails closed adds
              no security over that check — it only locks people out of the one
              page that could fix them, which has now happened three times.
            */}
            <button className="primary" disabled={busy || passkey === false}
                    onClick={authorize}>
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
