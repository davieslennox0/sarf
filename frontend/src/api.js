// Thin API client. Session token lives in sessionStorage only (cleared on
// tab close and by "End session"); it is a server-minted bearer token, never
// key material.

const TOKEN_KEY = 'sarf.session';

export function getSession() {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    if (Date.now() > s.expiresAt) {
      sessionStorage.removeItem(TOKEN_KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

export function setSession(token, address, expiresInSeconds, mcpUrl) {
  sessionStorage.setItem(
    TOKEN_KEY,
    JSON.stringify({ token, address, expiresAt: Date.now() + expiresInSeconds * 1000, mcpUrl }),
  );
}

export function clearSession() {
  sessionStorage.removeItem(TOKEN_KEY);
}

// Challenge → wallet personal-message signature → server verification →
// short-lived session token. The server verifies the signature with the Sui
// SDK (full zkLogin proof checks for zkLogin wallets) before minting; every
// API call and MCP tool call is bound to this session's address. Re-runs
// automatically once the token expires — expiry means signing in again, by
// design (no silent renewal).
export async function ensureSession(address, signMessage) {
  const existing = getSession();
  if (existing && existing.address === address) return existing;
  // Discarding a token (e.g. the wallet address changed) must void it
  // server-side too — a token we abandon is still a live MCP credential.
  if (existing) api.logout(existing.token);
  clearSession();
  const { message } = await api.authChallenge(address);
  const res = await signMessage({ message: new TextEncoder().encode(message) });
  const out = await api.authVerify(address, res.signature);
  setSession(out.token, out.address, out.expires_in, out.mcp_url);
  return getSession();
}

async function req(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const s = getSession();
  if (s && !headers.Authorization) headers.Authorization = `Bearer ${s.token}`;
  const r = await fetch(path, { ...opts, headers });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || body.error || `HTTP ${r.status}`);
  return body;
}

export const api = {
  stats: () => req('/api/stats'),
  proposal: (id) => req(`/api/proposal/${encodeURIComponent(id)}`),
  submit: (proposalId, bytesB64, signatures) =>
    req('/api/submit', {
      method: 'POST',
      body: JSON.stringify({
        proposal_id: proposalId,
        signed_tx_bytes_base64: bytesB64,
        signatures,
      }),
    }),
  authChallenge: (address) => req(`/api/auth/challenge?address=${address}`),
  authVerify: (address, signature) =>
    req('/api/auth/verify', { method: 'POST', body: JSON.stringify({ address, signature }) }),
  // Token passed explicitly: callers revoke while (or after) clearing local
  // state, so logout must not depend on the token still being in storage.
  logout: (token) =>
    req('/api/auth/logout', {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    }).catch(() => {}),
  myActivity: () => req('/api/me/activity'),
};
