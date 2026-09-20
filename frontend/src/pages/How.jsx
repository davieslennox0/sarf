import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api.js';
import { CLIENTS, MCP_URL, STEPS, openClient } from '../guide.jsx';
import { ASSET_COUNT } from '../market.jsx';

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
  const [count, setCount] = useState(null);
  useEffect(() => { api.list().then((d) => setCount(d.count)).catch(() => {}); }, []);

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
        <span className="k">Assets:</span> {count ?? ASSET_COUNT} tokenized stocks and ETFs
      </div>

      <div className="card accent">
        <h3>What the connector can and cannot do</h3>
        <p>
          It can read your holdings, price assets and build transactions. It cannot move
          funds on its own: every trade comes back as an unsigned transaction for your wallet
          to sign, unless you have granted a session key, in which case small trades settle
          in chat within caps the contract itself enforces. Transfers to another address
          always go to your wallet and can never be delegated.
        </p>
      </div>

      <div className="section-label">Chat or website: your choice</div>
      <div className="surfaces">
        <div className="surface">
          <h3>In Claude or ChatGPT</h3>
          <p>Ask in plain words. Sarf quotes it, builds it, and hands you a link to sign, or settles small trades in chat under your session key.</p>
          <ul>
            <li>"buy $50 of NVDAx" <em>· swaps and orders</em></li>
            <li>"zap 0.5 SPCXx, exit at 8% IL" <em>· IL-protected liquidity</em></li>
            <li>"split $100 across SPYx and QQQx" <em>· basket orders</em></li>
            <li>"how is my portfolio doing?" <em>· holdings and analysis</em></li>
          </ul>
          <button className="primary" onClick={() => open(CLIENTS[0])}>Add to Claude</button>
        </div>
        <div className="surface">
          <h3>Right here on the website</h3>
          <p>No chat needed. The same prices, checks and fee, and every action is signed in your own wallet.</p>
          <ul>
            <li>Swap <em>· any xStock against USDT, USDC, OKB or another xStock</em></li>
            <li>Zap <em>· deposit, watch IL, exit and re-enter</em></li>
            <li>Portfolio <em>· holdings, value, send</em></li>
            <li>Account <em>· connected agents, session key, revoke</em></li>
          </ul>
          <Link className="btn primary" to="/swap">Open Swap</Link>
        </div>
      </div>

      <table className="parity">
        <thead><tr><th>What you want to do</th><th>In chat</th><th>On the website</th></tr></thead>
        <tbody>
          <tr><td>See prices and markets</td><td>"price of NVDAx"</td><td><Link to="/markets">Markets</Link></td></tr>
          <tr><td>Buy, sell or swap</td><td>"buy $50 of SPYx"</td><td><Link to="/swap">Swap</Link></td></tr>
          <tr><td>IL-protected liquidity</td><td>"zap 0.5 SPCXx"</td><td><Link to="/zap">Zap</Link></td></tr>
          <tr><td>Holdings and value</td><td>"how is my portfolio doing?"</td><td><Link to="/portfolio">Portfolio</Link></td></tr>
          <tr><td>Send to another address</td><td>"send 10 USDT to 0x…"</td><td><Link to="/portfolio">Portfolio → Send</Link></td></tr>
          <tr><td>Add money by card</td><td>"deposit $50"</td><td><Link to="/portfolio?fund=1">Portfolio → Fund</Link></td></tr>
          <tr><td>Stop-loss and take-profit levels</td><td>"stop-loss NVDAx at $200"</td><td><Link to="/portfolio">Portfolio → Levels</Link></td></tr>
          <tr><td>xPoints</td><td>"how many xPoints do I have?"</td><td><Link to="/portfolio">Portfolio</Link></td></tr>
          <tr><td>Basket orders</td><td>"split $100 across SPYx and QQQx"</td><td><b>Chat for now</b>, or one swap per asset</td></tr>
        </tbody>
      </table>

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
