import React from 'react';
import { Link } from 'react-router-dom';

/**
 * How it works. Describes the actual mechanism, including where it stops —
 * the point of the page is that nothing executes without a signature, so the
 * steps are written to be checkable rather than reassuring.
 */
export default function How() {
  return (
    <section>
      <div className="eyebrow tick">Start to finish</div>
      <h1>How Sarf works</h1>
      <p className="sub">
        Four steps. Nothing leaves your wallet until the last one, and that one
        is a signature only you can produce.
      </p>

      <div className="steps" style={{ marginTop: 30 }}>
        <div className="step">
          <div className="step-num">01</div>
          <div className="step-body">
            <h3>Read an address</h3>
            <p>
              Yours, or one you are reviewing. Sarf reads tokenized-stock holdings
              straight from X Layer state — no wallet connection and no signup,
              because reading public chain state needs neither.
            </p>
          </div>
        </div>
        <div className="step">
          <div className="step-num">02</div>
          <div className="step-body">
            <h3>Get the measurements</h3>
            <p>
              Concentration, sector mix and cash buffer, each reported next to the
              reference point it is measured against — "41% of holdings, against a
              15–20% band commonly used as a single-name limit". Sarf is not a
              licensed or registered adviser, it does not know your income, goals
              or risk tolerance, and it does not tell you what to do.
            </p>
          </div>
        </div>
        <div className="step">
          <div className="step-num">03</div>
          <div className="step-body">
            <h3>Sign in when you want to act</h3>
            <p>
              Sign in with Google and Sarf provisions a wallet for you, or bring
              your own. Either way the keys stay with you — the server never
              receives them and cannot reconstruct them.
            </p>
          </div>
        </div>
        <div className="step">
          <div className="step-num">04</div>
          <div className="step-body">
            <h3>Confirm the trade</h3>
            <p>
              Sarf builds the transaction and shows you exactly what it does. You
              sign it. It settles on X Layer and you get the transaction hash
              back — verifiable by anyone, not a screenshot.
            </p>
          </div>
        </div>
      </div>

      <div className="card accent" style={{ marginTop: 30 }}>
        <h3>Talking to Sarf directly</h3>
        <p>
          Once connected, just ask — "how is my portfolio balanced?" or "buy $200
          of AAPLx" work as plain requests inside Claude or ChatGPT. No separate
          app to open.
        </p>
      </div>

      <div className="card">
        <h3>Trading inside the chat</h3>
        <p>
          Optionally, you can grant a session key so small trades settle without
          leaving the conversation. The caps are enforced by the contract, not by
          Sarf — and transfers are excluded from it entirely, so a session key can
          never move funds to someone else. You set the limits and can revoke at
          any time from <Link to="/settings">settings</Link>.
        </p>
      </div>

      <div className="connect-cta">
        <p>Ready to try it? Reading a portfolio needs no account at all.</p>
        <Link className="cta-btn" to="/portfolio">Read a portfolio</Link>
      </div>
    </section>
  );
}
