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

export function setSession(token, address, expiresInSeconds) {
  sessionStorage.setItem(
    TOKEN_KEY,
    JSON.stringify({ token, address, expiresAt: Date.now() + expiresInSeconds * 1000 }),
  );
}

export function clearSession() {
  sessionStorage.removeItem(TOKEN_KEY);
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
  logout: () => req('/api/auth/logout', { method: 'POST' }).catch(() => {}),
  myActivity: () => req('/api/me/activity'),
};
