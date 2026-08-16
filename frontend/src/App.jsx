import React, { useEffect, useState } from 'react';
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import Home from './pages/Home.jsx';
import Activity from './pages/Activity.jsx';
import Portfolio from './pages/Portfolio.jsx';
import Markets from './pages/Markets.jsx';
import How from './pages/How.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Sign from './pages/Sign.jsx';
import Authorize from './pages/Authorize.jsx';
import { api, clearSession, getSession, registerPasskey } from './api.js';
import Onboarding from './Onboarding.jsx';
import {
  CHAIN_ID, currentAccount, chainId as getChainId,
  ensureXLayer, hasWallet, onAccountsChanged, onChainChanged, short,
} from './wallet.js';
import { onPrivyChange, privyContext, privyEnabled } from './privy.jsx';

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
            <Link className="cta-btn" to="/dashboard/security" style={{ padding: '9px 14px', fontSize: 11 }}
                  onClick={() => setOpen(false)}>
              Security
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
 * /approve carries the OAuth request. Bouncing to / would sign them in and
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

  // Whether the wallet layer can answer questions yet.
  //
  // Privy rehydrates asynchronously, and account pages open by asking it who
  // you are. Every page used to ask immediately and deal with "not yet" on its
  // own — badly, in two cases, where the answer arrived as an error the page
  // never retried. Holding the route for one render instead is both simpler
  // and faster to read: the page mounts once, with an answer, rather than
  // mounting into a race and recovering from it.
  const [walletReady, setWalletReady] = useState(
    !privyEnabled() || privyContext().ready);
  useEffect(() => {
    if (walletReady) return undefined;
    return onPrivyChange((c) => { if (c.ready) setWalletReady(true); });
  }, [walletReady]);

  const gate = (what, element) => {
    if (!signedIn) return <SignInRequired what={what} onDone={refresh} />;
    if (!walletReady) {
      return (
        <section>
          <p className="muted small" style={{ marginTop: 24 }}>Restoring your session…</p>
        </section>
      );
    }
    return element;
  };

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
          {/* Public first. The account-only pages are appended once there is a
              session rather than shown and then refused — each of them opens by
              asking the wallet who you are.

              Security is not in the list at all any more: it was one control
              and three paragraphs about it, and it now unfolds inside the
              dashboard beside the agent it applies to. */}
          <Link className={pathname === '/' ? 'on' : ''} to="/">Home</Link>
          <Link className={pathname === '/markets' ? 'on' : ''} to="/markets">Markets</Link>
          <Link className={pathname === '/how' ? 'on' : ''} to="/how">How it works</Link>
          {signedIn && (
            <>
              <Link className={pathname === '/portfolio' ? 'on' : ''} to="/portfolio">Portfolio</Link>
              <Link className={pathname === '/dashboard' ? 'on' : ''} to="/dashboard">Dashboard</Link>
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
          {/* Portfolio is account-only. It used to read any pasted address,
              which made a wallet-shaped page look public. */}
          <Route path="/portfolio" element={gate('Your portfolio', <Portfolio />)} />
          <Route path="/markets" element={<Markets />} />
          <Route path="/how" element={<How />} />
          {/* /security is now a section of the dashboard. Kept as a redirect
              because the URL is printed in server error messages ("verify at
              …/security"), in the README, and in links already handed out. */}
          <Route path="/security" element={<Navigate to="/dashboard/security" replace />} />
          {/* /connect was the setup page; its content is now the whole of
              "How it works", since how it works and how you set it up were
              never two questions. Redirected rather than removed: the URL was
              printed in setup instructions and is out in the wild. */}
          <Route path="/connect" element={<Navigate to="/how" replace />} />

          {/* Account-only. */}
          {/* /settings folded into /security: same subject, two URLs, and the
              half with the controls was not the half users were sent to. */}
          <Route path="/settings" element={<Navigate to="/dashboard/security" replace />} />
          <Route path="/dashboard" element={gate('Your dashboard', <Dashboard />)} />
          {/* Same component: no :section renders the index, a section renders
              its own page. One data fetch, two layouts. */}
          <Route path="/dashboard/:section" element={gate('Your dashboard', <Dashboard />)} />
          <Route path="/activity" element={gate('Your activity', <Activity />)} />
          {/* /send removed — transfers belong in chat, where the passkey
              prompt is already the approval step. */}
          <Route path="/send" element={<Navigate to="/portfolio" replace />} />
          <Route path="/sign" element={gate('This transaction', <Sign />)} />
          {/* OAuth consent. /authorize is the server endpoint, which validates
              and hands off here; it lives on /approve rather than /connect,
              which used to be the public setup page. */}
          <Route path="/approve" element={gate('This connection request', <Authorize />)} />
        </Routes>
      </main>
      {/*
        One footer for the whole site.

        The disclosure and the fee line used to live inside Home.jsx, so they
        appeared on the landing page and nowhere else — not on the signer, not
        on a portfolio, not on the page Claude links people to. Those are the
        pages where "this is synthetic exposure and there is a fee" is load
        bearing rather than decorative, so the footer moved into the shell.
        Year is derived, so it never goes stale.
      */}
      <footer className="site-foot">
        <div className="footrow">
          <span className="brandmark">Sarf <span>/</span> X Layer RWA</span>
          <div className="footlinks">
            <Link to="/markets">Markets</Link>
            <Link to="/how">How it works</Link>
            <a href="https://web3.okx.com/explorer/x-layer" target="_blank" rel="noreferrer">
              Explorer
            </a>
          </div>
        </div>
        <p className="fine" style={{ margin: 0, textAlign: 'left' }}>
          Synthetic exposure. xStocks track the underlying share price only — no
          ownership, dividends, or voting rights, and redemption depends on the
          issuer. Sarf is not a broker and is not a licensed adviser. A flat $0.10
          platform fee is charged per swap in the stablecoin leg, inside the same
          transaction you sign; network gas is separate and paid in OKB.
        </p>
        <div className="copyright" style={{ margin: 0, padding: 0, border: 0, textAlign: 'left' }}>
          © {new Date().getFullYear()} Syketex Technologies. All rights reserved.
        </div>
      </footer>
    </div>
  );
}
