import React, { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { connect, currentAccount, short, signMessage } from '../wallet.js';

/**
 * OAuth consent. This is a functional requirement, not marketing: Claude and
 * ChatGPT land here to link a wallet, and without it there is no way to
 * authenticate an MCP session.
 *
 * The approval is one wallet signature over a server nonce. It authorizes no
 * transaction and moves no funds — the page says so plainly, because a wallet
 * prompt on a trading site otherwise looks like a trade.
 */
export default function Authorize() {
  const [params] = useSearchParams();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [address, setAddress] = useState(null);

  React.useEffect(() => { currentAccount().then(setAddress).catch(() => {}); }, []);

  const clientId = params.get('client_id');
  const redirectUri = params.get('redirect_uri');
  const codeChallenge = params.get('code_challenge');
  const state = params.get('state');

  if (!clientId || !redirectUri || !codeChallenge) {
    return (
      <section>
        <h1>Authorize</h1>
        <p className="error">
          This link is missing OAuth parameters. Start from your AI client's
          “Add connector” flow rather than opening this page directly.
        </p>
      </section>
    );
  }

  const approve = async () => {
    setBusy(true); setErr(null);
    try {
      const addr = address || (await connect());
      setAddress(addr);
      const { message } = await api.challenge(addr);
      const signature = await signMessage(addr, message);
      const res = await fetch('/api/oauth/approve', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          address: addr, signature, client_id: clientId,
          redirect_uri: redirectUri, code_challenge: codeChallenge, state,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'authorization failed');
      window.location.href = body.redirect;
    } catch (e) {
      setErr(e.message || String(e));
      setBusy(false);
    }
  };

  let host = redirectUri;
  try { host = new URL(redirectUri).host; } catch { /* show it raw */ }

  return (
    <section className="sign-card">
      <h1>Connect your wallet to Sarf</h1>
      <p>
        <b>{host}</b> is asking to connect to Sarf on your behalf, so your AI
        assistant can read your X Layer positions and prepare trades for you.
      </p>

      <div className="kv">
        <div><span>Wallet</span><b>{address ? short(address) : 'not connected'}</b></div>
        <div><span>Redirect</span><b>{host}</b></div>
        <div><span>Network</span><b>X Layer (196)</b></div>
      </div>

      <div className="risk">
        <div className="risk-title">What you are approving</div>
        <ul>
          <li>A signature proving you control this address. It <b>authorizes no transaction and moves no funds.</b></li>
          <li>The assistant may read your holdings and <b>build</b> trades — every trade still needs a separate signature from you.</li>
          <li>The session expires on its own, and <b>End session</b> here revokes it everywhere, including the connector.</li>
        </ul>
      </div>

      <div className="cta">
        <button className="primary big" onClick={approve} disabled={busy}>
          {busy ? 'Check your wallet…' : address ? 'Approve with wallet signature' : 'Connect wallet & approve'}
        </button>
      </div>
      {err && <p className="error">{err}</p>}
    </section>
  );
}
