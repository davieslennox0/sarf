/**
 * X Layer wallet access over EIP-1193 — OKX Wallet first, any injected
 * provider as fallback.
 *
 * Deliberately no wagmi/viem: the whole surface we need is connect, sign a
 * login message, switch chain, and send one transaction whose calldata the
 * server already built. A connector framework would add weight without
 * removing a decision, and OKX Wallet injects a standard provider.
 *
 * No key material passes through here. The provider signs; we only ever hold
 * an address, a signature, and a transaction hash.
 */

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

export function provider() {
  if (typeof window === 'undefined') return null;
  // Prefer the OKX injection: on a machine with several wallets, window.ethereum
  // is whichever one won the race, which is not necessarily the one the user
  // expects for an OKX-ecosystem chain.
  return window.okxwallet || window.ethereum || null;
}

export function hasWallet() {
  return Boolean(provider());
}

export async function connect() {
  const p = provider();
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
  const p = provider();
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

export function onAccountsChanged(cb) {
  const p = provider();
  if (!p || !p.on) return () => {};
  const handler = (accs) => cb(accs && accs.length ? accs[0].toLowerCase() : null);
  p.on('accountsChanged', handler);
  return () => p.removeListener && p.removeListener('accountsChanged', handler);
}

export function onChainChanged(cb) {
  const p = provider();
  if (!p || !p.on) return () => {};
  const handler = (cid) => cb(parseInt(cid, 16));
  p.on('chainChanged', handler);
  return () => p.removeListener && p.removeListener('chainChanged', handler);
}

export const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '');
export const txUrl = (h) => `${EXPLORER}/tx/${h}`;
