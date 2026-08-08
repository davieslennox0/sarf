import React, { useState } from 'react';
import { Link } from 'react-router-dom';

/**
 * MCP setup instructions.
 *
 * The endpoint below is the ONLY correct one: the MCP transport is served on
 * the sarf-mcp host, and nothing but /mcp and health is reachable there. The
 * main site host does not serve /mcp at all, so a connector pointed at it
 * fails with a 404 that looks like the server being down.
 */
const MCP_URL = 'https://sarf-mcp.managerx.xyz/mcp';

export default function Connect() {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    navigator.clipboard?.writeText(MCP_URL);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <section>
      <div className="eyebrow tick">Set up once</div>
      <h1>Connect Sarf</h1>
      <p className="sub">
        Add the MCP server to Claude or ChatGPT — about a minute, and you only do
        it once.
      </p>

      <div className="steps" style={{ marginTop: 30 }}>
        <div className="step">
          <div className="step-num">01</div>
          <div className="step-body">
            <h3>Open Settings → Connectors</h3>
            <p>In Claude or ChatGPT, choose to add a custom connector.</p>
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
          cannot sign one. Every trade comes back as an unsigned transaction with
          a link to review and sign in your own wallet — unless you have granted a
          session key, in which case small trades settle in chat under limits the
          contract enforces, and transfers still never can.
        </p>
      </div>

      <div className="connect-cta">
        <p>No wallet needed to try the analysis first.</p>
        <Link className="cta-btn" to="/portfolio">Read a portfolio</Link>
      </div>
    </section>
  );
}
