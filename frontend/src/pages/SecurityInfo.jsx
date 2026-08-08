import React from 'react';
import { Link } from 'react-router-dom';
import { EXPLORER } from '../wallet.js';

/**
 * The public security page: what stops Sarf moving your funds, stated as
 * mechanism rather than promise. Account controls (passkeys, session keys)
 * live behind sign-in at /settings — this page is what you can read without
 * one.
 */
export default function SecurityInfo() {
  return (
    <section>
      <div className="eyebrow tick">Non-custodial by construction</div>
      <h1>Security</h1>
      <p className="sub">
        Sarf is built so it structurally cannot move your funds. Not a policy
        promise — a set of things that are simply not wired up.
      </p>

      <div className="card green" style={{ marginTop: 30 }}>
        <h3>No keys, ever</h3>
        <p>
          The server never receives, stores, or can reconstruct your private key
          or seed phrase. Signing in proves you control an address; it grants a
          session, not custody. Ending the session revokes it everywhere,
          including any connected AI client.
        </p>
      </div>

      <div className="card accent">
        <h3>Per-action confirmation</h3>
        <p>
          Every state-changing action is signed by you. Sarf can price and build a
          transaction — it cannot execute one. What you approve is shown in full
          before you sign it, including the recipient, in monospace and untruncated.
        </p>
      </div>

      <div className="card accent">
        <h3>Passkeys gate the dangerous actions</h3>
        <p>
          A passkey here is not a second signer — your wallet signature is what
          authorizes funds, always. It proves a person is present for the actions
          where that matters: sending funds to someone else, and executing an
          unusually large order from a chat session. Transfers require a fresh
          passkey every time, regardless of amount.
        </p>
      </div>

      <div className="card">
        <h3>Session keys are capped in the contract</h3>
        <p>
          If you opt into trading inside a chat, the per-trade and daily limits are
          enforced by the deployed contract, not by Sarf's server — no
          configuration change on our side can raise them. Transfers are excluded
          from that path entirely: a session key can trade, and can never move
          funds to another address.
        </p>
      </div>

      <div className="card">
        <h3>Read-only by default</h3>
        <p>
          Portfolio analysis works from a pasted address alone — no connection, no
          permissions granted, and nothing to revoke later if you were only
          looking.
        </p>
      </div>

      <div className="section-label">Verify it yourself</div>
      <p className="sub" style={{ marginTop: 6 }}>
        Every executed trade returns an X Layer transaction hash. Check it on the{' '}
        <a href={EXPLORER} target="_blank" rel="noreferrer">block explorer</a>{' '}
        rather than taking our word for it — that is the difference between a
        settlement and a screenshot.
      </p>

      <div className="connect-cta">
        <p>Already signed in? Passkeys and session-key limits are in your settings.</p>
        <Link className="cta-btn" to="/settings">Open settings</Link>
      </div>
    </section>
  );
}
