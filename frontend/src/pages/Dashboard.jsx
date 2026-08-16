import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount, short } from '../wallet.js';
import { formatLeft, useGrantClock } from '../grant.js';

/**
 * The account dashboard: an index, then a page per section.
 *
 * It used to be one long scroll with every section stacked. As a list it says
 * what exists in a glance and puts each answer on its own screen — which is
 * also what makes it work on a phone: three tappable rows instead of a column
 * of key/value grids to scroll past looking for the one you wanted.
 *
 * Read-only throughout. The controls that change access live on /security, so
 * a page meant for glancing at cannot be where access changes by accident.
 */

const SECTIONS = [
  { id: 'agents', title: 'Agents', blurb: 'What is connected, and what it may do' },
  { id: 'credentials', title: 'Credentials', blurb: 'Wallet, passkey, session key' },
  { id: 'activity', title: 'Activity', blurb: 'Recent orders and their status' },
];

function useAccountData() {
  const [data, setData] = useState({ session: getSession() });
  const [err, setErr] = useState(null);

  const loadGrant = async () => {
    const grant = await api.grant().catch(() => null);
    setData((d) => ({ ...d, grant }));
  };

  // Named, so the error state can offer it again. The effect runs once; a
  // transient failure on it used to leave the dashboard permanently showing
  // an error with nothing to press.
  const load = async () => {
    setErr(null);
    try {
      const addr = (await currentAccount()) || (await connect());
      await ensureSession(addr);
      const [grant, passkey, orders] = await Promise.all([
        api.grant().catch(() => null),
        api.passkeyStatus().catch(() => null),
        api.myOrders().catch(() => null),
      ]);
      setData({ session: getSession(), grant, passkey, orders });
    } catch (e) {
      setErr(e.message || String(e));
    }
  };

  useEffect(() => { load(); }, []);

  return { ...data, err, loadGrant, reload: load };
}

export default function Dashboard() {
  const { section } = useParams();
  const navigate = useNavigate();
  const { session, grant, passkey, orders, err, loadGrant, reload } = useAccountData();

  // Liveness is decided against the clock, not against whatever the fetch said
  // when the page opened — see grant.js. The refetch on expiry replaces the
  // stale object with the server's own "this ended, and when".
  const { live, left } = useGrantClock(grant, loadGrant);
  const autonomous = grant?.approval_mode === 'autonomous';
  const previous = grant?.previous_grant;

  if (!section) {
    return (
      <section>
        <h1>Dashboard</h1>
        {err && (
          <>
            <p className="error">{err}</p>
            <div className="cta"><button onClick={reload}>Try again</button></div>
          </>
        )}
        <div className="rowlist">
          {SECTIONS.map((s) => (
            <Link key={s.id} className="rowlink" to={`/dashboard/${s.id}`}>
              <span className="rowlink-main">
                <b>{s.title}</b>
                <span className="muted small">{s.blurb}</span>
              </span>
              <span className="rowlink-go" aria-hidden="true">›</span>
            </Link>
          ))}
        </div>
      </section>
    );
  }

  const meta = SECTIONS.find((s) => s.id === section);
  if (!meta) {
    // Unknown section in the URL: back to the index rather than an empty page
    // that reads as data having failed to load.
    navigate('/dashboard', { replace: true });
    return null;
  }

  return (
    <section>
      <Link className="backlink" to="/dashboard">‹ Dashboard</Link>
      <h1>{meta.title}</h1>
      {err && (
          <>
            <p className="error">{err}</p>
            <div className="cta"><button onClick={reload}>Try again</button></div>
          </>
        )}

      {section === 'agents' && (
        session ? (
          <>
            <div className="kv">
              <div><span>Connected as</span><b>{short(session.address)}</b></div>
              <div><span>Session expires</span>
                <b>{new Date(session.expiresAt).toLocaleString()}</b></div>
              <div><span>In-chat execution</span>
                <b className={live ? 'ok' : ''}>
                  {live
                    ? `live · ${formatLeft(left)} left`
                    : previous ? 'ended — authorize again' : 'not set up'}
                </b></div>
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
            <p className="muted small">
              Ending a session in the account menu disconnects the MCP connector
              too — one session, one set of permissions, no second place to revoke.
            </p>
          </>
        ) : <p className="muted">No live session.</p>
      )}

      {section === 'credentials' && (
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
          {live ? (
            <>
              <div><span>Session key</span>
                <b className="addr">{short(grant.grant.session_key || '')}</b></div>
              <div><span>Per trade</span>
                <b>${Number(grant.grant.per_trade_cap_usd).toLocaleString()}</b></div>
              <div><span>Per day</span>
                <b>${Number(grant.grant.daily_cap_usd).toLocaleString()}</b></div>
              <div><span>Key expires</span>
                <b>{new Date(grant.grant.expires_at * 1000).toLocaleString()}
                  {' · '}{formatLeft(left)} left</b></div>
            </>
          ) : (
            // Never the old key. Once a grant ends there is no session key on
            // this account — the row is revoked server-side and its signing
            // material destroyed — so showing the address it used to have
            // would be describing something that no longer exists.
            <div><span>Session key</span>
              <b>{previous
                ? `none — ${previous.reason} ${new Date(previous.ended_at * 1000).toLocaleString()}`
                : 'none'}</b></div>
          )}
        </div>
      )}

      {section === 'activity' && (
        Array.isArray(orders?.orders) && orders.orders.length > 0 ? (
          // One per line, never comma-joined — the same rule the assistant
          // follows in chat. A run-together list is unreadable at exactly the
          // moment someone is checking whether a trade went through.
          <div className="kv">
            {orders.orders.map((o) => (
              <div key={o.order_id}>
                <span>{o.side} {o.symbol}</span>
                {/* est_usd, not estimated_usd: /api/me/orders returns the
                    column name, so the old key was undefined on every row and
                    no order here has ever shown its value. */}
                <b>
                  {o.status}
                  {o.est_usd != null ? ` · $${Number(o.est_usd).toFixed(2)}` : ''}
                </b>
              </div>
            ))}
          </div>
        ) : <p className="muted">No orders yet.</p>
      )}

      <div className="cta">
        <Link className="cta-btn" to="/security">Manage access</Link>
      </div>
    </section>
  );
}
