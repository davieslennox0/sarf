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
      let eip1193 = null;
      try {
        eip1193 = w ? await w.getEthereumProvider() : null;
      } catch {
        eip1193 = null;
      }
      if (cancelled) return;
      publish({ provider: eip1193 });
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
