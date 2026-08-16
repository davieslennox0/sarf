/**
 * Privy bridge.
 *
 * Privy is hooks-only, but `ensureSession()` in api.js is a plain async
 * function called from event handlers — it has to sign the login challenge and
 * it cannot call a hook. So the signer is published into a module-scoped ref by
 * a component mounted inside PrivyProvider, and wallet.js reads that ref. This
 * is the whole reason wallet.js survives the move to Privy: it is the non-hook
 * accessor the rest of the app already imports.
 *
 * Passkeys are deliberately NOT a Privy concern. Privy logs the user in with
 * Google and provisions the embedded wallet; approval of a transfer or a large
 * order is a WebAuthn assertion over a challenge OUR server generated and OUR
 * server verifies (passkey.py). Privy cannot substitute for that: its login
 * state is a claim the browser relays, not a signature over our nonce, and it
 * does not exist at all on the MCP path where Claude calls execute_order.
 * One passkey on this origin, one meaning: "approve this action."
 */

import React, { useEffect } from 'react';
import {
  PrivyProvider, usePrivy, useWallets, useSign7702Authorization,
} from '@privy-io/react-auth';

export const PRIVY_APP_ID = import.meta.env.VITE_PRIVY_APP_ID || '';

/** No app ID = Privy is off and wallet.js falls back to the injected provider. */
export function privyEnabled() {
  return Boolean(PRIVY_APP_ID);
}

// --- module-scoped signer ref ------------------------------------------------

let _ctx = {
  ready: false,
  authenticated: false,
  address: null,
  provider: null,      // EIP-1193 from the embedded wallet
  login: null,
  logout: null,
  signAuthorization: null,
};

const _subs = new Set();

export function privyContext() {
  return _ctx;
}

function publish(next) {
  _ctx = { ..._ctx, ...next };
  for (const cb of _subs) {
    try { cb(_ctx); } catch { /* a bad subscriber must not break the others */ }
  }
}

export function onPrivyChange(cb) {
  _subs.add(cb);
  return () => _subs.delete(cb);
}

/**
 * Resolve once Privy has finished initialising. -> true, or false on timeout.
 *
 * The reason this exists: `ready` is false for the first few hundred
 * milliseconds of every page load, and callers used to treat that as a
 * refusal — connect() threw "Still loading — try again in a moment." at
 * anything that ran during the window, including the mount-time load on
 * Dashboard and Activity, which never retried. The state those pages showed
 * was therefore decided by whether React got there before Privy did.
 *
 * Not initialising yet is a wait, not an error. Resolving false rather than
 * rejecting keeps that decision with the caller: connect() can raise a real
 * message, a read path can carry on as signed-out.
 */
export function waitForReady(timeoutMs = 12000) {
  if (!privyEnabled()) return Promise.resolve(true);
  if (privyContext().ready) return Promise.resolve(true);
  return new Promise((resolve) => {
    const timer = setTimeout(() => { off(); resolve(false); }, timeoutMs);
    const off = onPrivyChange((next) => {
      if (next.ready) { clearTimeout(timer); off(); resolve(true); }
    });
  });
}

/**
 * The signed-in address once Privy can answer -> address, or null.
 *
 * Two waits, both bounded: for initialisation, and — only if Privy says this
 * browser is authenticated — for the rehydrated wallet to publish its address.
 * A signed-in user reloading the page briefly has a session token but no
 * address, and reading that instant as "signed out" is what sent people to a
 * sign-in prompt they had already been through.
 */
export async function waitForAccount(timeoutMs = 8000) {
  if (!privyEnabled()) return null;
  const started = Date.now();
  if (!(await waitForReady(timeoutMs))) return privyContext().address || null;
  const c = privyContext();
  if (c.address || !c.authenticated) return c.address || null;
  const remaining = Math.max(0, timeoutMs - (Date.now() - started));
  if (!remaining) return null;
  return new Promise((resolve) => {
    const timer = setTimeout(() => { off(); resolve(privyContext().address || null); }, remaining);
    const off = onPrivyChange((next) => {
      if (next.address) { clearTimeout(timer); off(); resolve(next.address); }
    });
  });
}

/**
 * Resolve once an authenticated wallet with a usable provider exists.
 * Used by connect(): login() opens a modal and returns before the user has
 * finished, so the caller needs somewhere to wait that isn't a poll loop.
 */
export function waitForWallet(timeoutMs = 120000) {
  const c = privyContext();
  if (c.address && c.provider) return Promise.resolve(c.address);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      off();
      reject(new Error('Timed out waiting for the wallet to connect.'));
    }, timeoutMs);
    const off = onPrivyChange((next) => {
      if (next.address && next.provider) {
        clearTimeout(timer);
        off();
        resolve(next.address);
      }
    });
  });
}

// --- the component that fills the ref ----------------------------------------

/**
 * Renders nothing. Lives inside PrivyProvider purely so it can call the hooks
 * and push their values somewhere non-React code can reach.
 */
function PrivyBridge() {
  const { ready, authenticated, login, logout } = usePrivy();
  const { wallets } = useWallets();
  const { signAuthorization } = useSign7702Authorization();

  useEffect(() => {
    let cancelled = false;

    // Prefer the embedded wallet. A user who ALSO has OKX injected would
    // otherwise get whichever entry happens to be first, and the address the
    // session was minted for would stop matching the one that signs.
    const w =
      wallets.find((x) => x.walletClientType === 'privy') || wallets[0] || null;

    // Publish the auth state SYNCHRONOUSLY, before touching the provider.
    //
    // This used to live inside the async block below, so `ready` only reached
    // non-React code once getEthereumProvider() had resolved. That call can
    // hang indefinitely when the embedded wallet has not been provisioned yet
    // — which is exactly the state a first-time Google sign-in is in — and a
    // promise that never settles meant `ready: true` was never published at
    // all. connect() then failed its `if (!c.ready)` check forever and threw
    // "Still loading — try again in a moment." on every attempt, with no way
    // out but a reload that landed in the same state.
    //
    // Liveness and capability are two different facts and are now published
    // separately: whether Privy has finished initialising does not depend on
    // whether a signer exists yet.
    publish({
      ready,
      authenticated,
      login,
      logout,
      signAuthorization,
      address: w?.address ? w.address.toLowerCase() : null,
    });

    (async () => {
      if (!w) { if (!cancelled) publish({ provider: null }); return; }
      // Retry, because one failure here is not a permanent one.
      //
      // After a page reload Privy rehydrates as authenticated before the
      // embedded wallet finishes coming back, and getEthereumProvider() throws
      // "Unable to connect to wallet" for a second or two. A single attempt
      // published provider:null and this effect does not re-run on its own, so
      // the provider stayed null forever: connect() then skipped the login
      // modal (already authenticated) and sat in waitForWallet() until it timed
      // out. Hitting Connect after a refresh failed every time; a fresh sign-in
      // worked, which is what made it look like a wallet problem rather than a
      // rehydration race.
      // Backoff starts short and grows, rather than starting at 400ms.
      //
      // Rehydration usually succeeds on the second attempt, and the old
      // schedule (400, 800, 1200…) meant the common case waited 400ms for a
      // provider that was ready in ~50 — a delay every account page then sat
      // behind. 120ms first, doubling to a similar total: fast when it is
      // nearly ready, still patient when it is not.
      const backoff = [120, 240, 480, 900, 1600, 2400];
      for (let attempt = 0; attempt < backoff.length && !cancelled; attempt += 1) {
        try {
          const eip1193 = await w.getEthereumProvider();
          if (cancelled) return;
          if (eip1193) { publish({ provider: eip1193 }); return; }
        } catch {
          /* still rehydrating — fall through to the backoff below */
        }
        await new Promise((r) => setTimeout(r, backoff[attempt]));
      }
      if (!cancelled) publish({ provider: null });
    })();

    return () => { cancelled = true; };
  }, [ready, authenticated, wallets, login, logout, signAuthorization]);

  return null;
}

// --- provider ----------------------------------------------------------------

// X Layer as viem expects it. Declared inline rather than imported from
// viem/chains so a viem version bump cannot silently change what chain 196
// means to this app.
const xLayer = {
  id: 196,
  name: 'X Layer',
  nativeCurrency: { name: 'OKB', symbol: 'OKB', decimals: 18 },
  rpcUrls: { default: { http: ['https://rpc.xlayer.tech'] } },
  blockExplorers: {
    default: { name: 'OKX Explorer', url: 'https://web3.okx.com/explorer/x-layer' },
  },
};

/**
 * Wraps the app. With no app ID configured this is a pass-through, so the site
 * keeps working on the injected-wallet path while the Privy app is being set up.
 */
export function WalletProvider({ children }) {
  if (!privyEnabled()) return children;
  return (
    <PrivyProvider
      appId={PRIVY_APP_ID}
      config={{
        // Google for people who have never held a wallet, plus real wallets for
        // people who already have one and would rather bring it.
        //
        // Privy also offers PASSKEY as a login method. That one stays off, and
        // it is the only omission here that is a security decision rather than
        // a taste one — see the header comment.
        loginMethods: ['google', 'wallet'],
        appearance: {
          theme: 'dark',
          accentColor: '#e8a33d',
          logo: '/favicon.ico',
          // OKX first: X Layer is OKX's chain, so it is the wallet most likely
          // to already hold OKB for gas and to know the network.
          walletList: [
            'okx_wallet', 'metamask', 'coinbase_wallet',
            'rainbow', 'zerion', 'uniswap',
          ],
        },
        embeddedWallets: {
          ethereum: { createOnLogin: 'users-without-wallets' },
          // The signing surface shows what is being signed; Privy's own
          // confirmation modal on top of ours would be a second, differently
          // worded description of the same transaction.
          showWalletUIs: false,
        },
        defaultChain: xLayer,
        supportedChains: [xLayer],
      }}
    >
      <PrivyBridge />
      {children}
    </PrivyProvider>
  );
}
