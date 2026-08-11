/**
 * X Layer wallet access over EIP-1193.
 *
 * Two sources, one interface. When Privy is configured (VITE_PRIVY_APP_ID) the
 * signer is the user's embedded wallet, provisioned from a Google login and
 * published into a module ref by the bridge in privy.jsx. Otherwise it is the
 * injected provider — OKX Wallet first, any other injection second.
 *
 * The point of keeping this module is that it is the app's only non-hook
 * accessor to a signer. `ensureSession()` in api.js has to sign the login
 * challenge from a plain async function, where a React hook cannot be called.
 * Every page imports these five functions and none of them needs to know which
 * source is behind them.
 *
 * No key material passes through here. The provider signs; we only ever hold
 * an address, a signature, and a transaction hash.
 */

import { onPrivyChange, privyContext, privyEnabled, waitForWallet } from './privy.jsx';

export const CHAIN_ID = 196;
export const CHAIN_ID_HEX = '0xc4';
export const EXPLORER = 'https://web3.okx.com/explorer/x-layer';

const X_LAYER_PARAMS = {
  chainId: CHAIN_ID_HEX,
  chainName: 'X Layer',
  nativeCurrency: { name: 'OKB', symbol: 'OKB', decimals: 18 },
  rpcUrls: ['https://rpc.xlayer.tech'],
  blockExplorerUrls: ['https://web3.okx.com/explorer/x-layer'],
};

function injected() {
  if (typeof window === 'undefined') return null;
  // Prefer the OKX injection: on a machine with several wallets, window.ethereum
  // is whichever one won the race, which is not necessarily the one the user
  // expects for an OKX-ecosystem chain.
  return window.okxwallet || window.ethereum || null;
}

export function provider() {
  return privyContext().provider || injected();
}

/**
 * Whether signing is possible at all. With Privy configured this is true before
 * login too — the wallet does not exist yet, but it will, and the user needs a
 * login button rather than "install a wallet extension".
 */
export function hasWallet() {
  return privyEnabled() || Boolean(injected());
}

/** True once a Privy embedded wallet is the active signer. */
export function isEmbedded() {
  return Boolean(privyContext().provider);
}

export async function connect() {
  if (privyEnabled()) {
    const c = privyContext();
    if (c.address && c.provider) return c.address;
    if (!c.ready) throw new Error('Still loading — try again in a moment.');
    // Opens the Privy modal. It returns before the user has finished, so the
    // address is awaited from the bridge rather than from this call.
    if (!c.authenticated && c.login) c.login();
    const address = await waitForWallet();
    await ensureXLayer();
    return address;
  }

  const p = injected();
  if (!p) {
    throw new Error(
      'No EVM wallet found. Install OKX Wallet to trade on X Layer.'
    );
  }
  const accounts = await p.request({ method: 'eth_requestAccounts' });
  if (!accounts || !accounts.length) throw new Error('No account authorized');
  await ensureXLayer();
  return accounts[0].toLowerCase();
}

export async function currentAccount() {
  if (privyEnabled()) return privyContext().address;
  const p = injected();
  if (!p) return null;
  const accounts = await p.request({ method: 'eth_accounts' });
  return accounts && accounts.length ? accounts[0].toLowerCase() : null;
}

export async function chainId() {
  const p = provider();
  if (!p) return null;
  return parseInt(await p.request({ method: 'eth_chainId' }), 16);
}

/** Switch to X Layer, adding it if the wallet doesn't know it yet. */
export async function ensureXLayer() {
  const p = provider();
  if (!p) throw new Error('No wallet');
  if ((await chainId()) === CHAIN_ID) return;
  try {
    await p.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: CHAIN_ID_HEX }],
    });
  } catch (e) {
    // 4902 = chain unknown to the wallet; add it, then the switch is implicit.
    if (e && (e.code === 4902 || e.code === -32603)) {
      await p.request({ method: 'wallet_addEthereumChain', params: [X_LAYER_PARAMS] });
    } else {
      throw e;
    }
  }
  if ((await chainId()) !== CHAIN_ID) {
    throw new Error('Please switch your wallet to X Layer to continue.');
  }
}

/** EIP-191 personal_sign of the server's login challenge. Authorizes nothing. */
export async function signMessage(address, message) {
  const p = provider();
  return p.request({ method: 'personal_sign', params: [message, address] });
}

/**
 * Send the server-built transaction. We pass through `to`/`data`/`value` and
 * let the wallet price gas itself — a stale gasPrice from our quote would be
 * a worse estimate than the wallet's live one, and gas is not part of what
 * the user is approving economically.
 */
export async function sendTransaction(address, tx) {
  const p = provider();
  await ensureXLayer();
  const params = {
    from: address,
    to: tx.to,
    data: tx.data,
    value: tx.value && tx.value !== '0' ? toHex(tx.value) : '0x0',
  };
  if (tx.gas) params.gas = toHex(tx.gas);
  return p.request({ method: 'eth_sendTransaction', params: [params] });
}

function toHex(v) {
  if (typeof v === 'string' && v.startsWith('0x')) return v;
  return '0x' + BigInt(v).toString(16);
}

/**
 * Send a transaction carrying an EIP-7702 authorization, delegating the
 * account to `delegate` in the same transaction that uses it.
 *
 * Wallet support for type-4 is uneven and there is no single agreed 1193
 * method yet, so this tries the shapes in circulation and reports honestly
 * rather than pretending. The two failure modes are told apart deliberately:
 * a wallet that cannot do 7702 is a "use a different wallet" problem, while a
 * rejection is the user changing their mind, and conflating them sends people
 * to install something they do not need.
 */
export async function sendWithAuthorization(address, tx, delegate, relayer) {
  const p = provider();
  await ensureXLayer();
  const base = {
    from: address,
    to: tx.to,
    data: tx.data,
    value: '0x0',
  };

  // Embedded path: Privy signs the authorization properly rather than us
  // guessing at a request shape. `executor: 'self'` because the same account
  // both authorizes and sends, which is what shifts the authorization nonce.
  const { signAuthorization } = privyContext();
  if (signAuthorization) {
    // executor: 'self' is right ONLY while the same account both authorizes and
    // sends. If the browser cannot broadcast type-4 we hand the authorization
    // to the relayer instead, and then a different account sends it — which
    // shifts whose nonce the authorization must carry. So sign twice-shaped:
    // try self first, and re-sign for a third-party executor on fallback.
    const signed = await signAuthorization(
      { contractAddress: delegate, chainId: CHAIN_ID, executor: 'self' },
      { address },
    );
    try {
      return await p.request({
        method: 'eth_sendTransaction',
        params: [{ ...base, type: '0x4', authorizationList: [signed] }],
      });
    } catch (e) {
      // Privy collapses failures here into "An unexpected error occurred.
      // Please try again later.", which names neither the cause nor the layer
      // it came from. EIP-7702 type-4 broadcast is the one unproven step in
      // this flow on chain 196, so an opaque failure here is exactly the case
      // that needs its detail preserved rather than flattened.
      const detail = [
        e?.details, e?.shortMessage, e?.cause?.message, e?.data?.message,
        typeof e?.code === 'number' ? `code ${e.code}` : null,
      ].filter(Boolean).join(' · ');
      const err = new Error(
        detail
          ? `Session key authorization failed: ${detail}`
          : `Session key authorization failed: ${e?.message || String(e)} `
            + '(EIP-7702 broadcast on X Layer)',
      );
      err.cause = e;
      // Marks this as "signing worked, sending did not" — the only case the
      // relayer fallback can rescue. Anything else should surface as itself.
      err.sevenSevenZeroTwo = true;
      // Re-sign for a THIRD-PARTY sender. EIP-7702 derives the authorization
      // nonce differently when the authority is not the account broadcasting,
      // so the self-signed one above cannot simply be forwarded to the relayer.
      err.reSign = relayer
        ? () => signAuthorization(
          { contractAddress: delegate, chainId: CHAIN_ID, executor: relayer },
          { address },
        )
        : null;
      // Full object to the console: the fields worth reading vary by which
      // layer rejected it, and guessing wrong loses the only copy.
      console.error('[sarf] 7702 authorization broadcast failed', e);
      throw err;
    }
  }

  const auth = [{ address: delegate, chainId: CHAIN_ID_HEX }];

  const attempts = [
    // Shape most wallets converged on: the authorization list rides on the
    // transaction and the wallet signs both parts.
    () => p.request({ method: 'eth_sendTransaction',
                      params: [{ ...base, type: '0x4', authorizationList: auth }] }),
    // Some builds accept the list without an explicit type.
    () => p.request({ method: 'eth_sendTransaction',
                      params: [{ ...base, authorizationList: auth }] }),
  ];

  let unsupported = null;
  for (const attempt of attempts) {
    try {
      return await attempt();
    } catch (e) {
      // 4001 is "user rejected" — stop immediately, do not retry another
      // shape and prompt them a second time for something they declined.
      if (e && (e.code === 4001 || /reject|denied/i.test(e.message || ''))) throw e;
      unsupported = e;
    }
  }
  const err = new Error(
    'This wallet could not sign an EIP-7702 authorization. In-chat execution ' +
    'needs one. You can still trade normally — every order is signed in your ' +
    'wallet as usual.'
  );
  err.cause = unsupported;
  err.unsupported = true;
  throw err;
}

export function onAccountsChanged(cb) {
  if (privyEnabled()) {
    // Callers treat this as "the account you had is gone" and drop the session.
    // The first login is null -> address, which is NOT that: firing there would
    // clear the session ensureSession() had just minted. Only a transition away
    // from an address we were already using counts.
    let last = privyContext().address;
    return onPrivyChange((next) => {
      const now = next.address;
      if (now === last) return;
      const previous = last;
      last = now;
      if (previous) cb(now);
    });
  }
  const p = injected();
  if (!p || !p.on) return () => {};
  const handler = (accs) => cb(accs && accs.length ? accs[0].toLowerCase() : null);
  p.on('accountsChanged', handler);
  return () => p.removeListener && p.removeListener('accountsChanged', handler);
}

export function onChainChanged(cb) {
  // Privy is pinned to X Layer by supportedChains, so there is no chain to
  // change to and no event to subscribe to.
  if (privyEnabled()) return () => {};
  const p = injected();
  if (!p || !p.on) return () => {};
  const handler = (cid) => cb(parseInt(cid, 16));
  p.on('chainChanged', handler);
  return () => p.removeListener && p.removeListener('chainChanged', handler);
}

export const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '');
export const txUrl = (h) => `${EXPLORER}/tx/${h}`;
