import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, ensureSession, registerPasskey, verifyPasskey } from '../api.js';
import { connect, currentAccount } from '../wallet.js';

/**
 * Send tokens or OKB to another address.
 *
 * Two things make this page different from the trading surfaces, and both are
 * deliberate rather than incidental:
 *
 * A transfer is the only thing Sarf builds that moves funds to somebody else,
 * and it is unrecoverable. So the recipient is echoed back in full, in
 * monospace, at the review step — never truncated to 0x1edd…9110, which is
 * exactly the format in which a swapped character survives a glance.
 *
 * And it is gated on a *fresh* passkey assertion regardless of amount. The
 * server enforces that; this page just makes the state visible, so the failure
 * arrives as a button that says what to do rather than an error after the user
 * has typed everything.
 */
export default function Transfer() {
  const nav = useNavigate();
  const [assets, setAssets] = useState([]);
  const [pk, setPk] = useState(null);
  const [now, setNow] = useState(Date.now());

  const [symbol, setSymbol] = useState('USDT');
  const [amount, setAmount] = useState('');
  const [to, setTo] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);

  const load = async () => {
    try {
      const addr = (await currentAccount()) || (await connect());
      await ensureSession(addr);
      setPk(await api.passkeyStatus());
    } catch (e) { setErr(e.message); }
  };

  useEffect(() => {
    load();
    api.list().then((d) => setAssets(d.assets || [])).catch(() => {});
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // Mirrors the server's window. Shown as a countdown because "verified" with
  // no clock is the state people trust right up until it has quietly lapsed.
  const freshFor = useMemo(() => {
    if (!pk?.last_verified_at) return 0;
    const left = pk.last_verified_at + (pk.stepup_valid_for_seconds || 180)
      - Math.floor(now / 1000);
    return Math.max(0, left);
  }, [pk, now]);

  const run = async (fn, okMsg) => {
    setBusy(true); setErr(null); setMsg(null);
    try { await fn(); setMsg(okMsg); setPk(await api.passkeyStatus()); }
    catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(false); }
  };

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(null); setMsg(null);
    try {
      const order = await api.transferPrepare({ symbol, amount, to_address: to });
      // Hand off to the existing review-and-sign surface rather than growing a
      // second one here: one signing screen means one place where what the user
      // approves is displayed.
      nav(`/sign?o=${encodeURIComponent(order.order_id)}`);
    } catch (e2) {
      setErr(e2.message || String(e2));
    } finally { setBusy(false); }
  };

  const ready = freshFor > 0 && amount && to && !busy;

  return (
    <section>
      <h1>Send</h1>

      <div className="disclosure">
        <b>Transfers are final.</b> Once mined there is no recall and no reversal,
        and an address that is wrong but valid will accept the funds with nobody
        able to return them. Check the recipient character by character on the next
        screen.
        <br /><br />
        Sarf builds this transaction; <b>you</b> sign it in your own wallet. No
        session grant can ever perform a transfer — the contract behind in-chat
        trading is built so that moving funds out is impossible.
      </div>

      {!pk?.registered ? (
        <>
          <p className="error">
            Transfers need a passkey. It is what proves a person is present for the
            one action that sends funds to someone else.
          </p>
          <div className="cta">
            <button className="primary" disabled={busy}
                    onClick={() => run(registerPasskey, 'Passkey registered.')}>
              Register a passkey
            </button>
          </div>
        </>
      ) : freshFor <= 0 ? (
        <>
          <p className="muted small">
            Verify with your passkey to unlock sending. The window is short on
            purpose — {pk.stepup_valid_for_seconds}s — so an unattended session
            cannot be used to move funds later.
          </p>
          <div className="cta">
            <button className="primary" disabled={busy}
                    onClick={() => run(verifyPasskey, 'Verified — you can send now.')}>
              Verify to send
            </button>
          </div>
        </>
      ) : (
        <p className="ok">
          Verified — sending unlocked for {Math.floor(freshFor / 60)}m{' '}
          {String(freshFor % 60).padStart(2, '0')}s
        </p>
      )}

      <form onSubmit={submit} style={{ marginTop: 18 }}>
        <div className="kv">
          <div>
            <span>Asset</span>
            <b>
              <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
                <option value="USDT">USDT</option>
                <option value="OKB">OKB (gas)</option>
                {assets.map((a) => (
                  <option key={a.symbol} value={a.symbol}>{a.symbol}</option>
                ))}
              </select>
            </b>
          </div>
          <div>
            <span>Amount</span>
            <b>
              <input inputMode="decimal" placeholder="0.0" value={amount}
                     onChange={(e) => setAmount(e.target.value.trim())} />
            </b>
          </div>
          <div>
            <span>To address</span>
            <b>
              <input className="addr" placeholder="0x…" value={to}
                     onChange={(e) => setTo(e.target.value.trim())} />
            </b>
          </div>
        </div>

        <div className="cta">
          <button className="primary" type="submit" disabled={!ready}>
            {busy ? 'Building…' : 'Review transfer'}
          </button>
        </div>
      </form>

      {symbol === 'OKB' && (
        <p className="muted small">
          OKB pays for gas on X Layer, including this transfer. A float is kept back
          automatically so a send cannot leave the wallet unable to transact.
        </p>
      )}
      {msg && <p className="ok">{msg}</p>}
      {err && <p className="error">{err}</p>}
    </section>
  );
}
