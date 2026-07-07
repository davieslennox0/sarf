import React, { useEffect, useState } from 'react';
import { Link, Route, Routes, useLocation } from 'react-router-dom';
import { ConnectButton, useCurrentAccount, useDisconnectWallet } from '@mysten/dapp-kit';
import Stats from './pages/Stats.jsx';
import Activity from './pages/Activity.jsx';
import Sign from './pages/Sign.jsx';
import Authorize from './pages/Authorize.jsx';
import { api, clearSession, getSession } from './api.js';
import { SESSION_MINUTES, endEphemeralSession } from './zklogin.js';

/**
 * Session banner: permanent, visible whenever any signing capability is live
 * (wallet connected). Counts down; "End session" disconnects the wallet,
 * revokes the API session, and wipes any ephemeral key material immediately.
 */
function SessionBanner() {
  const account = useCurrentAccount();
  const { mutate: disconnect } = useDisconnectWallet();
  const [startedAt, setStartedAt] = useState(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (account && !startedAt) setStartedAt(Date.now());
    if (!account) setStartedAt(null);
  }, [account, startedAt]);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  if (!account || !startedAt) return null;

  const expiresAt = startedAt + SESSION_MINUTES * 60 * 1000;
  const msLeft = expiresAt - now;

  const end = () => {
    endEphemeralSession();
    // Revoke server-side BEFORE forgetting the token locally — this same
    // token is the MCP connector credential, and revocation is what actually
    // disconnects Claude. Passed explicitly so the ordering can't regress.
    api.logout(getSession()?.token);
    clearSession();
    disconnect();
  };

  if (msLeft <= 0) {
    end();
    return null;
  }
  const m = Math.floor(msLeft / 60000);
  const s = Math.floor((msLeft % 60000) / 1000);

  return (
    <div className="session-banner">
      <span className="dot" />
      Signing session active — {account.address.slice(0, 8)}…{account.address.slice(-4)} — expires
      in {m}m {String(s).padStart(2, '0')}s
      <button className="end-session" onClick={end}>
        End session
      </button>
    </div>
  );
}

export default function App() {
  const loc = useLocation();
  return (
    <div className="shell">
      <SessionBanner />
      <header>
        <div className="brand">
          <Link to="/" className="brand-link">
            <span className="logo">◈</span> Sarf
          </Link>
          <span className="tag">non-custodial Sui lending assistant</span>
        </div>
        <nav>
          <Link className={loc.pathname === '/' ? 'on' : ''} to="/">
            Stats
          </Link>
          <Link className={loc.pathname === '/activity' ? 'on' : ''} to="/activity">
            My activity
          </Link>
          <ConnectButton connectText="Connect wallet" />
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Stats />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/sign" element={<Sign />} />
          <Route path="/authorize" element={<Authorize />} />
        </Routes>
      </main>
      <footer>
        Sarf never holds keys. Transactions are built server-side, simulated, and signed only in
        your wallet.
      </footer>
    </div>
  );
}
