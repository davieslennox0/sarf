import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useCurrentAccount, useSignPersonalMessage } from '@mysten/dapp-kit';
import { api, ensureSession, getSession } from '../api.js';

// Authenticated view of the proposal audit trail. Sign-in = wallet signs a
// server nonce (personal message, explicitly "authorizes no transaction");
// the server verifies the signature (zkLogin signatures included) and mints
// a bearer session. Keys never leave the wallet.

const CHIP = {
  proposed: ['chip amber', 'awaiting signature'],
  submitted: ['chip green', 'broadcast ✓'],
  failed: ['chip red', 'broadcast failed'],
  simulation_failed: ['chip red', 'simulation failed'],
  rejected: ['chip red', 'rejected (byte mismatch)'],
  expired: ['chip gray', 'expired unsigned'],
};

function statusChip(p) {
  let key = p.status;
  if (key === 'proposed' && p.expires_at * 1000 < Date.now()) key = 'expired';
  const [cls, label] = CHIP[key] ?? ['chip gray', key];
  return <span className={cls}>{label}</span>;
}

// The session token doubles as the Claude connector credential. Shown only
// after wallet sign-in; goes stale with the session (~30 min) — that is the
// point: a pasted/leaked connector URL cannot outlive the session, and
// renewing it always costs a fresh wallet signature.
function McpConnectCard() {
  const [now, setNow] = useState(Date.now());
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const s = getSession();
  if (!s?.mcpUrl) return null;
  const msLeft = s.expiresAt - now;
  if (msLeft <= 0) return null;
  const copy = async () => {
    await navigator.clipboard.writeText(s.mcpUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="connect-card">
      <div className="row-title">Connect Claude to this account</div>
      <p className="muted small">
        Add this as a custom connector URL in Claude (Settings → Connectors). It is bound to your
        verified address and expires with this session in{' '}
        <b>{Math.floor(msLeft / 60000)}m {String(Math.floor((msLeft % 60000) / 1000)).padStart(2, '0')}s</b>
        — after that, sign in again for a fresh one.
      </p>
      <div className="token-row">
        <code className="token-url">{s.mcpUrl}</code>
        <button onClick={copy}>{copied ? 'Copied ✓' : 'Copy'}</button>
      </div>
    </div>
  );
}

export default function Activity() {
  const account = useCurrentAccount();
  const { mutateAsync: signMessage } = useSignPersonalMessage();
  const [authed, setAuthed] = useState(Boolean(getSession()));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  // Sessions are short-lived (server-enforced); when one lapses, drop back
  // to the sign-in state instead of showing stale data behind a dead token.
  useEffect(() => {
    const t = setInterval(() => {
      if (authed && !getSession()) setAuthed(false);
    }, 5000);
    return () => clearInterval(t);
  }, [authed]);

  const { data, refetch } = useQuery({
    queryKey: ['activity'],
    queryFn: api.myActivity,
    enabled: authed,
    retry: false,
  });

  const signIn = async () => {
    setErr(null);
    setBusy(true);
    try {
      await ensureSession(account.address, signMessage);
      setAuthed(true);
      refetch();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (!account) {
    return (
      <section>
        <h1>My activity</h1>
        <p className="muted">Connect your wallet (top right) to view your proposal history.</p>
      </section>
    );
  }

  if (!authed) {
    return (
      <section>
        <h1>My activity</h1>
        <p className="muted">
          Prove ownership of {account.address.slice(0, 10)}… by signing a one-time message. The
          message authorizes nothing on-chain.
        </p>
        <button className="primary" disabled={busy} onClick={signIn}>
          {busy ? 'Waiting for wallet…' : 'Sign in with wallet'}
        </button>
        {err && <div className="error">{err}</div>}
      </section>
    );
  }

  const rows = data?.proposals ?? [];
  return (
    <section>
      <h1>My activity</h1>
      <McpConnectCard />
      <p className="muted">Every proposal Sarf built for this address, and what became of it.</p>
      {rows.length === 0 && <p className="muted">No proposals yet.</p>}
      <div className="rows">
        {rows.map((p) => (
          <div className="row" key={p.proposal_id}>
            <div>
              <div className="row-title">{p.summary ?? p.tool}</div>
              <div className="row-sub">
                {new Date(p.created_at * 1000).toLocaleString()} · {p.tool} ·{' '}
                <code>{p.proposal_id.slice(0, 14)}…</code>
              </div>
            </div>
            <div className="row-right">
              {statusChip(p)}
              {p.tx_digest && (
                <a
                  href={`https://suivision.xyz/txblock/${p.tx_digest}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  view tx ↗
                </a>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
