import React, { useState } from 'react';

/**
 * How it works — which is, in practice, how you connect.
 *
 * This page used to be prose about the design while /connect carried the setup
 * steps, and the two said the same thing in different words. /connect is gone
 * and its content lives here, because "how does this work" and "how do I set it
 * up" were never two questions.
 *
 * The endpoint below is the ONLY correct one: the MCP transport is served on
 * the sarf-mcp host, and nothing but /mcp and health is reachable there. The
 * main site host does not serve /mcp at all, so a connector pointed at it fails
 * with a 404 that looks like the server being down.
 */
const MCP_URL = 'https://sarf-mcp.managerx.xyz/mcp';

/**
 * Where each client keeps its connector list.
 *
 * Neither Claude nor ChatGPT accepts a prefilled "add this MCP server" deep
 * link — there is no documented URL that carries a name and endpoint into the
 * dialog, and inventing one produces a link that silently lands on a settings
 * page with an empty form. So the button does the two things that ARE
 * possible, in the order that makes the paste work: put the endpoint on the
 * clipboard first, then open the page where it has to go. The user arrives
 * with the URL already copied and one field to fill.
 */
const CLIENTS = [
  {
    id: 'claude',
    label: 'Add to Claude',
    href: 'https://claude.ai/settings/connectors',
    where: 'Settings → Connectors → Add custom connector',
  },
  {
    id: 'chatgpt',
    label: 'Add to ChatGPT',
    href: 'https://chatgpt.com/#settings/Connectors',
    where: 'Settings → Connectors → Add (developer mode, Plus/Pro)',
  },
];

export default function How() {
  const [copied, setCopied] = useState(false);
  const [sent, setSent] = useState(null);

  const copy = () => {
    navigator.clipboard?.writeText(MCP_URL);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const open = async (client) => {
    // Copy BEFORE navigating away. A clipboard write is a user-gesture
    // permission and window.open can steal the gesture's tail on some
    // browsers, so the write is awaited first and the tab opens after —
    // and if the clipboard is unavailable the tab still opens, because the
    // endpoint is on screen to copy by hand two lines below.
    try {
      await navigator.clipboard?.writeText(MCP_URL);
      setSent(client.id);
      setTimeout(() => setSent((s) => (s === client.id ? null : s)), 6000);
    } catch { /* no clipboard: the code block above is still copyable */ }
    window.open(client.href, '_blank', 'noopener,noreferrer');
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
        <div className="step">
          <div className="step-num">01</div>
          <div className="step-body">
            <h3>Open Settings → Connectors</h3>
            <p>
              In Claude or ChatGPT, choose to add a custom connector. The
              buttons above take you straight there.
            </p>
            <ul className="where">
              {CLIENTS.map((c) => (
                <li key={c.id}>
                  <b>{c.label.replace('Add to ', '')}</b> — {c.where}
                </li>
              ))}
            </ul>
          </div>
        </div>
        <div className="step">
          <div className="step-num">02</div>
          <div className="step-body">
            <h3>Paste the Sarf MCP URL</h3>
            <div className="copy-row">
              <code>{MCP_URL}</code>
              <button onClick={copy}>{copied ? 'copied' : 'copy'}</button>
            </div>
            <p style={{ marginTop: 8 }}>
              Your client will send you here to approve the connection. That
              approval is one signature proving you control the address — it
              authorizes no transaction and moves no funds.
            </p>
          </div>
        </div>
        <div className="step">
          <div className="step-num">03</div>
          <div className="step-body">
            <h3>Add a passkey</h3>
            <p>
              One touch of Face ID, Touch ID, or your device PIN. It is what
              approves every transaction from then on — nothing gets signed
              without it, and it never leaves your device.
            </p>
          </div>
        </div>
        <div className="step">
          <div className="step-num">04</div>
          <div className="step-body">
            <h3>Choose how it asks</h3>
            <p>
              <b>Always Ask</b> — every trade needs your passkey, whatever the
              size.
              <br />
              <b>Autonomous</b> — trades up to a limit you set go through without
              a prompt; anything above it still asks. Changing that limit needs
              your passkey again, so the agent can never raise it on its own.
            </p>
          </div>
        </div>
        <div className="step">
          <div className="step-num">05</div>
          <div className="step-body">
            <h3>Start asking</h3>
            <p>
              Try "what can I buy?", "price of NVDAx", or "how is my portfolio
              balanced?" right in the chat.
            </p>
          </div>
        </div>
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
