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
        <h3>Your passkey gates every transaction</h3>
        <p>
          You register it once when you sign in, and after that it guards both
          ends: a session key is only issued after you prove it is you, and no
          transaction that key makes goes through without it. One verification
          covers a session rather than a single order — that is what lets you
          approve trades in chat instead of returning to your wallet each time.
        </p>
        <p>
          Being straight about the trade: this is not the same as signing every
          trade in your wallet. Within a session, your passkey — not a wallet
          signature — is what authorizes a trade. That is why the session key is
          capped per trade and per day <em>in the contract</em>, and why it
          expires. Those limits are the ceiling on what one verification can
          ever authorize, and Sarf cannot raise them. Transfers are outside this
          entirely: they always need a fresh passkey, they can never be
          delegated to a session key, and no amount is small enough to skip it.
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
