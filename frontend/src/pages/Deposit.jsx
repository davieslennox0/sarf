import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ensureSession } from '../api.js';
import { connect, currentAccount, short } from '../wallet.js';
import { ONRAMP_NAME, privyContext } from '../privy.jsx';

/**
 * MoonPay's mark, inline.
 *
 * Inline rather than an <img>: the deposit page is the one place a fake would
 * do real damage, and an asset fetched at runtime is an asset that can fail to
 * load, leaving an unnamed button that takes a card number. This renders from
 * the document itself, offline, with no request to anywhere.
 *
 * The crescent and the brand purple, at the size of a favicon — enough to
 * recognise beside the name, and deliberately not a reproduction of the full
 * lockup, which is theirs and not ours to redraw.
 */
function MoonPayMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <defs>
        <mask id="mp-crescent">
          <rect width="32" height="32" fill="#fff" />
          <circle cx="23" cy="9" r="8.5" fill="#000" />
        </mask>
      </defs>
      <circle cx="16" cy="16" r="13" fill="#7D00FF" mask="url(#mp-crescent)" />
      <circle cx="23" cy="9" r="4.6" fill="#7D00FF" />
    </svg>
  );
}

/**
 * Deposit: dollars from a card into something that can buy a stock.
 *
 * The route, and why it has this shape:
 *
 *   1. Fiat -> USDC on Base, done by MoonPay through Privy's funding flow.
 *      Sarf is not in this leg at all. It cannot be: taking a card payment is
 *      money transmission, and MoonPay's KYC and payout go directly to the
 *      user's own address.
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
 *
 * CLOSING THIS TAB IS SAFE. The first version of this page held the burn hash
 * in React state, which meant a refresh between the burn and the mint left
 * money burned on Base with nothing anywhere that knew to mint it. The hash is
 * now posted to the server the instant the wallet returns it, a sweeper
 * finishes anything still pending, and the list below is read back from the
 * server rather than from memory. The polling here only exists so that
 * somebody who *is* watching sees it land in seconds.
 */

const PRESETS = [50, 100, 250, 1000];

export default function Deposit() {
  const [address, setAddress] = useState(null);
  const [amount, setAmount] = useState(100);
  const [quote, setQuote] = useState(null);
  const [busy, setBusy] = useState(null);      // 'card' | 'moving' | null
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [deposits, setDeposits] = useState([]);

  const refresh = async () => {
    try { setDeposits((await api.depositList()).deposits || []); }
    catch { /* a listing failure must never break the funding flow */ }
  };

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const a = (await currentAccount()) || (await connect());
        if (!live) return;
        setAddress(a);
        await ensureSession(a);
        await refresh();
      } catch (e) { if (live) setErr(e.message || String(e)); }
    })();
    return () => { live = false; };
  }, []);

  // Keep the list honest while a deposit is in flight: the sweeper may be the
  // one that finishes it, and this page should show that when it happens.
  const anyPending = deposits.some((d) => d.status === 'pending');
  useEffect(() => {
    if (!anyPending) return undefined;
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [anyPending]);

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

  /*
    Step 1, the fiat leg — one route, because only one is enabled.

    This page briefly offered a second button for an ACH/wire deposit through
    Bridge. Bridge is not enabled on the Privy app, so that button opened a flow
    with nothing behind it: it did not error, it just failed to produce a way to
    pay, which is the worst shape a broken control can take. Offering a payment
    method that cannot take a payment is worse than offering one method.

    So: MoonPay, through Privy's unified funding flow. The flow offers whatever
    is enabled on the app and names no provider itself — see ONRAMP_NAME, which
    is what the copy on this page reads from, so enabling a second provider is
    one edit rather than a hunt through prose.

    Sarf is not a party to the payment. MoonPay runs its own KYC and pays out to
    the user's own address; no card number touches this app.
  */

  /** The destination, as the on-ramp wants it. Server-owned — see /deposit/quote. */
  const onrampTarget = quote?.from?.token_address && quote?.from?.caip2
    ? { address, chain: quote.from.caip2, asset: quote.from.token_address }
    : null;

  const notReady = 'Funding is still initialising. Give it a moment, or deposit '
    + 'by sending USDC on Base to your address below.';

  /*
    Closing the provider's modal is a decision, not a failure — but Privy
    rejects the promise for a deliberate exit and a real failure alike, and only
    the wording separates them.

    So the code wins where there is one. Privy's funding errors are the ones the
    user most needs to read (identity verification required, transaction limit
    reached, quote expired), and none of those should be lost to a word match.

    Where there is no code, this stays deliberately narrow. Guessing "exit" too
    eagerly means a genuine failure disappears and the button just does nothing,
    which is worse than an error message after a cancel: one is confusing, the
    other is invisible.
  */
  const report = (e) => {
    const m = e?.message || String(e);
    if (e?.privyErrorCode || !/exit|cancel|dismiss|user closed|closed the/i.test(m)) {
      setErr(m);
    }
  };

  const payByCard = async () => {
    setErr(null); setMsg(null); setBusy('card');
    try {
      const { addFunds } = privyContext();
      if (!addFunds) throw new Error(notReady);
      if (!onrampTarget) throw new Error('Still reading the deposit route — try again in a moment.');
      const res = await addFunds({
        destination: onrampTarget,
        fiat: {
          source: { assets: ['usd'], defaultAsset: 'usd' },
          defaultAmount: String(amount),
        },
      });
      setMsg(res?.status === 'confirmed'
        ? 'Paid. The USDC is on Base — press Move to X Layer below.'
        : 'Payment submitted. It lands as USDC on Base, usually within minutes; '
          + 'come back and move it over once it shows.');
    } catch (e) { report(e); } finally { setBusy(null); }
  };

  /** Step 2 — burn on Base, mint on X Layer. */
  const moveOver = async () => {
    setErr(null); setMsg(null); setBusy('moving');
    try {
      const prep = await api.depositPrepare(amount);
      const { sendOnChain } = await import('../wallet.js');
      // Approve first when the allowance is short. Doing it unconditionally
      // would be a second wallet prompt on every deposit after the first.
      const allowance = await api.depositAllowance(amount).catch(() => ({ enough: false }));
      const needsApproval = !allowance.enough;

      /*
        Gas, before anything is signed.

        The burn is sent by the user, on Base, so the user pays Base gas in ETH
        — and an on-ramp delivers USDC and nothing else. A wallet that has just
        been funded therefore holds exactly what it is trying to move and none
        of what moves it, and the wallet's answer to that is "insufficient
        funds for gas", which reads as the deposit being broken rather than as
        the account being a cent short.

        So Sarf's relayer tops the shortfall up in the user's own account first.
        It is a cent, it is unconditionally theirs, and it can only ever be
        spent on a transaction they sign themselves.

        Never fatal. If the top-up is refused — nothing to cover it with, a
        daily cap already used — the wallet still gets its chance to send: an
        account that already has ETH needs none of this, and the honest failure
        comes from the wallet with the real reason attached.
      */
      setMsg('Checking this wallet can pay for the burn…');
      const gas = await api.depositGas(needsApproval).catch(() => null);
      if (gas?.funded) {
        setMsg(gas.mined
          ? 'Sarf covered the gas for this burn — approve the transaction to send it.'
          : 'Sarf sent the gas for this burn; it is still confirming on Base. '
            + 'If the wallet says you cannot afford it, wait a few seconds and try again.');
      } else if (gas && Number(gas.short_wei) > 0) {
        setMsg(
          `Your wallet is short of ETH on Base to send this (${gas.reason}). `
          + 'Approve anyway if you have gas elsewhere, or send a little ETH to '
          + 'this address on Base.'
        );
      }

      if (needsApproval) {
        await sendOnChain(address, prep.approval, prep.chain_id);
      }
      const hash = await sendOnChain(address, prep.transaction, prep.chain_id);
      // Before anything else, and before any await that could be interrupted
      // by the user leaving: the money has already moved, so the server has to
      // know the deposit exists. Everything after this line is a convenience.
      await api.depositRecord(hash, amount).catch(() => {});
      await refresh();
      setMsg('Burn sent on Base. Waiting for Circle to attest it — you can close this page.');

      // Poll for the attestation, then let the relayer mint. Fast transfers
      // are seconds; the standard path waits for Base finality.
      for (let i = 0; i < 40; i += 1) {
        // eslint-disable-next-line no-await-in-loop
        const res = await api.depositComplete(hash, amount).catch((e) => ({ error: e.message }));
        if (res.settled) {
          setMsg(`Deposited. ${prep.receives_usdc ?? ''} USDC is on X Layer — mint ${res.mint_tx}.`);
          await refresh();
          return;
        }
        // eslint-disable-next-line no-await-in-loop
        await new Promise((r) => setTimeout(r, 6000));
      }
      setMsg(
        'Circle is still attesting. Nothing is stuck and nothing more is needed '
        + 'from you — Sarf finishes this in the background, and it will appear '
        + 'below and in your portfolio when it lands.'
      );
    } catch (e) {
      setErr(e.message || String(e));
    } finally { setBusy(null); await refresh(); }
  };

  /** Nudge a pending deposit rather than wait for the next sweep. */
  const finish = async (hash) => {
    setBusy('moving'); setErr(null);
    try {
      const res = await api.depositComplete(hash);
      setMsg(res.settled ? `Deposited — mint ${res.mint_tx}.` : res.note || 'Still pending.');
    } catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(null); await refresh(); }
  };

  return (
    <section>
      <div className="eyebrow tick">Add funds</div>
      <h1>Deposit dollars</h1>
      <p className="sub">
        Move dollars from a card into USDC on X Layer, ready to buy tokenized
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
              Pay by card and it arrives in minutes. {ONRAMP_NAME} runs its own
              checks and pays out to your address — Sarf never sees your card
              details and never holds the money.
            </p>
            <div className="cta">
              <button className="primary" disabled={busy !== null || !address || !onrampTarget}
                      onClick={payByCard}>
                {busy === 'card' ? 'Opening…' : 'Pay by card'}
              </button>
            </div>
            {/* Named and shown, not just implied. Handing someone off to a
                third party to type a card number into is the one moment on
                this page where they should know exactly who they are about to
                be dealing with, before the modal opens rather than after. */}
            <div className="provider">
              <MoonPayMark />
              <span>Payments by {ONRAMP_NAME}</span>
            </div>
          </div>
        </div>
        <div className="step">
          <div className="step-num">02</div>
          <div className="step-body">
            <h3>Move it to X Layer</h3>
            <p>
              Circle burns the USDC on Base and mints it here, 1:1. You sign
              once; Sarf covers the gas at both ends, so a wallet holding only
              USDC can still send it.
            </p>
            <div className="cta">
              <button disabled={busy !== null || !address} onClick={moveOver}>
                {busy === 'moving' ? 'Working…' : 'Move to X Layer'}
              </button>
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

      {deposits.length > 0 && (
        <>
          <div className="section-label">Your deposits</div>
          {/* Read back from the server, not from this page's memory. That is
              the whole point: a deposit exists because it is on chain and
              recorded, not because a tab is still open on it. */}
          <div className="ledger">
            {deposits.map((d) => (
              <div className="row static" key={d.burn_tx}>
                <span className="row-left">
                  <span className="row-id">
                    <span className="sym">
                      {d.amount_usd != null ? `$${Number(d.amount_usd).toFixed(2)}` : 'Deposit'}
                      <span className="chip" style={{ marginLeft: 8 }}>
                        {d.status === 'minted' ? 'ARRIVED'
                          : d.status === 'failed' ? 'NEEDS HELP' : 'IN FLIGHT'}
                      </span>
                    </span>
                    <span className="name">
                      {d.status === 'minted'
                        ? 'Minted on X Layer'
                        : d.status === 'failed'
                          ? 'Sarf stopped retrying — the burn is still on chain and can be finished'
                          : 'Burned on Base, waiting for Circle — Sarf finishes this for you'}
                    </span>
                  </span>
                </span>
                <span className="row-right">
                  {d.explorer_url
                    ? <a className="price" href={d.explorer_url} target="_blank" rel="noreferrer">View</a>
                    : (
                      <button className="linkish" disabled={busy !== null}
                              onClick={() => finish(d.burn_tx)}>
                        Check now
                      </button>
                    )}
                  <span className="weight">
                    {new Date((d.created_at || 0) * 1000).toLocaleString()}
                  </span>
                </span>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="card accent">
        <h3>Already hold USDC somewhere else?</h3>
        <p>
          Send it to <b className="addr">{address ? short(address) : 'your address'}</b> on
          Base and press <b>Move to X Layer</b> — the fiat step is only for
          starting from dollars. The same address works on both chains.
        </p>
      </div>

      <p className="fine">
        Sarf is not a bank, a broker or a money transmitter. Card payments are
        performed by {ONRAMP_NAME} under its own terms, KYC and fees; the
        transfer between chains is Circle's CCTP, burning and minting the same
        asset rather than bridging into a wrapper.
      </p>
    </section>
  );
}
