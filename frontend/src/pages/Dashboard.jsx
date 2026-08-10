import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount, short } from '../wallet.js';

/**
 * The account dashboard: what is connected, and what it is allowed to spend.
 *
 * Two questions people actually have — "what has access to my wallet?" and
 * "what can it do without asking me?" — answered on one screen instead of
 * inferred from a settings page about caps. Everything here is read-only; the
 * controls that change access live on /settings, so a page meant for glancing
 * at cannot become a page where something is changed by accident.
 */
export default function Dashboard() {
  const [session, setSession] = useState(getSession());
  const [grant, setGrant] = useState(null);
  const [passkey, setPasskey] = useState(null);
  const [orders, setOrders] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const addr = (await currentAccount()) || (await connect());
        await ensureSession(addr);
        setSession(getSession());
        const [g, p, h] = await Promise.all([
          api.grant().catch(() => null),
          api.passkeyStatus().catch(() => null),
          api.myOrders().catch(() => null),
        ]);
        setGrant(g); setPasskey(p); setOrders(h);
      } catch (e) {
        setErr(e.message || String(e));
      }
    })();
  }, []);

  const live = grant?.grant && grant.grant.active && grant.delegation_installed;
  const autonomous = grant?.approval_mode === 'autonomous';

  return (
    <section>
      <h1>Dashboard</h1>
      {err && <p className="error">{err}</p>}

      {/* ---- Agents -------------------------------------------------------
          An "agent" here is anything holding a live session token: Claude,
          ChatGPT, or the site itself. The connector and the browser share one
          session by design, so this lists what that session can do rather than
          pretending to enumerate clients it cannot see. */}
      <h2 className="eyebrow tick">Agents</h2>
      {session ? (
        <div className="kv">
          <div><span>Connected as</span><b>{short(session.address)}</b></div>
          <div><span>Session expires</span>
            <b>{new Date(session.expiresAt).toLocaleString()}</b></div>
          <div><span>In-chat execution</span>
            <b className={live ? 'ok' : ''}>{live ? 'live' : 'not set up'}</b></div>
          <div><span>Approval mode</span>
            <b>{autonomous ? 'Autonomous' : 'Always Ask'}</b></div>
          <div><span>Asks for a passkey</span>
            <b>
              {autonomous
                ? `above $${Number(grant?.autonomous_limit_usd || 0).toLocaleString()}`
                : 'on every trade'}
            </b>
          </div>
        </div>
      ) : (
        <p className="muted">No live session.</p>
      )}
      <p className="muted small">
        Ending a session in the account menu disconnects the MCP connector too —
        one session, one set of permissions, no second place to revoke.
      </p>

      {/* ---- Credentials --------------------------------------------------- */}
      <h2 className="eyebrow tick">Credentials</h2>
      <div className="kv">
        <div><span>Wallet</span><b className="addr">{session?.address || '—'}</b></div>
        <div><span>Network</span><b>X Layer · 196</b></div>
        <div><span>Passkey</span>
          <b className={passkey?.registered ? 'ok' : 'error'}>
            {passkey?.registered ? 'registered' : 'not registered'}
          </b>
        </div>
        <div><span>Last verified</span>
          <b>
            {passkey?.last_verified_at
              ? new Date(passkey.last_verified_at * 1000).toLocaleString()
              : 'never'}
          </b>
        </div>
        {live && (
          <>
            <div><span>Session key</span>
              <b className="addr">{short(grant.grant.session_key || '')}</b></div>
            <div><span>Per trade</span>
              <b>${Number(grant.grant.per_trade_cap_usd).toLocaleString()}</b></div>
            <div><span>Per day</span>
              <b>${Number(grant.grant.daily_cap_usd).toLocaleString()}</b></div>
            <div><span>Key expires</span>
              <b>{new Date(grant.grant.expires_at * 1000).toLocaleString()}</b></div>
          </>
        )}
      </div>

      <div className="cta">
        <Link className="cta-btn" to="/settings">Manage access</Link>
      </div>

      {/* ---- Recent activity ---------------------------------------------- */}
      {Array.isArray(orders?.orders) && orders.orders.length > 0 && (
        <>
          <h2 className="eyebrow tick">Recent</h2>
          {/* One per line, never comma-joined — the same rule the assistant
              follows in chat. A run-together list of trades is unreadable at
              exactly the moment someone is checking whether one went through. */}
          <div className="kv">
            {orders.orders.map((o) => (
              <div key={o.order_id}>
                <span>{o.side} {o.symbol}</span>
                <b>{o.status}{o.estimated_usd ? ` · $${o.estimated_usd}` : ''}</b>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
