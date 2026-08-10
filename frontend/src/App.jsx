import React, { useEffect, useState } from 'react';
import { Link, Route, Routes, useLocation } from 'react-router-dom';
import Home from './pages/Home.jsx';
import Activity from './pages/Activity.jsx';
import Portfolio from './pages/Portfolio.jsx';
import Markets from './pages/Markets.jsx';
import How from './pages/How.jsx';
import SecurityInfo from './pages/SecurityInfo.jsx';
import Connect from './pages/Connect.jsx';
import Settings from './pages/Settings.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Transfer from './pages/Transfer.jsx';
import Sign from './pages/Sign.jsx';
import Authorize from './pages/Authorize.jsx';
import { api, clearSession, getSession, registerPasskey } from './api.js';
import Onboarding from './Onboarding.jsx';
import {
  CHAIN_ID, currentAccount, chainId as getChainId,
  ensureXLayer, hasWallet, onAccountsChanged, onChainChanged, short,
} from './wallet.js';

/**
 * Session banner. Visible whenever a session is live, because a signing
 * surface should never leave you guessing whether something is still
 * authenticated. "End session" revokes server-side, which also disconnects
 * the MCP connector — ending it here ends it in Claude.
 */
/**
 * The account control in the header's right slot: a "Connect" button when there
 * is no session, and an address chip with a live expiry countdown when there
 * is. The countdown is on the chip rather than tucked in the menu because a
 * session that has quietly lapsed is the thing people are surprised by.
 */
function AccountControl({ session, setSession }) {
  const [now, setNow] = useState(Date.now());
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // Close on any click outside the menu, which is what a dropdown is expected
  // to do; without it the panel survives navigation and looks stuck.
  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!e.target.closest('.account')) setOpen(false); };
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [open]);

  const end = async () => {
    try { await api.logout(); } catch { /* revoke best-effort; clear locally regardless */ }
    clearSession();
    setSession(null);
    setOpen(false);
  };

  if (!session) return <Onboarding onDone={() => setSession(getSession())} />;

  const left = Math.max(0, Math.floor((session.expiresAt - now) / 1000));
  const mm = Math.floor(left / 60);
  return (
    <div className="account">
      <button className="account-chip" onClick={() => setOpen((v) => !v)}>
        <span className={`dot${mm < 5 ? ' soon' : ''}`} />
        {short(session.address)}
        <span className="ttl">{mm}m {String(left % 60).padStart(2, '0')}s</span>
      </button>
      {open && (
        <div className="account-menu">
          <div className="label">Signed in as</div>
          <div className="addr">{session.address}</div>
          <hr />
          <p className="muted small">
            Ending the session revokes it server-side, which also disconnects the
            MCP connector — ending it here ends it in Claude.
          </p>
          <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
            <Link className="cta-btn" to="/settings" style={{ padding: '9px 14px', fontSize: 11 }}
                  onClick={() => setOpen(false)}>
              Settings
            </Link>
            <button className="danger" onClick={end}>End session</button>
          </div>
        </div>
      )}
    </div>
  );
}

/** Only real problems get a strip: a wrong network, or no way to sign at all. */
function WarningBar({ setSession }) {
  const [account, setAccount] = useState(null);
  const [chain, setChain] = useState(null);

  useEffect(() => {
    currentAccount().then(setAccount).catch(() => {});
    getChainId().then(setChain).catch(() => {});
    const offA = onAccountsChanged((a) => { setAccount(a); clearSession(); setSession(null); });
    const offC = onChainChanged(setChain);
    return () => { offA(); offC(); };
  }, [setSession]);

  if (!hasWallet()) {
    return (
      <div className="bar warn">
        No EVM wallet detected —{' '}
        <a href="https://www.okx.com/web3" target="_blank" rel="noreferrer">install OKX Wallet</a>{' '}
        to trade on X Layer.
      </div>
    );
  }

  // Chain mismatch cannot happen on the embedded wallet — it is pinned to
  // X Layer — so this only ever fires for an injected provider.
  if (account && chain != null && chain !== CHAIN_ID) {
    return (
      <div className="bar warn">
        Wrong network — Sarf trades on X Layer (196).{' '}
        <button onClick={() => ensureXLayer().then(() => getChainId().then(setChain))}>
          Switch to X Layer
        </button>
      </div>
    );
  }
  return null;
}

/**
 * Stands in for a page that needs an account.
 *
 * It renders in place rather than redirecting home, because the URL is often
 * the payload: /sign?o=... is the link Claude hands the user from chat, and
 * /connect carries the OAuth request. Bouncing to / would sign them in and
 * then leave them staring at the markets page with the order id gone.
 */
function SignInRequired({ what, onDone }) {
  return (
    <section>
      <h1>Sign in</h1>
      <p className="muted">
        {what} belongs to your account, so it needs you signed in first. Sign in
        with Google and Sarf provisions a wallet for you — nothing to install.
      </p>
      <div className="cta"><Onboarding onDone={onDone} /></div>
    </section>
  );
}

/**
 * Blocking passkey registration. No dismiss, and deliberately so.
 *
 * Registering is now mandatory rather than offered: the passkey gates every
 * transaction, so an account without one is an account that cannot do
 * anything. Letting it be skipped only deferred that discovery to the first
 * trade, and — before the check moved to session level — stranded people with
 * no route back.
 *
 * This is the ONLY place a passkey is created. Onboarding's "Later" and the
 * settings register button are both gone, which is safe precisely because this
 * renders above every authenticated route, not just the sign-in flow.
 */
function RequirePasskey({ onDone }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const supported = typeof window !== 'undefined' && window.PublicKeyCredential;

  const add = async () => {
    setBusy(true); setErr(null);
    try {
      await registerPasskey();
      onDone();
    } catch (e) {
      setErr(e.message || String(e));
    } finally { setBusy(false); }
  };

  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h2>Add a passkey</h2>
        <p className="muted small">
          One touch of Face ID, Touch ID, or your device PIN. It is what approves
          every transaction on your account — nothing can be signed without it.
        </p>
        <p className="muted small">
          Your passkey never leaves your device, and it is not your wallet key —
          it approves actions, it cannot sign transactions on its own.
        </p>
        {!supported && (
          <p className="error">
            This browser does not support passkeys (WebAuthn). Open Sarf in a
            browser that does — there is no way to transact without one.
          </p>
        )}
        {err && <p className="error">{err}</p>}
        <div className="cta">
          <button className="primary" disabled={busy || !supported} onClick={add}>
            {busy ? 'Waiting for your device…' : 'Add passkey'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const { pathname } = useLocation();
  // Session state lives here so the nav, the bar and the route guards all read
  // the same thing. sessionStorage is the source of truth; this is a mirror of
  // it that re-renders, and it also expires on its own (getSession drops a
  // token past expiresAt), so the UI locks itself without anything to notify.
  const [session, setSession] = useState(getSession());
  useEffect(() => {
    const t = setInterval(() => setSession(getSession()), 1000);
    return () => clearInterval(t);
  }, []);

  const signedIn = Boolean(session);
  const refresh = () => setSession(getSession());
  const gate = (what, element) =>
    signedIn ? element : <SignInRequired what={what} onDone={refresh} />;

  // A session without a passkey cannot be reached, from any route.
  //
  // The prompt used to live only in Onboarding, but Portfolio, Activity, Sign,
  // Transfer and Settings each call ensureSession() themselves — so a new
  // account landing on any of them got a working session and never saw the
  // prompt, then found every transaction blocked by a gate it had no way to
  // satisfy. Checking here instead of in one component covers every path that
  // can ever mint a session, including ones added later.
  //
  // This is what makes registration genuinely mandatory, and it is the
  // precondition for removing the onboarding skip and the settings register
  // button: those were the escape hatches, and they are only safe to delete
  // once no one can end up needing them.
  const [needsPasskey, setNeedsPasskey] = useState(false);
  // Mobile nav. Collapsed by default and closed on navigation, so the menu
  // never sits open over the page the user just chose.
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => {
    let cancelled = false;
    if (!signedIn) { setNeedsPasskey(false); return undefined; }
    (async () => {
      try {
        const pk = await api.passkeyStatus();
        if (!cancelled) setNeedsPasskey(!pk?.registered);
      } catch {
        // Unknown state is not a reason to wave someone through: the check
        // fails toward the prompt, same as in Onboarding.
        if (!cancelled) setNeedsPasskey(true);
      }
    })();
    return () => { cancelled = true; };
  }, [signedIn, session?.address]);

  if (needsPasskey) return <RequirePasskey onDone={() => setNeedsPasskey(false)} />;

  return (
    <div className="app">
      <nav>
        <Link className="brand" to="/">
          Sarf <span>/</span> X Layer RWA
        </Link>
        <button className="nav-toggle" aria-label="Menu" aria-expanded={navOpen}
                onClick={() => setNavOpen((v) => !v)}>
          {navOpen ? '\u2715' : '\u2630'}
        </button>
        <div className={`links${navOpen ? ' open' : ''}`} onClick={() => setNavOpen(false)}>
          {/* Public first: everything here works without an account, including
              Portfolio, which reads any pasted address. The account-only pages
              are appended once there is a session rather than shown and then
              refused — each of them opens by asking the wallet who you are. */}
          <Link className={pathname === '/' ? 'on' : ''} to="/">Home</Link>
          <Link className={pathname === '/portfolio' ? 'on' : ''} to="/portfolio">Portfolio</Link>
          <Link className={pathname === '/markets' ? 'on' : ''} to="/markets">Markets</Link>
          <Link className={pathname === '/how' ? 'on' : ''} to="/how">How it works</Link>
          <Link className={pathname === '/security' ? 'on' : ''} to="/security">Security</Link>
          <Link className={pathname === '/connect' ? 'on' : ''} to="/connect">Connect</Link>
          {signedIn && (
            <>
              <Link className={pathname === '/dashboard' ? 'on' : ''} to="/dashboard">Dashboard</Link>
              <Link className={pathname === '/send' ? 'on' : ''} to="/send">Send</Link>
              <Link className={pathname === '/activity' ? 'on' : ''} to="/activity">Activity</Link>
            </>
          )}
        </div>
        <div className="header-right">
          <AccountControl session={session} setSession={setSession} />
        </div>
      </nav>
      <WarningBar setSession={setSession} />
      <main>
        <Routes>
          {/* Public. Portfolio is public on purpose: reading an address needs
              no account, and it degrades to a prompt only for "my holdings". */}
          <Route path="/" element={<Home />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/markets" element={<Markets />} />
          <Route path="/how" element={<How />} />
          <Route path="/security" element={<SecurityInfo />} />
          <Route path="/connect" element={<Connect />} />

          {/* Account-only. */}
          <Route path="/settings" element={gate('Your security settings', <Settings />)} />
          <Route path="/dashboard" element={gate('Your dashboard', <Dashboard />)} />
          <Route path="/activity" element={gate('Your activity', <Activity />)} />
          <Route path="/send" element={gate('Sending', <Transfer />)} />
          <Route path="/sign" element={gate('This transaction', <Sign />)} />
          {/* OAuth consent. /authorize is the server endpoint, which validates
              and hands off here; this route moved off /connect when that name
              was taken by the public setup page. */}
          <Route path="/approve" element={gate('This connection request', <Authorize />)} />
        </Routes>
      </main>
      {/* Site-wide, so it appears on the signer and consent pages too — not
          just the landing page. Year is derived, so it never goes stale. */}
      <div className="copyright">
        © {new Date().getFullYear()} Syketec Technologies. All rights reserved.
      </div>
    </div>
  );
}
