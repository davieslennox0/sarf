import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ensureSession } from '../api.js';
import { connect, currentAccount, short } from '../wallet.js';
import { privyContext } from '../privy.jsx';

/**
 * Deposit: dollars from a bank account into something that can buy a stock.
 *
 * The route, and why it has this shape:
 *
 *   1. Fiat -> USDC on Base, done by a licensed provider through Privy's
 *      funding flow. Sarf is not in this leg at all. It cannot be: taking a
 *      bank transfer is money transmission, and the provider's KYC and payout
 *      go directly to the user's own address.
 *   2. USDC on Base -> USDC on X Layer, by Circle's CCTP. Burned there,
 *      minted here, 1:1. Not a bridge — there is no pool to drain and no
 *      wrapper to hold.
 *   3. USDC buys xStocks directly. No conversion to USDT first: the
 *      aggregator routes USDC into every listed asset.
 *
 * The user signs ONE transaction, the burn. Sarf's relayer submits the mint,
 * which is permissionless and can only deliver to the recipient written into
 * the message they signed — so there is nothing to trust it with.
 *
 * No on-ramp settles onto X Layer, which is why Base appears at all. It is
 * plumbing, and the page says so rather than pretending the money took a
 * straight line.
 */

const USDC_BASE = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913';
const BASE_CAIP2 = 'eip155:8453';
const PRESETS = [50, 100, 250, 1000];

export default function Deposit() {
  const [address, setAddress] = useState(null);
  const [amount, setAmount] = useState(100);
  const [quote, setQuote] = useState(null);
  const [busy, setBusy] = useState(null);      // 'funding' | 'moving' | null
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [burnHash, setBurnHash] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const a = (await currentAccount()) || (await connect());
        setAddress(a);
        await ensureSession(a);
      } catch (e) { setErr(e.message || String(e)); }
    })();
  }, []);

  // Quote whenever the amount settles. It is the only place the cost of the
  // transfer is stated, and it is quoted from Circle rather than assumed.
  useEffect(() => {
    let live = true;
    const t = setTimeout(async () => {
      try {
        const q = await api.depositQuote(amount);
        if (live) { setQuote(q); setErr(null); }
      } catch (e) { if (live) setQuote(null); }
    }, 350);
    return () => { live = false; clearTimeout(t); };
  }, [amount]);

  /** Step 1 — the bank leg, run entirely by the provider. */
  const addCash = async () => {
    setErr(null); setMsg(null); setBusy('funding');
    try {
      const { addFunds } = privyContext();
      if (!addFunds) {
        throw new Error(
          'Card and bank funding is not enabled on this deployment yet. You can '
          + 'still deposit by sending USDC on Base to your address below.'
        );
      }
      await addFunds({
        destination: { address, chain: BASE_CAIP2, asset: USDC_BASE },
        fiat: { source: { assets: ['usd'], defaultAsset: 'usd' },
                defaultAmount: String(amount) },
      });
      setMsg(
        'Provider flow finished. Bank transfers can take a few minutes to '
        + 'settle — once the USDC shows on Base, move it over below.'
      );
    } catch (e) {
      // A user closing the provider modal is not an error worth shouting about.
      const m = e?.message || String(e);
      if (!/exit|cancel|close/i.test(m)) setErr(m);
    } finally { setBusy(null); }
  };

  /** Step 2 — burn on Base, mint on X Layer. */
  const moveOver = async () => {
    setErr(null); setMsg(null); setBusy('moving');
    try {
      const prep = await api.depositPrepare(amount);
      const { sendOnChain } = await import('../wallet.js');
      // Approve first when the allowance is short. Doing it unconditionally
      // would be a second wallet prompt on every deposit after the first.
      const needsApproval = await api.depositAllowance(amount).catch(() => ({ enough: false }));
      if (!needsApproval.enough) {
        await sendOnChain(address, prep.approval, prep.chain_id);
      }
      const hash = await sendOnChain(address, prep.transaction, prep.chain_id);
      setBurnHash(hash);
      setMsg('Burn sent on Base. Waiting for Circle to attest it…');

      // Poll for the attestation, then let the relayer mint. Fast transfers
      // are seconds; the standard path waits for Base finality.
      for (let i = 0; i < 40; i += 1) {
        // eslint-disable-next-line no-await-in-loop
        const res = await api.depositComplete(hash).catch((e) => ({ error: e.message }));
        if (res.settled) {
          setMsg(`Deposited. ${prep.receives_usdc ?? ''} USDC is on X Layer — mint ${res.mint_tx}.`);
          return;
        }
        // eslint-disable-next-line no-await-in-loop
        await new Promise((r) => setTimeout(r, 6000));
      }
      setMsg(
        'Still waiting on Circle. The burn is safe — reload this page later and '
        + 'press Complete deposit to finish it.'
      );
    } catch (e) {
      setErr(e.message || String(e));
    } finally { setBusy(null); }
  };

  const finish = async () => {
    if (!burnHash) return;
    setBusy('moving'); setErr(null);
    try {
      const res = await api.depositComplete(burnHash);
      setMsg(res.settled ? `Deposited — mint ${res.mint_tx}.` : res.note || 'Still pending.');
    } catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(null); }
  };

  return (
    <section>
      <div className="eyebrow tick">Add funds</div>
      <h1>Deposit dollars</h1>
      <p className="sub">
        Move USD from your bank into USDC on X Layer, ready to buy tokenized
        stocks. Sarf never holds the money at any point.
      </p>

      <div className="kv">
        <div><span>Depositing to</span><b className="addr">{address || '—'}</b></div>
        <div><span>Arrives as</span><b>USDC · X Layer</b></div>
      </div>

      <div className="section-label">Amount</div>
      <div className="cta">
        {PRESETS.map((p) => (
          <button key={p} className={amount === p ? 'primary' : ''} onClick={() => setAmount(p)}>
            ${p}
          </button>
        ))}
        <input
          type="number" min="5" value={amount}
          onChange={(e) => setAmount(Number(e.target.value) || 0)}
          style={{ width: 120 }}
        />
      </div>

      {quote && (
        <div className="kv">
          <div><span>You send</span><b>${Number(quote.amount_usd).toFixed(2)}</b></div>
          <div><span>You receive</span><b>{Number(quote.receives_usdc).toFixed(2)} USDC</b></div>
          <div><span>Transfer cost</span>
            <b>${Number(quote.fee_usd).toFixed(3)} ({quote.fee_bps} bps)</b></div>
          <div><span>Takes about</span><b>{quote.estimated_seconds}s</b></div>
          <div><span>Mechanism</span><b>{quote.mechanism}</b></div>
        </div>
      )}

      <div className="steps grid g3" style={{ marginTop: 8 }}>
        <div className="step">
          <div className="step-num">01</div>
          <div className="step-body">
            <h3>Add cash</h3>
            <p>
              Your bank or card, handled by a licensed provider with its own
              checks. It pays out to your address — Sarf never sees the details.
            </p>
            <div className="cta">
              <button className="primary" disabled={busy !== null || !address}
                      onClick={addCash}>
                {busy === 'funding' ? 'Opening…' : 'Add cash'}
              </button>
            </div>
          </div>
        </div>
        <div className="step">
          <div className="step-num">02</div>
          <div className="step-body">
            <h3>Move it to X Layer</h3>
            <p>
              Circle burns the USDC on Base and mints it here, 1:1. You sign
              once; Sarf's relayer pays for the arrival.
            </p>
            <div className="cta">
              <button disabled={busy !== null || !address} onClick={moveOver}>
                {busy === 'moving' ? 'Working…' : 'Move to X Layer'}
              </button>
              {burnHash && (
                <button onClick={finish} disabled={busy !== null}>Complete deposit</button>
              )}
            </div>
          </div>
        </div>
        <div className="step">
          <div className="step-num">03</div>
          <div className="step-body">
            <h3>Buy</h3>
            <p>
              USDC buys any listed asset directly — no conversion first. Ask in
              chat, or start from the markets list.
            </p>
            <div className="cta">
              <Link className="cta-btn ghost" to="/markets">Browse markets</Link>
            </div>
          </div>
        </div>
      </div>

      {msg && <p className="ok">{msg}</p>}
      {err && <p className="error">{err}</p>}

      <div className="card accent">
        <h3>Already hold USDC somewhere else?</h3>
        <p>
          Send it to <b className="addr">{address ? short(address) : 'your address'}</b> on
          Base and press <b>Move to X Layer</b> — the fiat step is only for
          starting from dollars. The same address works on both chains.
        </p>
      </div>

      <p className="fine">
        Sarf is not a bank, a broker or a money transmitter. The bank transfer is
        performed by a licensed provider under its own terms and KYC; the transfer
        between chains is Circle's CCTP, burning and minting the same asset rather
        than bridging into a wrapper. xStocks bought afterwards are synthetic
        exposure — no ownership, dividends or voting rights.
      </p>
    </section>
  );
}
