import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, ensureSession, getSession, verifyPasskey } from '../api.js';
import { connect, currentAccount, short } from '../wallet.js';
import { formatLeft, useGrantClock } from '../grant.js';
import SessionGrant from '../SessionGrant.jsx';
import { onPrivyChange, privyContext, privyEnabled } from '../privy.jsx';

/**
 * Export the wallet's private key.
 *
 * This is the exit. Sarf is non-custodial in the sense that matters — it never
 * holds the key — but a wallet you cannot take with you is still a wallet
 * someone else chose the walls for. This is the control that makes leaving
 * possible: import the key into MetaMask, OKX Wallet or anything else and the
 * account is fully yours, with or without Sarf.
 *
 * Two things about how it is built are deliberate.
 *
 * The key never touches this app. Privy renders it inside an iframe served
 * from its own origin, so the page you are reading cannot read it — which is
 * also why this cannot offer a "copy to clipboard" of its own, and should not
 * pretend to.
 *
 * The passkey is required first. Not because the server can enforce it — this
 * whole exchange is between the browser and Privy, and the server is not in it
 * — but because the product's promise is that your passkey stands in front of
 * anything that can move your money, and a private key moves all of it at
 * once. An export that asked for less than a trade does would make that
 * promise false at exactly the point it matters most.
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
        <b>{short(ctx.address)}</b>, which you can import into MetaMask, OKX
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

/**
 * The account, on one page.
 *
 * Previous shapes: one long scroll of every section stacked; then an index of
 * three rows that each navigated to their own screen; and a separate /security
 * page holding the session-key controls. Three layouts, and the third put the
 * two things people compare — what is connected, and what it may spend — on
 * different URLs.
 *
 * Now: a list of sections that unfold where they are. The index is still an
 * index (closed, it says what exists in a glance), opening one costs no
 * navigation, and /dashboard/<section> still deep-links to a section open, so
 * every link the server hands out keeps working — including the /security
 * URLs baked into server error messages, which redirect here.
 */

// Activity is deliberately NOT here. It had its own page in the nav the whole
// time, so the dashboard fold was a second door onto the same orders — and the
// one that cost an extra fetch on every dashboard load. /dashboard/activity
// still resolves; it redirects to the page.
const SECTIONS = [
  { id: 'agents', title: 'Agents', blurb: 'What is connected, and what it may do' },
  { id: 'security', title: 'Trading in chat', blurb: 'Session key, limits, revoke' },
  { id: 'credentials', title: 'Credentials', blurb: 'Wallet, passkey, session key, export' },
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
      const [grant, passkey, connections] = await Promise.all([
        api.grant().catch(() => null),
        api.passkeyStatus().catch(() => null),
        api.connections().catch(() => null),
      ]);
      setData({ session: getSession(), grant, passkey, connections });
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
  const {
    session, grant, passkey, connections, err, loadGrant, reload,
  } = useAccountData();
  const [msg, setMsg] = useState(null);
  const [grantErr, setGrantErr] = useState(null);

  // The URL is the open section, so a deep link and a click produce the same
  // state and the back button undoes an expansion.
  const open = SECTIONS.some((s) => s.id === section) ? section : null;
  const toggle = (id) =>
    navigate(id === open ? '/dashboard' : `/dashboard/${id}`, { replace: true });

  // Liveness is decided against the clock, not against whatever the fetch said
  // when the page opened — see grant.js.
  const { live, left } = useGrantClock(grant, loadGrant);
  const autonomous = grant?.approval_mode === 'autonomous';
  const previous = grant?.previous_grant;

  const rows = connections?.connections || [];
  // The assistants, separated from the browser session reading this page: the
  // question the section answers is "what else is holding a key to my wallet".
  const assistants = rows.filter((c) => c.kind === 'assistant');

  const body = {
    agents: () => (
      <>
        <div className="kv">
          <div><span>Connected as</span><b>{short(session?.address)}</b></div>
          <div><span>In-chat execution</span>
            <b className={live ? 'ok' : ''}>
              {live
                ? `live · ${formatLeft(left)} left`
                : previous ? 'ended — authorize again' : 'not set up'}
            </b></div>
          <div><span>Asks for a passkey</span>
            <b>
              {live && autonomous
                ? `above $${Number(grant?.autonomous_limit_usd || 0).toLocaleString()}`
                : 'on every trade'}
            </b>
          </div>
        </div>

        <div className="section-label" style={{ marginTop: 22 }}>Connected clients</div>
        {rows.length === 0 ? (
          <p className="muted small">
            Nothing is connected right now. Add the MCP connector in Claude or
            ChatGPT and it appears here.
          </p>
        ) : (
          <div className="ledger">
            {rows.map((c) => (
              <div className="row static" key={`${c.name}-${c.connected_at}`}>
                <span className="row-left">
                  <span className="tokenmark" style={{ background: clientColour(c) }}>
                    <i>{initials(c.name)}</i>
                  </span>
                  <span className="row-id">
                    <span className="sym">{c.name}</span>
                    <span className="name">
                      {c.kind === 'browser' ? 'this browser' : 'assistant · MCP connector'}
                      {' · connected '}
                      {new Date(c.connected_at * 1000).toLocaleTimeString()}
                    </span>
                  </span>
                </span>
                <span className="row-right">
                  <span className="chip green">live</span>
                </span>
              </div>
            ))}
          </div>
        )}
        <p className="muted small" style={{ marginTop: 12 }}>
          {assistants.length > 0
            ? `${assistants.map((c) => c.name).join(', ')} can read your holdings and build
               trades. Every trade is still signed by you, or settled under the session key
               within its on-chain caps.`
            : 'A connected assistant can read your holdings and build trades; it can never move funds to another address.'}
          {' '}Ending the session in the account menu revokes all of these at once —
          one session, one set of permissions, no second place to revoke.
        </p>
        {/* A name here is the client's own OAuth registration, not something
            Sarf verified. Said out loud rather than presented as identity. */}
        {rows.some((c) => !c.identified) && (
          <p className="muted small">
            Connectors added before names were recorded, or with a ?key= URL, show
            as unnamed. They are still live sessions — end the session to clear them.
          </p>
        )}
      </>
    ),

    security: () => (
      <>
        <div className="card accent" style={{ marginTop: 0 }}>
          <h3>What a session key can and cannot do</h3>
          <p>
            <b>No keys, ever.</b> Sarf cannot sign for your wallet. Transfers to
            another address always need a fresh passkey and can never be delegated.
            <br /><br />
            <b>Capped in the contract.</b> A session key can only swap listed
            assets, under the per-trade and daily limits you set, until it
            expires — enforced on{' '}
            <a href="https://web3.okx.com/explorer/x-layer/address/0xaeBc963A2e8c3e42d070f5767Def5Fe430151946"
               target="_blank" rel="noreferrer">X Layer</a>, not by us.
            <br /><br />
            <b>Revoking needs nothing from Sarf.</b> It is a transaction from
            your own wallet, so it works whatever we do.
          </p>
        </div>
        {msg && <p className="ok">{msg}</p>}
        {grantErr && <p className="error">{grantErr}</p>}
        <SessionGrant
          onMessage={setMsg}
          onError={setGrantErr}
          passkey={passkey ? passkey.registered : (session?.hasPasskey ?? null)}
        />
      </>
    ),

    credentials: () => (
      <>
      <div className="kv">
        <div><span>Wallet</span><b className="addr">{session?.address || '—'}</b></div>
        <div><span>Network</span><b>X Layer · 196</b></div>
        <div><span>Session expires</span>
          <b>{session ? new Date(session.expiresAt).toLocaleString() : '—'}</b></div>
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
      {/* Last in the section, and after the facts rather than among them: it
          is the only control here, and the only one that hands out something
          irrevocable. */}
      <ExportKey />
      </>
    ),
  };

  return (
    <section>
      <h1>Dashboard</h1>
      <p className="sub">
        Everything this account has: what is connected to it, what it may spend
        without asking, and what it has done.
      </p>

      {err && (
        <>
          <p className="error">{err}</p>
          <div className="cta"><button onClick={reload}>Try again</button></div>
        </>
      )}

      <div className="folds">
        {SECTIONS.map((s) => {
          const isOpen = open === s.id;
          return (
            <div className={`fold${isOpen ? ' on' : ''}`} key={s.id}>
              <button className="fold-head" onClick={() => toggle(s.id)} aria-expanded={isOpen}>
                <span className="fold-main">
                  <b>{s.title}</b>
                  <span className="muted small">{summary(s.id, { live, left, rows, passkey }) || s.blurb}</span>
                </span>
                <span className="fold-go" aria-hidden="true">{isOpen ? '−' : '+'}</span>
              </button>
              {isOpen && <div className="fold-body">{body[s.id]()}</div>}
            </div>
          );
        })}
      </div>

      <p className="muted small" style={{ marginTop: 24 }}>
        Looking for the security page? It is here — <Link to="/dashboard/security">Trading in chat</Link>{' '}
        holds the session key and its limits.
      </p>
    </section>
  );
}

/** A closed row should still answer its own question. */
function summary(id, { live, left, rows, passkey }) {
  if (id === 'agents') {
    const names = (rows || []).filter((c) => c.kind === 'assistant').map((c) => c.name);
    if (names.length) return `${names.join(', ')} connected`;
    return rows?.length ? 'this browser only' : null;
  }
  if (id === 'security') return live ? `live · ${formatLeft(left)} left` : null;
  if (id === 'credentials') return passkey?.registered ? 'passkey registered' : null;
  return null;
}

const initials = (name) =>
  String(name || '?').replace(/[^A-Za-z0-9 ]/g, '').trim().slice(0, 2).toUpperCase() || '?';

/** Same deterministic-shade trick the token marks use — see markBg in Home.jsx
    for why this is a lightness rather than a hue. */
function clientColour(c) {
  const base = String(c.name || '?');
  let h = 0;
  for (const ch of base) h = (h * 31 + ch.charCodeAt(0)) % 360;
  const l = 21 + (h % 17);
  return `linear-gradient(140deg, hsl(214,7%,${l + 9}%), hsl(214,8%,${l}%))`;
}
