import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  ConnectButton,
  useCurrentAccount,
  useSignPersonalMessage,
  useSignTransaction,
} from '@mysten/dapp-kit';
import { api, ensureSession, getSession } from '../api.js';

/**
 * The in-chat signer. Claude links here (sign_url on every proposal); the
 * page shows the SIMULATED outcome — summary, gas, risk notes — and only
 * then lets the user sign, in their own wallet, the exact bytes the server
 * proposed. The signed bytes go back to /api/submit, which refuses anything
 * that doesn't byte-match the stored proposal.
 *
 * This page is self-hosted (not a Claude artifact) because the Artifacts
 * sandbox CSP blocks all external network calls — it could neither fetch the
 * proposal nor reach a wallet/fullnode from inside chat.
 */

function Countdown({ until }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const ms = until * 1000 - now;
  if (ms <= 0) return <span className="chip red">expired</span>;
  return (
    <span className="chip amber">
      expires in {Math.floor(ms / 60000)}m {String(Math.floor((ms % 60000) / 1000)).padStart(2, '0')}s
    </span>
  );
}

export default function Sign() {
  const [params] = useSearchParams();
  const pid = params.get('p');
  const account = useCurrentAccount();
  const { mutateAsync: signTransaction } = useSignTransaction();
  const { mutateAsync: signMessage } = useSignPersonalMessage();
  const [phase, setPhase] = useState('review'); // review | signing | done
  const [result, setResult] = useState(null);
  const [err, setErr] = useState(null);

  const { data: prop, isLoading, error } = useQuery({
    queryKey: ['proposal', pid],
    queryFn: () => api.proposal(pid),
    enabled: Boolean(pid),
    refetchInterval: phase === 'review' ? 30_000 : false,
  });

  if (!pid) return <section><h1>Sign</h1><p className="error">Missing proposal id (?p=sarf_…)</p></section>;
  if (isLoading) return <section><p className="muted">Loading proposal…</p></section>;
  if (error) return <section><p className="error">{error.message}</p></section>;

  const expired = prop.expired || prop.expires_at * 1000 < Date.now();
  const notSignable = prop.status !== 'proposed' || expired;
  const wrongAccount = account && account.address !== prop.user_address;

  const approve = async () => {
    setErr(null);
    setPhase('signing');
    try {
      // Submitting requires an authenticated session for the proposal's
      // address (the server refuses proposals that belong to another
      // account). If none is live, the wallet first asks for a sign-in
      // message — that's the extra prompt before the transaction itself.
      await ensureSession(account.address, signMessage);
      // Sign the exact server-built bytes. The wallet shows its own review UI
      // on top of ours; the server re-checks byte equality on submit.
      const signed = await signTransaction({
        transaction: prop.ptb_base64,
        chain: 'sui:mainnet',
      });
      const out = await api.submit(prop.proposal_id, signed.bytes ?? prop.ptb_base64, [
        signed.signature,
      ]);
      setResult(out);
      setPhase('done');
    } catch (e) {
      setErr(e.message);
      setPhase('review');
    }
  };

  if (phase === 'done' && result) {
    const ok = result.status === 'success';
    return (
      <section>
        <h1>{ok ? 'Transaction broadcast ✓' : 'Broadcast failed'}</h1>
        {result.tx_digest && (
          <p>
            Digest: <code>{result.tx_digest}</code>{' '}
            <a href={`https://suivision.xyz/txblock/${result.tx_digest}`} target="_blank" rel="noreferrer">
              view on explorer ↗
            </a>
          </p>
        )}
        {result.obligation_cap_id && (
          <p className="ok">
            New ObligationOwnerCap: <code>{result.obligation_cap_id}</code> — Sarf will use it for
            your next actions automatically.
          </p>
        )}
        {!ok && <p className="error">{result.error}</p>}
        <p className="muted">You can close this tab and return to the chat.</p>
      </section>
    );
  }

  return (
    <section className="sign-card">
      <h1>Review &amp; sign</h1>

      <div className="summary">{prop.human_summary}</div>

      <div className="kv">
        <div><span>Action</span><b>{prop.tool.replace('propose_', '')}</b></div>
        {Object.entries(prop.params).map(([k, v]) => (
          <div key={k}><span>{k}</span><b>{String(v)}</b></div>
        ))}
        <div>
          <span>Simulated gas</span>
          <b>{prop.simulation ? `${prop.simulation.gasUsedSui.toFixed(4)} SUI` : 'n/a'}</b>
        </div>
        <div>
          <span>Simulation</span>
          <b className={prop.simulation?.status === 'success' ? 'ok' : 'error'}>
            {prop.simulation?.status ?? 'unknown'}
          </b>
        </div>
        <div><span>Validity</span><Countdown until={prop.expires_at} /></div>
      </div>

      {prop.risk_notes?.length > 0 && (
        <div className="risk">
          <div className="risk-title">Risk notes — read before signing</div>
          <ul>
            {prop.risk_notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </div>
      )}

      {notSignable ? (
        <div className="error">
          This proposal is no longer signable ({expired ? 'expired' : prop.status}). Ask the
          assistant for a fresh one — prices and rates move.
        </div>
      ) : !account ? (
        <div className="cta">
          <p className="muted">Connect the wallet that owns {prop.user_address.slice(0, 10)}…</p>
          <ConnectButton connectText="Connect wallet to sign" />
        </div>
      ) : wrongAccount ? (
        <div className="error">
          Connected wallet {account.address.slice(0, 10)}… does not match this proposal&apos;s
          address {prop.user_address.slice(0, 10)}… Switch accounts to sign.
        </div>
      ) : (
        <div className="cta">
          <button className="primary big" disabled={phase === 'signing'} onClick={approve}>
            {phase === 'signing' ? 'Confirm in your wallet…' : 'Sign in wallet & broadcast'}
          </button>
          {!getSession() && (
            <p className="muted small">
              Your wallet will ask for two confirmations: a one-time sign-in message (proves
              address ownership, authorizes nothing), then the transaction itself.
            </p>
          )}
        </div>
      )}

      {err && <div className="error">{err}</div>}

      <p className="muted small">
        Sarf built and simulated this transaction but cannot execute it: signing happens only in
        your wallet, and the server will refuse any bytes that differ from what you see simulated
        here.
      </p>
    </section>
  );
}
