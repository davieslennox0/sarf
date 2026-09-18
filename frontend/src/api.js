/**
 * API client. The session token is the same credential the MCP connector
 * uses, so it lives in sessionStorage only — never localStorage, never a
 * cookie — and dies with the tab.
 */

import { signMessage } from './wallet.js';
import { identityToken } from './privy.jsx';

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

/**
 * `req` plus the Privy identity token.
 *
 * The token is fetched per call rather than cached, because Privy rotates it
 * and a stale copy would turn the console into an intermittent 403 that looks
 * like the allow-list is wrong. It is cheap — Privy reads it from its own
 * cookie, no network hop.
 *
 * A missing token is NOT short-circuited here. Letting the request go and the
 * server refuse keeps one place deciding who is an admin; deciding it twice,
 * once in a browser, is how the two versions drift apart.
 */
async function adminReq(path, opts = {}) {
  const id = await identityToken();
  return req(path, {
    ...opts,
    headers: { ...(opts.headers || {}), ...(id ? { 'x-privy-id-token': id } : {}) },
  });
}

export const api = {
  health: () => req('/healthz'),
  stats: () => req('/api/stats'),
  list: () => req('/api/rwa/list'),
  price: (symbol) => req(`/api/rwa/price/${encodeURIComponent(symbol)}`),
  // Every visible row in one request, answered from the server's warm price
  // cache. The list pages used to fetch a row at a time — one connection per
  // asset, all of them queued behind the aggregator's rate limiter — which is
  // why a market list sat on dashes for seconds before it filled in.
  prices: (symbols) =>
    req(`/api/rwa/prices?symbols=${encodeURIComponent(symbols.join(','))}`),

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
  // Read-only, no session. Carries the analysis alongside the holdings so the
  // page renders both from one round trip.
  publicPortfolio: (address) => req(`/api/portfolio/${encodeURIComponent(address)}`),

  transferPrepare: (body) =>
    req('/api/transfer/prepare', { method: 'POST', body: JSON.stringify(body) }),

  // What is holding a live session for this wallet — the assistants and
  // this browser. Read by the dashboard's Agents section.
  connections: () => req('/api/connections'),
  // Deposits: dollars in, by burn and mint. The quote comes from Circle, the
  // transactions are built server-side and signed by the user on Base.
  depositQuote: (amount) => req(`/api/deposit/quote?amount=${encodeURIComponent(amount)}`),
  depositAllowance: (amount) =>
    req(`/api/deposit/allowance?amount=${encodeURIComponent(amount)}`),
  depositPrepare: (amount, fast = true) =>
    req('/api/deposit/prepare', { method: 'POST', body: JSON.stringify({ amount, fast }) }),
  // Make sure the wallet can afford to send its own burn. An on-ramp delivers
  // USDC and no ETH, so a freshly funded wallet cannot pay Base gas; the
  // relayer covers the shortfall into the user's own account. Never fatal —
  // see the call site in Deposit.jsx.
  depositGas: (needsApproval = true) =>
    req('/api/deposit/gas', {
      method: 'POST',
      body: JSON.stringify({ needs_approval: needsApproval }),
    }),
  // Told to the server the moment the burn is broadcast, so that finishing the
  // deposit never depends on this tab still being open. The server's sweeper
  // mints anything left pending.
  depositRecord: (txHash, amountUsd) =>
    req('/api/deposit/record', {
      method: 'POST',
      body: JSON.stringify({ tx_hash: txHash, amount_usd: amountUsd }),
    }),
  depositList: () => req('/api/deposit/list'),
  depositComplete: (txHash, amountUsd) =>
    req('/api/deposit/complete', {
      method: 'POST',
      body: JSON.stringify({ tx_hash: txHash, amount_usd: amountUsd }),
    }),

  grant: () => req('/api/grant'),
  grantPrepare: (body) =>
    req('/api/grant/prepare', { method: 'POST', body: JSON.stringify(body) }),
  // Fallback for wallets that sign a 7702 authorization but cannot broadcast
  // the type-4 transaction carrying it (Privy's embedded wallet). The relayer
  // pays gas and presses send; the authorization is still the user's.
  grantRelay: (body) =>
    req('/api/grant/relay', { method: 'POST', body: JSON.stringify(body) }),
  grantRevoke: () => req('/api/grant/revoke', { method: 'POST' }),

  // Single-asset zap. The same endpoints behind the MCP zap tools, so the page
  // and the chat never compute a split or an IL figure differently.
  zapPools: () => req('/api/zap/pools'),
  zapPosition: (id) => req(`/api/zap/position/${encodeURIComponent(id)}`),
  zapMine: () => req('/api/zap/positions'),
  zapDeposit: (body) => req('/api/zap/deposit', { method: 'POST', body: JSON.stringify(body) }),
  zapThreshold: (id, body) =>
    req(`/api/zap/${encodeURIComponent(id)}/threshold`, { method: 'POST', body: JSON.stringify(body) }),
  zapExit: (id) => req(`/api/zap/${encodeURIComponent(id)}/exit`, { method: 'POST' }),
  zapReenter: (id) => req(`/api/zap/${encodeURIComponent(id)}/reenter`, { method: 'POST' }),
  zapCancel: (id) => req(`/api/zap/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  zapStep: (id) => req(`/api/zap/${encodeURIComponent(id)}/step`, { method: 'POST' }),
  zapStepSubmitted: (id, txHash) =>
    req(`/api/zap/${encodeURIComponent(id)}/step/submitted`, {
      method: 'POST', body: JSON.stringify({ tx_hash: txHash }),
    }),

  passkeyStatus: () => req('/api/passkey/status'),
  passkeyRegisterOptions: () => req('/api/passkey/register/options', { method: 'POST' }),
  passkeyRegisterVerify: (cred) =>
    req('/api/passkey/register/verify', { method: 'POST', body: JSON.stringify(cred) }),
  passkeyAuthOptions: () => req('/api/passkey/auth/options', { method: 'POST' }),
  passkeyAuthVerify: (cred) =>
    req('/api/passkey/auth/verify', { method: 'POST', body: JSON.stringify(cred) }),

  // --- admin console ---------------------------------------------------
  //
  // Two credentials, both required by the server (see server/sarf/admin.py):
  // the ordinary session bearer that `req` already attaches, plus the Privy
  // identity token, which is the half that says WHICH HUMAN this is rather
  // than which wallet. Sent through `adminReq` rather than `req` so it rides
  // only on these calls — the identity token has no business on a quote or a
  // portfolio read, and a header added globally is a header that ends up
  // everywhere.
  //
  // Every one of these 403s for anyone not on SARF_ADMIN_EMAILS. The one
  // exception is adminWhoami, which answers `{is_admin: false}` instead, so
  // the ordinary user's page load does not have to treat a refusal as an
  // error.
  adminWhoami: () => adminReq('/api/admin/whoami'),
  adminOverview: () => adminReq('/api/admin/overview'),
  adminUsers: (q, limit = 50) =>
    adminReq(`/api/admin/users?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ''}`),
  adminOrders: (limit = 50) => adminReq(`/api/admin/orders?limit=${limit}`),
  adminDeposits: (limit = 50) => adminReq(`/api/admin/deposits?limit=${limit}`),
  adminGrants: (limit = 50) => adminReq(`/api/admin/grants?limit=${limit}`),
  adminAudit: (limit = 100) => adminReq(`/api/admin/audit?limit=${limit}`),
  adminRevokeSessions: (address) =>
    adminReq('/api/admin/sessions/revoke', {
      method: 'POST', body: JSON.stringify({ address }),
    }),
  adminRevokeGrant: (address) =>
    adminReq('/api/admin/grants/revoke', {
      method: 'POST', body: JSON.stringify({ address }),
    }),
  adminRetryDeposit: (burnTx) =>
    adminReq('/api/admin/deposits/retry', {
      method: 'POST', body: JSON.stringify({ burn_tx: burnTx }),
    }),
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
