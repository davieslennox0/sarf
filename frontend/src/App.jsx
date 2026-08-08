import React, { useEffect, useState } from 'react';
import { Link, Route, Routes, useLocation } from 'react-router-dom';
import Home from './pages/Home.jsx';
import Activity from './pages/Activity.jsx';
import Portfolio from './pages/Portfolio.jsx';
import Security from './pages/Security.jsx';
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
function SessionBar() {
  const [account, setAccount] = useState(null);
  const [chain, setChain] = useState(null);
  const [session, setSession] = useState(getSession());
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    currentAccount().then(setAccount).catch(() => {});
    getChainId().then(setChain).catch(() => {});
    const offA = onAccountsChanged((a) => { setAccount(a); clearSession(); setSession(null); });
    const offC = onChainChanged(setChain);
    const t = setInterval(() => { setNow(Date.now()); setSession(getSession()); }, 1000);
    return () => { offA(); offC(); clearInterval(t); };
  }, []);

  const end = async () => {
    try { await api.logout(); } catch { /* revoke best-effort; clear locally regardless */ }
    clearSession();
    setSession(null);
  };

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

  if (!session) {
    return (
      <div className="bar">
        {account
          ? <>Wallet {short(account)} connected — sign in to trade.</>
          : 'Sign in with Google to get a wallet and start trading.'}
        <Onboarding onDone={() => {
          setSession(getSession());
          currentAccount().then(setAccount).catch(() => {});
        }} />
      </div>
    );
  }

  const left = Math.max(0, Math.floor((session.expiresAt - now) / 1000));
  return (
    <div className="bar live">
      Session active for {short(session.address)} — expires in{' '}
      {Math.floor(left / 60)}m {String(left % 60).padStart(2, '0')}s
      <button className="danger" onClick={end}>End session</button>
    </div>
  );
}

export default function App() {
  const { pathname } = useLocation();
  return (
    <div className="app">
      <nav>
        <Link className="brand" to="/">
          Sarf <span>/</span> X Layer RWA
        </Link>
        <div className="links">
          <Link className={pathname === '/' ? 'on' : ''} to="/">Markets</Link>
          <Link className={pathname === '/portfolio' ? 'on' : ''} to="/portfolio">Holdings</Link>
          <Link className={pathname === '/send' ? 'on' : ''} to="/send">Send</Link>
          <Link className={pathname === '/activity' ? 'on' : ''} to="/activity">Activity</Link>
          <Link className={pathname === '/security' ? 'on' : ''} to="/security">Security</Link>
        </div>
      </nav>
      <SessionBar />
      <main>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/send" element={<Transfer />} />
          <Route path="/security" element={<Security />} />
          <Route path="/sign" element={<Sign />} />
          {/* /authorize is the OAuth endpoint itself; the consent UI lives here. */}
          <Route path="/connect" element={<Authorize />} />
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
