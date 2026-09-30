/**
 * Earn: the same zap product against a DIFFERENT backend.
 *
 * Earn runs as its own pm2 app on its own port with its own database, so
 * nothing it does can reach the positions the main Sarf app is watching. That
 * isolation is the whole point, and it has one honest consequence: sessions
 * live in the server's database, so an Earn session is a separate credential
 * and needs its own wallet signature. It is kept under its own storage key so
 * the two never overwrite each other.
 *
 * Everything else is deliberately the same shape as api.js, because the pages
 * are shared: whichever client a page is handed, it calls the same methods.
 */

import { signMessage } from './wallet.js';

// Caddy strips this prefix and proxies to the Earn app; the server itself
// serves the same /api/... paths as Sarf.
const BASE = '/earn-api';
const KEY = 'earn.session';

export function getEarnSession() {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    if (!s.expiresAt || s.expiresAt < Date.now()) {
      sessionStorage.removeItem(KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

export function setEarnSession(s) {
  sessionStorage.setItem(KEY, JSON.stringify(s));
}

export function clearEarnSession() {
  sessionStorage.removeItem(KEY);
}

async function req(path, opts = {}) {
  const s = getEarnSession();
  const headers = { 'content-type': 'application/json', ...(opts.headers || {}) };
  if (s?.token) headers.authorization = `Bearer ${s.token}`;
  const res = await fetch(BASE + path, { ...opts, headers });
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { detail: text };
  }
  if (!res.ok) {
    if (res.status === 401) clearEarnSession();
    throw new Error(body.detail || body.message || `Request failed (${res.status})`);
  }
  return body;
}

/** One wallet signature over an Earn-issued nonce. Authorises no transaction. */
export async function ensureEarnSession(address) {
  const s = getEarnSession();
  if (s && s.address?.toLowerCase() === address.toLowerCase()) return s;
  const { message } = await req(`/api/auth/challenge?address=${encodeURIComponent(address)}`);
  const signature = await signMessage(address, message);
  const out = await req('/api/auth/verify', {
    method: 'POST', body: JSON.stringify({ address, signature }),
  });
  const session = {
    token: out.token,
    address: out.address,
    expiresAt: Date.now() + out.expires_in * 1000,
  };
  setEarnSession(session);
  return session;
}

export const earnApi = {
  zapPools: () => req('/api/zap/pools'),
  // The vault-list surface: one row per asset, not per pool. Read-only and
  // not gated behind the deposit flag — see the server route's own comment.
  earnVaults: () => req('/api/zap/earn/vaults'),
  earnVault: (symbol) => req(`/api/zap/earn/vaults/${encodeURIComponent(symbol)}`),
  zapPosition: (id) => req(`/api/zap/position/${encodeURIComponent(id)}`),
  zapMine: () => req('/api/zap/positions'),
  zapDeposit: (body) => req('/api/zap/deposit', { method: 'POST', body: JSON.stringify(body) }),
  zapThreshold: (id, body) =>
    req(`/api/zap/${encodeURIComponent(id)}/threshold`, { method: 'POST', body: JSON.stringify(body) }),
  zapStep: (id) => req(`/api/zap/${encodeURIComponent(id)}/step`, { method: 'POST' }),
  zapStepSubmitted: (id, txHash) =>
    req(`/api/zap/${encodeURIComponent(id)}/step/submitted`, {
      method: 'POST', body: JSON.stringify({ tx_hash: txHash }),
    }),
  zapExit: (id) => req(`/api/zap/${encodeURIComponent(id)}/exit`, { method: 'POST' }),
  zapReenter: (id) => req(`/api/zap/${encodeURIComponent(id)}/reenter`, { method: 'POST' }),
  zapClose: (id) => req(`/api/zap/${encodeURIComponent(id)}/close`, { method: 'POST' }),
  health: () => req('/healthz'),
};
