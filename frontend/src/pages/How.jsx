import React, { useState } from 'react';
import { CLIENTS, MCP_URL, STEPS, openClient } from '../guide.jsx';

/**
 * How it works — which is, in practice, how you connect.
 *
 * This page used to be prose about the design while /connect carried the setup
 * steps, and the two said the same thing in different words. /connect is gone
 * and its content lives here, because "how does this work" and "how do I set it
 * up" were never two questions.
 *
 * The steps themselves now come from ../guide.jsx, because the header renders
 * the same list once you are signed in and two copies would drift.
 */
export default function How() {
  const [copied, setCopied] = useState(false);
  const [sent, setSent] = useState(null);

  const copy = () => {
    navigator.clipboard?.writeText(MCP_URL);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const open = async (client) => {
    if (await openClient(client)) {
      setSent(client.id);
      setTimeout(() => setSent((s) => (s === client.id ? null : s)), 6000);
    }
  };

  return (
    <section>
      <div className="eyebrow tick">Set up once</div>
      <h1>How it works</h1>
      <p className="sub">
        Add Sarf to Claude or ChatGPT — about a minute, and you only do it once.
      </p>

      {/* The shortcut, above the steps rather than after them: the steps are
          for people who want to know what is happening, and everyone else
          just wants to be taken to the right settings page with the endpoint
          already copied. */}
      <div className="client-buttons">
        {CLIENTS.map((c) => (
          <button key={c.id} className="primary" onClick={() => open(c)}>
            {sent === c.id ? 'URL copied — paste it' : c.label}
          </button>
        ))}
      </div>
      <p className="muted small" style={{ marginTop: 10 }}>
        Opens your client's connector settings in a new tab and copies the Sarf
        endpoint to your clipboard — paste it into <b>Add custom connector</b>.
        Neither client accepts a prefilled link, so this is one paste, not none.
      </p>

      <div className="steps" style={{ marginTop: 30 }}>
        {STEPS.map((s) => (
          // The id is what the header menu links to. Scroll margin keeps the
          // sticky header off the heading when you arrive from one.
          <div className="step" key={s.id} id={s.id}>
            <div className="step-num">{s.num}</div>
            <div className="step-body">
              <h3>{s.title}</h3>
              {/* Step 02 is the only one that needs a control rather than
                  prose, and it belongs above its explanation. */}
              {s.id === 'step-endpoint' && (
                <div className="copy-row">
                  <code>{MCP_URL}</code>
                  <button onClick={copy}>{copied ? 'copied' : 'copy'}</button>
                </div>
              )}
              {s.body}
            </div>
          </div>
        ))}
      </div>

      <div className="code-block">
        <span className="k">MCP endpoint:</span>{'\n'}
        {MCP_URL}{'\n\n'}
        <span className="k">Chain:</span> X Layer (chain id 196){'\n'}
        <span className="k">Assets:</span> 40 tokenized equities and ETFs
      </div>

      <div className="card accent">
        <h3>What the connector can and cannot do</h3>
        <p>
          It can read your holdings, price assets, and build transactions. It
          cannot sign one without your passkey. Every trade comes back as an
          unsigned transaction to review and sign in your own wallet — unless you
          have granted a session key, in which case trades settle in chat under
          limits the contract enforces, still gated by your passkey. Transfers to
          another address always need a fresh passkey and can never be delegated.
        </p>
      </div>

      {/*
        The "Read a portfolio" call to action is gone.

        It offered an analysis of any pasted address, which stopped being true
        when /portfolio became account-only — the button led to a sign-in
        prompt, so the one thing it promised (no wallet needed) was the one
        thing it could not do.
      */}
    </section>
  );
}
