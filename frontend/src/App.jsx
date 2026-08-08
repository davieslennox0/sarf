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
import Transfer from './pages/Transfer.jsx';
import Sign from './pages/Sign.jsx';
import Authorize from './pages/Authorize.jsx';
import { api, clearSession, getSession } from './api.js';
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
 * The account control in the header's right slot: a "Log in" button when there
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

  return (
    <div className="app">
      <nav>
        <Link className="brand" to="/">
          Sarf <span>/</span> X Layer RWA
        </Link>
        <div className="links">
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
