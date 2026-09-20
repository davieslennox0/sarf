import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import { api, clearSession, getSession, registerPasskey } from './api.js';
import Onboarding from './Onboarding.jsx';
import Sheet from './Sheet.jsx';
import { WalletCtx } from './walletctx.jsx';
import {
  CHAIN_ID, currentAccount, chainId as getChainId,
  ensureXLayer, hasWallet, onAccountsChanged, onChainChanged, shortAddr,
} from './wallet.js';
import { onPrivyChange, privyContext, privyEnabled } from './privy.jsx';

// Every page is its own chunk, fetched on first visit. Admin in particular is
// only ever requested by an operator, so its code never reaches anyone else.
const Home = lazy(() => import('./pages/Home.jsx'));
const Markets = lazy(() => import('./pages/Markets.jsx'));
const Portfolio = lazy(() => import('./pages/Portfolio.jsx'));
const Zap = lazy(() => import('./pages/Zap.jsx'));
const ZapPosition = lazy(() => import('./pages/ZapPosition.jsx'));
const Swap = lazy(() => import('./pages/Swap.jsx'));
const How = lazy(() => import('./pages/How.jsx'));
const Account = lazy(() => import('./pages/Account.jsx'));
const Admin = lazy(() => import('./pages/Admin.jsx'));
const Sign = lazy(() => import('./pages/Sign.jsx'));
const Authorize = lazy(() => import('./pages/Authorize.jsx'));
const AgentsSession = lazy(() => import('./sections/AgentsSession.jsx'));
const Credentials = lazy(() => import('./sections/Credentials.jsx'));

const NAV = [
  { to: '/markets', label: 'Markets', on: (p) => p === '/markets' },
  { to: '/portfolio', label: 'Portfolio', on: (p) => p === '/portfolio' },
  { to: '/zap', label: 'Zap', on: (p) => p.startsWith('/zap') },
];

const isPhone = () => typeof window !== 'undefined' && window.matchMedia('(max-width: 760px)').matches;

/**
 * The wallet pill. Signed out it is the Connect button; signed in it is a
 * status dot, the address and a chevron, and opens the account menu. Both
 * occupy the same reserved width, so signing in never moves the header.
 * The sign-in countdown lives inside the menu, not on the pill, so the pill's
 * width never changes as it ticks.
 */
function WalletMenu({ session, setSession, isAdmin }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [sheet, setSheet] = useState(null); // 'agents' | 'credentials' on phones
  const [now, setNow] = useState(Date.now());
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    const away = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('click', away);
    document.addEventListener('keydown', esc);
    return () => { clearInterval(t); document.removeEventListener('click', away); document.removeEventListener('keydown', esc); };
  }, [open]);

  if (!session) {
    return <div className="wallet-slot"><Onboarding onDone={() => setSession(getSession())} /></div>;
  }

  const left = Math.max(0, Math.floor((session.expiresAt - now) / 1000));
  const soon = session.expiresAt - Date.now() < 300000;
  const go = (to) => { setOpen(false); navigate(to); };
  const account = (id) => {
    setOpen(false);
    if (isPhone()) setSheet(id); else navigate(`/account#${id}`);
  };
  const end = async () => {
    try { await api.logout(); } catch { /* revoke best-effort; clear locally regardless */ }
    clearSession();
    setSession(null);
    setOpen(false);
  };
  const Section = sheet === 'agents' ? AgentsSession : Credentials;

  return (
    <div className="wallet-slot account" ref={ref}>
      <button className="wallet-pill" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className={`dot${soon ? ' soon' : ''}`} />
        <span className="wallet-addr">{shortAddr(session.address)}</span>
        <span className="caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="account-menu" role="menu">
          <div className="menu-id">
            <b className="wallet-addr">{shortAddr(session.address)}</b>
            <span className="muted small">Session: {Math.floor(left / 60)}m {String(left % 60).padStart(2, '0')}s</span>
          </div>
          <button role="menuitem" className="menu-item" onClick={() => go('/portfolio?fund=1')}>Fund</button>
          <button role="menuitem" className="menu-item" onClick={() => account('agents')}>Agents &amp; session</button>
          <button role="menuitem" className="menu-item" onClick={() => account('credentials')}>Credentials</button>
          {isAdmin && <button role="menuitem" className="menu-item" onClick={() => go('/admin')}>Admin</button>}
          <hr />
          <button role="menuitem" className="menu-item danger-text" onClick={end}>Sign out</button>
          <p className="muted small" style={{ padding: '6px 10px 2px' }}>
            Signing out also disconnects Claude and ChatGPT.
          </p>
        </div>
      )}
      <Sheet open={Boolean(sheet)} title={sheet === 'agents' ? 'Agents & session' : 'Credentials'} onClose={() => setSheet(null)}>
        <Suspense fallback={<p className="muted small">Loading…</p>}><Section /></Suspense>
      </Sheet>
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
        No EVM wallet detected.{' '}
        <a href="https://www.okx.com/web3" target="_blank" rel="noreferrer">Install OKX Wallet</a>{' '}
        to trade on X Layer.
      </div>
    );
  }
  if (account && chain != null && chain !== CHAIN_ID) {
    return (
      <div className="bar warn">
        Wrong network: Sarf trades on X Layer (196).{' '}
        <button onClick={() => ensureXLayer().then(() => getChainId().then(setChain))}>Switch to X Layer</button>
      </div>
    );
  }
  return null;
}

function SignInRequired({ what, onDone }) {
  return (
    <section>
      <h1>Sign in</h1>
      <p className="muted">
        {what} belongs to your account, so it needs you signed in first. Sign in
        with Google and Sarf provisions a wallet for you, nothing to install.
      </p>
      <div className="cta"><Onboarding onDone={onDone} /></div>
    </section>
  );
}

function RequirePasskey({ onDone, onLater, otherDomains = [] }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const supported = typeof window !== 'undefined' && window.PublicKeyCredential;
  const add = async () => {
    setBusy(true); setErr(null);
    try { await registerPasskey(); onDone(); } catch (e) { setErr(e.message || String(e)); } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h2>Add a passkey</h2>
        {otherDomains.length > 0 && (
          <p className="disclosure" style={{ margin: '0 0 12px' }}>
            Sarf moved to getsarf.xyz. Passkeys are tied to the website address they were
            made on, so the one you made on {otherDomains.join(', ')} can't be used here.
            Add one for getsarf.xyz: it takes one touch.
          </p>
        )}
        <p className="muted small">
          One touch of Face ID, Touch ID, or your device PIN. It confirms it is really you
          when a trade settles in chat and for every transfer to another address.
        </p>
        <p className="muted small">
          Your passkey never leaves your device, and it is not your wallet key: it approves
          actions, it cannot sign transactions on its own.
        </p>
        {!supported && (
          <p className="error">
            This browser does not support passkeys (WebAuthn). Open Sarf in a browser that does.
          </p>
        )}
        {err && <p className="error">{err}</p>}
        <div className="cta">
          <button className="primary" disabled={busy || !supported} onClick={add}>
            {busy ? 'Waiting for your device…' : 'Add passkey'}
          </button>
          {onLater && (
            <button className="ghost" onClick={onLater}>
              {supported ? 'Not now' : 'Continue without one'}
            </button>
          )}
        </div>
        <p className="muted small" style={{ marginTop: 10 }}>
          You can browse without it. Adding one is needed before a trade can settle in
          chat or a transfer leaves your wallet, and you can add it later under Account.
        </p>
      </div>
    </div>
  );
}

/** Old /dashboard/<section> links, mapped to where each section lives now. */
/** Links that predate the /account restructure — in old chat transcripts, in
 *  bookmarks, in emails — must still land somewhere sensible, carrying any
 *  query the old URL had (an OAuth `authorize` hands over a whole query
 *  string, and dropping it would break the consent flow). */
function DashboardRedirect() {
  const { section } = useParams();
  const { search, hash } = useLocation();
  const to = {
    deposit: '/portfolio?fund=1',
    activity: '/portfolio?tab=activity',
    agents: '/account#agents',
    security: '/account#agents',
    credentials: '/account#credentials',
    authorize: '/approve',
  }[section] || '/account';
  // Splice rather than concatenate: the target may already carry a query or a
  // fragment of its own, and `?fund=1?amount=50` is not a URL.
  const [path, ownHash] = to.split('#');
  const [base, ownQuery] = path.split('?');
  const query = [ownQuery, search.replace(/^\?/, '')].filter(Boolean).join('&');
  const frag = hash.replace(/^#/, '') || ownHash;
  return <Navigate to={base + (query ? `?${query}` : '') + (frag ? `#${frag}` : '')} replace />;
}

/** Admin is guarded twice: the server refuses non-admins on every call, and
 *  here a non-admin is sent to Markets before the admin chunk is ever loaded. */
function AdminRoute({ isAdmin, children }) {
  if (isAdmin === null) return <section><p className="muted small" style={{ marginTop: 24 }}>Checking access…</p></section>;
  if (!isAdmin) return <Navigate to="/markets" replace />;
  return children;
}

export default function App() {
  const { pathname, hash } = useLocation();

  // A #fragment in a client-side link does nothing on its own, since React
  // Router changes the URL without a document load. Scroll to it by hand.
  useEffect(() => {
    if (!hash) return undefined;
    const id = hash.slice(1);
    const raf = requestAnimationFrame(() => {
      document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    return () => cancelAnimationFrame(raf);
  }, [pathname, hash]);

  const [session, setSession] = useState(getSession());
  // Picks up sign-in, sign-out and expiry from other tabs and flows. Returns
  // the same object while nothing changed, so the whole app does not
  // re-render every second.
  useEffect(() => {
    const t = setInterval(() => setSession((s) => {
      const n = getSession();
      return n?.token === s?.token ? s : n;
    }), 1000);
    return () => clearInterval(t);
  }, []);
  const signedIn = Boolean(session);
  const refresh = () => setSession(getSession());

  // Operator or not. null until the server has answered, so the admin route
  // waits rather than bouncing an operator who is still being checked.
  const [isAdmin, setIsAdmin] = useState(null);
  useEffect(() => {
    if (!signedIn) { setIsAdmin(false); return undefined; }
    let cancelled = false;
    setIsAdmin(null);
    api.adminWhoami()
      .then((r) => { if (!cancelled) setIsAdmin(Boolean(r?.is_admin)); })
      .catch(() => { if (!cancelled) setIsAdmin(false); });
    return () => { cancelled = true; };
  }, [signedIn, session?.address]);

  const [walletReady, setWalletReady] = useState(!privyEnabled() || privyContext().ready);
  useEffect(() => {
    if (walletReady) return undefined;
    return onPrivyChange((c) => { if (c.ready) setWalletReady(true); });
  }, [walletReady]);

  const gate = (what, element) => {
    if (!signedIn) return <SignInRequired what={what} onDone={refresh} />;
    if (!walletReady) return <section><p className="muted small" style={{ marginTop: 24 }}>Restoring your session…</p></section>;
    return element;
  };

  // Everyone is asked for a passkey when they sign in, and nobody is asked
  // again by being locked out of the site. This used to return INSTEAD of the
  // app, so a wallet without a passkey for this domain could not so much as
  // look at Markets — which is what a signed-in visitor did after the move to
  // getsarf.xyz. The prompt now sits over the app at sign-in, and afterwards
  // is a line in the header. Signing still asks for it: that is where a
  // second factor means something.
  const [needsPasskey, setNeedsPasskey] = useState(false);
  const [otherDomains, setOtherDomains] = useState([]);
  const [promptPasskey, setPromptPasskey] = useState(false);
  const [hidPasskeyNote, setHidPasskeyNote] = useState(false);
  const wasSignedIn = useRef(false);
  useEffect(() => {
    let cancelled = false;
    if (!signedIn) {
      setNeedsPasskey(false);
      setHidPasskeyNote(false);
      wasSignedIn.current = false;
      return undefined;
    }
    const fresh = !wasSignedIn.current;   // signed in during this visit = signup
    wasSignedIn.current = true;
    (async () => {
      try {
        const pk = await api.passkeyStatus();
        if (cancelled) return;
        const missing = !pk?.registered;
        setNeedsPasskey(missing);
        setOtherDomains(pk?.other_domains || []);
        if (missing && fresh) setPromptPasskey(true);
      } catch {
        // A status we cannot read is not a reason to block the site.
        if (!cancelled) setNeedsPasskey(false);
      }
    })();
    return () => { cancelled = true; };
  }, [signedIn, session?.address]);

  const ctx = { session, address: session?.address || null, signedIn, isAdmin, refresh };

  return (
    <WalletCtx value={ctx}>
      <div className="app">
        {promptPasskey && (
          <RequirePasskey
            otherDomains={otherDomains}
            onDone={() => { setPromptPasskey(false); setNeedsPasskey(false); }}
            onLater={() => setPromptPasskey(false)}
          />
        )}
        {needsPasskey && !promptPasskey && !hidPasskeyNote && (
          <div className="passkey-note">
            <span>No passkey on this device yet — needed before a trade settles in chat or a transfer leaves your wallet.</span>
            <button className="btn small primary" onClick={() => setPromptPasskey(true)}>Add passkey</button>
            <button className="linkish" onClick={() => setHidPasskeyNote(true)}>Dismiss</button>
          </div>
        )}
        <nav>
          <Link className="brand" to="/">
            <img className="brand-mark" src="/sarf-logo.png" alt="" width="42" height="26" />
            Sarf <em className="tagline">Your X Layer RWA assistant</em>
          </Link>
          <div className="links">
            {NAV.map((n) => (
              <Link key={n.to} className={n.on(pathname) ? 'on' : ''} to={n.to}>{n.label}</Link>
            ))}
          </div>
          <div className="header-right">
            <WalletMenu session={session} setSession={setSession} isAdmin={isAdmin} />
          </div>
        </nav>
        <WarningBar setSession={setSession} />
        <main>
          <Suspense fallback={<p className="muted small" style={{ marginTop: 32 }}>Loading…</p>}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/markets" element={<Markets />} />
              <Route path="/portfolio" element={gate('Your portfolio', <Portfolio />)} />
              <Route path="/zap" element={<Zap />} />
              <Route path="/zap/:id" element={<ZapPosition />} />
              <Route path="/swap" element={<Swap />} />
              <Route path="/how" element={<How />} />
              <Route path="/account" element={gate('Your account', <Account />)} />
              <Route path="/admin" element={gate('The operator console', <AdminRoute isAdmin={isAdmin}><Admin /></AdminRoute>)} />
              <Route path="/admin/:section" element={gate('The operator console', <AdminRoute isAdmin={isAdmin}><Admin /></AdminRoute>)} />
              <Route path="/sign" element={gate('This transaction', <Sign />)} />
              <Route path="/approve" element={gate('This connection request', <Authorize />)} />

              {/* Old addresses. They are printed in chat histories, setup
                  instructions and error messages, so they redirect rather
                  than 404. The MCP deposit tool hands out /deposit. */}
              <Route path="/dashboard" element={<Navigate to="/account" replace />} />
              <Route path="/dashboard/:section" element={<DashboardRedirect />} />
              <Route path="/activity" element={<Navigate to="/portfolio?tab=activity" replace />} />
              <Route path="/deposit" element={<Navigate to="/portfolio?fund=1" replace />} />
              <Route path="/security" element={<Navigate to="/account#agents" replace />} />
              <Route path="/settings" element={<Navigate to="/account#agents" replace />} />
              <Route path="/send" element={<Navigate to="/portfolio" replace />} />
              <Route path="/connect" element={<Navigate to="/how" replace />} />
            </Routes>
          </Suspense>
        </main>
        {/* One footer for the whole site. The year is derived so it never
            goes stale. */}
        <footer className="site-foot">
          <div className="footrow">
            <span className="brandmark">
              <img className="brand-mark" src="/sarf-logo.png" alt="" width="52" height="32" />
              Sarf
              <em className="tagline">Your X Layer RWA assistant</em>
            </span>
            <div className="footlinks">
              <Link to="/markets">Markets</Link>
              <Link to="/swap">Swap</Link>
              <Link to="/how">How it works</Link>
              <a href="https://web3.okx.com/explorer/x-layer" target="_blank" rel="noreferrer">Explorer</a>
            </div>
          </div>
          <p className="fine" style={{ margin: 0 }}>
            Sarf is not a broker and is not a licensed adviser. A flat $0.01
            platform fee is charged per swap in the stablecoin leg, inside the same
            transaction you sign; network gas is separate and paid in OKB.
          </p>
          <div className="copyright">© {new Date().getFullYear()} Syketex Technologies. All rights reserved.</div>
        </footer>
      </div>
    </WalletCtx>
  );
}
