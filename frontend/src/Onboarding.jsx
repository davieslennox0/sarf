/**
 * Sign-in, end to end, in one place.
 *
 *   Google  ->  embedded wallet  ->  silent personal_sign  ->  Sarf session
 *                                                          ->  passkey  ->  in
 *
 * The signature step looks skippable and is not. Sarf's session token is minted
 * only after an EIP-191 signature over a server nonce, and the passkey
 * registration endpoint is itself session-authenticated — so the passkey modal
 * cannot open before it. On an embedded wallet that signature raises no prompt,
 * which is what lets the whole chain read as a single "log in".
 *
 * The passkey registered here is Sarf's own WebAuthn credential, not Privy's.
 * It is what gates transfers and large orders, including on the MCP path where
 * Claude executes an order and no browser session exists at all. See privy.jsx.
 */

import React, { useState } from 'react';
import { api, ensureSession, registerPasskey } from './api.js';
import { connect } from './wallet.js';

const STEPS = {
  idle: null,
  connecting: 'Opening sign-in…',
  signing: 'Confirming your wallet…',
  checking: 'Almost there…',
};

export default function Onboarding({ onDone }) {
  const [step, setStep] = useState('idle');
  const [needsPasskey, setNeedsPasskey] = useState(false);
  const [err, setErr] = useState(null);
  const busy = step !== 'idle';

  const start = async () => {
    setErr(null);
    try {
      setStep('connecting');
      const address = await connect();

      setStep('signing');
      await ensureSession(address);

      setStep('checking');
      const pk = await api.passkeyStatus();
      setStep('idle');

      // First-time users register right here rather than being sent to a
      // settings page they would never visit — the gate has to exist before
      // the first transfer, not after someone discovers they cannot send.
      if (!pk?.registered) setNeedsPasskey(true);
      else onDone?.();
    } catch (e) {
      setStep('idle');
      setErr(e.message || String(e));
    }
  };

  const addPasskey = async () => {
    setErr(null);
    setStep('checking');
    try {
      await registerPasskey();
      setStep('idle');
      setNeedsPasskey(false);
      onDone?.();
    } catch (e) {
      setStep('idle');
      setErr(e.message || String(e));
    }
  };

  // Skipping is allowed, because a passkey prompt that cannot be dismissed is a
  // dead end on any device where the ceremony fails. Sending stays locked until
  // one exists, and /security offers it again.
  const skip = () => { setNeedsPasskey(false); onDone?.(); };

  if (needsPasskey) {
    return (
      <div className="modal-backdrop">
        <div className="modal">
          <h2>Add a passkey</h2>
          <p className="muted small">
            One touch of Face ID, Touch ID, or your device PIN. It is what proves
            a person is present before Sarf sends funds to someone else, or
            executes an unusually large order from chat.
          </p>
          <p className="muted small">
            Your passkey never leaves your device, and it is not your wallet key
            — it approves actions, it cannot sign transactions on its own.
          </p>
          {err && <p className="error">{err}</p>}
          <div className="cta">
            <button className="primary" disabled={busy} onClick={addPasskey}>
              {busy ? 'Waiting for your device…' : 'Add passkey'}
            </button>
            <button disabled={busy} onClick={skip}>Later</button>
          </div>
          <p className="muted small">
            Skipping leaves sending disabled until you add one.
          </p>
        </div>
      </div>
    );
  }

  return (
    <>
      <button className="primary" disabled={busy} onClick={start}>
        {busy ? STEPS[step] : 'Log in'}
      </button>
      {err && <span className="error"> {err}</span>}
    </>
  );
}
