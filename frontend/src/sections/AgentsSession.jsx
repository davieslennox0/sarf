import React, { useState } from 'react';
import { api } from '../api.js';
import { currentAccount, sendTransaction, shortAddr } from '../wallet.js';
import { formatLeft } from '../grant.js';
import SessionGrant from '../SessionGrant.jsx';
import useAccountData from './useAccountData.js';
import { Fact } from '../zapui.jsx';

const initials = (name) =>
  String(name || '?').replace(/[^A-Za-z0-9 ]/g, '').trim().slice(0, 2).toUpperCase() || '?';

function clientColour(c) {
  let h = 0;
  for (const ch of String(c.name || '?')) h = (h * 31 + ch.charCodeAt(0)) % 360;
  const l = 21 + (h % 17);
  return `linear-gradient(140deg, hsl(214,7%,${l + 9}%), hsl(214,8%,${l}%))`;
}

/**
 * Agents & session: what is connected to this wallet, what it may do, the
 * session key that lets small trades settle in chat, and the controls to end
 * any of it. Revoke-all at the top; per-agent revoke on each row.
 */
export default function AgentsSession() {
  const { session, grant, passkey, connections, live, left, err, reload } = useAccountData();
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);

  const rows = connections?.connections || [];
  const agents = rows.filter((c) => !c.current);
  const autonomous = grant?.approval_mode === 'autonomous';

  const revokeOne = async (c) => {
    setBusy(c.id); setError(null); setMsg(null);
    try {
      await api.revokeConnection({ id: c.id });
      setMsg(`${c.name} disconnected. It will need to sign in again to reconnect.`);
      await reload();
    } catch (e) { setError(e.message || String(e)); } finally { setBusy(null); }
  };

  const revokeAll = async () => {
    setBusy('all'); setError(null); setMsg(null);
    try {
      const { revoked } = await api.revokeConnection({ all: true });
      let note = `Disconnected ${revoked} agent session${revoked === 1 ? '' : 's'}.`;
      if (live) {
        // The same two steps as the session key's own Revoke button: Sarf
        // stops using the key now, and the wallet signs the on-chain revoke.
        const { transaction } = await api.grantRevoke();
        const addr = await currentAccount();
        const hash = await sendTransaction(addr, transaction);
        note += ` Session key revoked on-chain (${hash.slice(0, 10)}…).`;
      }
      setMsg(note);
      await reload();
    } catch (e) {
      setError(`${e.message || String(e)}${live ? ' Agents were disconnected; the session key revoke did not finish. Use its Revoke button below.' : ''}`);
      await reload();
    } finally { setBusy(null); }
  };

  return (
    <>
      <div className="panel-head">
        <p className="muted small">
          Agents can read your holdings and build trades. They never hold your keys, and every
          trade is still signed by you or settled under the capped session key below.
        </p>
        <button className="danger" disabled={busy != null || (!agents.length && !live)} onClick={revokeAll}>
          {busy === 'all' ? 'Revoking…' : 'Revoke all'}
        </button>
      </div>
      {err && <p className="error">{err}</p>}
      {msg && <p className="ok">{msg}</p>}
      {error && <p className="error">{error}</p>}

      {/* What the key can do, and for how long. These are limits somebody
          agreed to, not reference detail — they belong where they can be
          read at a glance rather than three rows into a list. */}
      <div className="dp-facts" style={{ marginTop: 14 }}>
        <Fact label="Session key"
              value={live ? 'Live' : grant?.previous_grant ? 'Ended' : 'Not set up'}
              tone={live ? undefined : 'warn'}
              sub={live ? `${formatLeft(left)} left`
                : grant?.previous_grant ? 'set one up again below' : 'in-chat trades need one'} />
        {live && (
          <Fact label="Per trade"
                value={`$${Number(grant.grant.per_trade_cap_usd).toLocaleString()}`}
                sub="the contract's own ceiling" />
        )}
        {live && (
          <Fact label="Per day"
                value={`$${Number(grant.grant.daily_cap_usd).toLocaleString()}`}
                sub="across every in-chat trade" />
        )}
        {live && (
          <Fact label="Without a prompt"
                value={autonomous
                  ? `$${Number(grant?.autonomous_limit_usd || grant?.auto_execute_under_usd || 0).toLocaleString()}`
                  : 'Never'}
                sub={autonomous ? 'settles in chat up to this' : 'every trade asks you first'} />
        )}
      </div>

      <div className="kv">
        <div><span>Signed in as</span><b>{shortAddr(session?.address)}</b></div>
      </div>

      <div className="section-label" style={{ marginTop: 24 }}>Connected agents</div>
      {rows.length === 0 ? (
        <p className="muted small">
          Nothing is connected. Add the Sarf connector in Claude or ChatGPT and it appears here.
        </p>
      ) : (
        <div className="ledger">
          {rows.map((c) => (
            <div className="row static" key={c.id}>
              <span className="row-left">
                <span className="tokenmark" style={{ background: clientColour(c) }}><i>{initials(c.name)}</i></span>
                <span className="row-id">
                  <span className="sym">{c.name}</span>
                  <span className="name">
                    {c.kind === 'browser' ? (c.current ? 'this browser' : 'another browser') : 'reads holdings, builds trades'}
                    {' · since '}{new Date(c.connected_at * 1000).toLocaleTimeString()}
                  </span>
                </span>
              </span>
              <span className="row-right">
                {c.current
                  ? <span className="chip green">you</span>
                  : (
                    <button className="btn small" disabled={busy != null} onClick={() => revokeOne(c)}>
                      {busy === c.id ? 'Revoking…' : 'Revoke'}
                    </button>
                  )}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="card accent" style={{ marginTop: 24 }}>
        <h3>What a session key can and cannot do</h3>
        <p>
          <b>No keys, ever.</b> Sarf cannot sign for your wallet, and transfers to another
          address always need your passkey and can never be delegated.{' '}
          <b>Capped in the contract.</b> A session key can only swap listed assets, under the
          per-trade and daily limits you set, until it expires, enforced on{' '}
          <a href="https://web3.okx.com/explorer/x-layer/address/0xaeBc963A2e8c3e42d070f5767Def5Fe430151946"
             target="_blank" rel="noreferrer">X Layer</a>, not by us.{' '}
          <b>Revoking needs nothing from Sarf:</b> it is a transaction from your own wallet.
        </p>
      </div>
      <SessionGrant
        onMessage={setMsg}
        onError={setError}
        passkey={passkey ? passkey.registered : (session?.hasPasskey ?? null)}
      />
    </>
  );
}
