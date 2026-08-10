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
import { ensureSession } from './api.js';
import { connect } from './wallet.js';

const STEPS = {
  idle: null,
  connecting: 'Opening sign-in…',
  signing: 'Confirming your wallet…',
  checking: 'Almost there…',
};

export default function Onboarding({ onDone }) {
  const [step, setStep] = useState('idle');
  const [err, setErr] = useState(null);
  const busy = step !== 'idle';

  const start = async () => {
    setErr(null);
    try {
      setStep('connecting');
      const address = await connect();

      setStep('signing');
      await ensureSession(address);

      setStep('idle');

      // Registration is NOT handled here any more. App.jsx checks for a
      // passkey above every authenticated route and blocks until one exists,
      // which covers the five pages that mint sessions on their own and never
      // came through this component. Prompting in both places would show the
      // modal twice.
      onDone?.();
    } catch (e) {
      setStep('idle');
      setErr(e.message || String(e));
    }
  };

  return (
    <>
      <button className="primary" disabled={busy} onClick={start}>
        {busy ? STEPS[step] : 'Connect'}
      </button>
      {err && <span className="error"> {err}</span>}
    </>
  );
}
