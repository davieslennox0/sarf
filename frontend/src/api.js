/**
 * API client. The session token is the same credential the MCP connector
 * uses, so it lives in sessionStorage only — never localStorage, never a
 * cookie — and dies with the tab.
 */

import { signMessage } from './wallet.js';

const KEY = 'sarf.session';

export function getSession() {
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

export function setSession(s) {
  sessionStorage.setItem(KEY, JSON.stringify(s));
}

export function clearSession() {
  sessionStorage.removeItem(KEY);
}

async function req(path, opts = {}) {
  const s = getSession();
  const headers = { 'content-type': 'application/json', ...(opts.headers || {}) };
  if (s?.token) headers.authorization = `Bearer ${s.token}`;
  const res = await fetch(path, { ...opts, headers });
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { detail: text };
  }
  if (!res.ok) {
    if (res.status === 401) clearSession();
    throw new Error(body.detail || body.message || `Request failed (${res.status})`);
  }
  return body;
}

export const api = {
  health: () => req('/healthz'),
  stats: () => req('/api/stats'),
  list: () => req('/api/rwa/list'),
  price: (symbol) => req(`/api/rwa/price/${encodeURIComponent(symbol)}`),

  challenge: (address) => req(`/api/auth/challenge?address=${encodeURIComponent(address)}`),
  verify: (address, signature) =>
    req('/api/auth/verify', { method: 'POST', body: JSON.stringify({ address, signature }) }),
  logout: () => req('/api/auth/logout', { method: 'POST' }),

  order: (id) => req(`/api/order/${encodeURIComponent(id)}`),
  orderStatus: (id) => req(`/api/order/${encodeURIComponent(id)}/status`),
  orderSubmitted: (id, txHash) =>
    req(`/api/order/${encodeURIComponent(id)}/submitted`, {
      method: 'POST',
      body: JSON.stringify({ tx_hash: txHash }),
    }),
  myOrders: () => req('/api/me/orders'),
  portfolio: () => req('/api/me/portfolio'),

  grant: () => req('/api/grant'),
  grantPrepare: (body) =>
    req('/api/grant/prepare', { method: 'POST', body: JSON.stringify(body) }),
  grantRevoke: () => req('/api/grant/revoke', { method: 'POST' }),

  passkeyStatus: () => req('/api/passkey/status'),
  passkeyRegisterOptions: () => req('/api/passkey/register/options', { method: 'POST' }),
  passkeyRegisterVerify: (cred) =>
    req('/api/passkey/register/verify', { method: 'POST', body: JSON.stringify(cred) }),
  passkeyAuthOptions: () => req('/api/passkey/auth/options', { method: 'POST' }),
  passkeyAuthVerify: (cred) =>
    req('/api/passkey/auth/verify', { method: 'POST', body: JSON.stringify(cred) }),
};

/**
 * Establish a session if there isn't a live one for this address.
 * Costs one wallet signature over a server nonce; authorizes no transaction.
 */
export async function ensureSession(address) {
  const s = getSession();
  if (s && s.address?.toLowerCase() === address.toLowerCase()) return s;
  const { message } = await api.challenge(address);
  const signature = await signMessage(address, message);
  const out = await api.verify(address, signature);
  const session = {
    token: out.token,
    address: out.address,
    expiresAt: Date.now() + out.expires_in * 1000,
    mcpUrl: out.mcp_url,
    hasPasskey: out.has_passkey,
  };
  setSession(session);
  return session;
}

// --- WebAuthn helpers -------------------------------------------------------
// The browser API speaks ArrayBuffers; the server speaks base64url. These two
// functions are the whole translation layer.

const b64uToBuf = (s) => {
  const pad = s.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(pad + '==='.slice((pad.length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
};

const bufToB64u = (b) =>
  btoa(String.fromCharCode(...new Uint8Array(b)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=/g, '');

export async function registerPasskey() {
  const opts = await api.passkeyRegisterOptions();
  const cred = await navigator.credentials.create({
    publicKey: {
      ...opts,
      challenge: b64uToBuf(opts.challenge),
      user: { ...opts.user, id: b64uToBuf(opts.user.id) },
      excludeCredentials: (opts.excludeCredentials || []).map((c) => ({
        ...c,
        id: b64uToBuf(c.id),
      })),
    },
  });
  return api.passkeyRegisterVerify({
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64u(cred.response.clientDataJSON),
      attestationObject: bufToB64u(cred.response.attestationObject),
    },
  });
}

export async function verifyPasskey() {
  const opts = await api.passkeyAuthOptions();
  const cred = await navigator.credentials.get({
    publicKey: {
      ...opts,
      challenge: b64uToBuf(opts.challenge),
      allowCredentials: (opts.allowCredentials || []).map((c) => ({
        ...c,
        id: b64uToBuf(c.id),
      })),
    },
  });
  return api.passkeyAuthVerify({
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64u(cred.response.clientDataJSON),
      authenticatorData: bufToB64u(cred.response.authenticatorData),
      signature: bufToB64u(cred.response.signature),
      userHandle: cred.response.userHandle ? bufToB64u(cred.response.userHandle) : null,
    },
  });
}
