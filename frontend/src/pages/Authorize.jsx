import React, { useMemo, useState } from 'react';
import { useCurrentAccount, useSignPersonalMessage } from '@mysten/dapp-kit';
import { api } from '../api.js';

// OAuth consent page: an MCP client (Claude) sent the user here from
// GET /authorize. Approving = the same one-time wallet signature used for
// dashboard sign-in — it proves address ownership and authorizes no
// transaction. On success we hand the client back an authorization code;
// it exchanges that for a 30-minute session token out of band. Keys never
// leave the wallet; this page never sees a token at all.

export default function Authorize() {
  const account = useCurrentAccount();
  const { mutateAsync: signMessage } = useSignPersonalMessage();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const params = useMemo(() => Object.fromEntries(new URLSearchParams(window.location.search)), []);
  const clientName = params.client_name || 'An MCP client';
  const valid = params.client_id && params.redirect_uri && params.code_challenge;

  const deny = () => {
    const q = new URLSearchParams({ error: 'access_denied' });
    if (params.state) q.set('state', params.state);
    window.location.replace(`${params.redirect_uri}${params.redirect_uri.includes('?') ? '&' : '?'}${q}`);
  };

  const approve = async () => {
    setErr(null);
    setBusy(true);
    try {
      const { message } = await api.authChallenge(account.address);
      const res = await signMessage({ message: new TextEncoder().encode(message) });
      const out = await api.oauthApprove({
        address: account.address,
        signature: res.signature,
        client_id: params.client_id,
        redirect_uri: params.redirect_uri,
        code_challenge: params.code_challenge,
        state: params.state,
      });
      window.location.replace(out.redirect);
    } catch (e) {
      setErr(e.message);
      setBusy(false);
    }
  };

  if (!valid) {
    return (
      <section>
        <h1>Connect to Sarf</h1>
        <p className="error">Malformed authorization request — missing OAuth parameters.</p>
      </section>
    );
  }

  return (
    <section>
      <h1>Connect to Sarf</h1>
      <p className="muted">
        <b>{clientName}</b> is asking to use Sarf with your wallet. Approving signs a one-time
        message that proves address ownership — it authorizes <b>no transaction</b> and shares no
        keys. The connection lasts 30 minutes and only ever reads your positions and builds
        unsigned proposals; anything that moves funds still requires your wallet signature on the
        specific transaction.
      </p>
      {!account ? (
        <p className="muted">Connect your wallet (top right) to continue.</p>
      ) : (
        <>
          <p className="muted small">
            Connecting as <code>{account.address.slice(0, 12)}…{account.address.slice(-6)}</code>
          </p>
          <div className="token-row">
            <button className="primary" disabled={busy} onClick={approve}>
              {busy ? 'Waiting for wallet…' : 'Approve with wallet signature'}
            </button>
            <button disabled={busy} onClick={deny}>Deny</button>
          </div>
        </>
      )}
      {err && <div className="error">{err}</div>}
    </section>
  );
}
