import React, { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api.js';
import { connect, currentAccount, sendTransaction } from '../wallet.js';
import { OpenInChat } from '../handoff.jsx';
import { Fact, num } from '../zapui.jsx';

/**
 * One Pendle "RWA Fixed Yield" position. Public and bookmarkable, same
 * reasoning as ZapPosition: it shows what the chain already shows. Signing
 * (entry, withdraw, and — only if there are real proceeds — the RWA buy)
 * needs the owner's wallet, same runFlow loop ZapPosition already uses:
 * ask the server for the next step, sign it, confirm, repeat until done.
 *
 * Deliberately much smaller than ZapPosition — no IL ring, no thresholds,
 * no exit/re-entry history. The whole position is: principal in, PT held,
 * and once withdrawn, what actually came back and what it bought.
 */

const REFRESH_MS = 15000;
const u6 = (units) => (units == null ? null : Number(units) / 1e6);
const u18 = (units) => (units == null ? null : Number(units) / 1e18);
const amt = (n, places = 6) => (n == null ? '—' : n.toLocaleString(undefined, { maximumFractionDigits: places }));

const STATE_LABEL = {
  entering: 'Depositing — sign the steps below',
  open: 'Open, earning the fixed rate',
  exiting: 'Withdrawing — sign the steps below',
  buying_rwa: 'Buying the designated RWA with proceeds — sign below',
  closed: 'Closed',
};

export default function PendlePosition() {
  const { id } = useParams();
  const [v, setV] = useState(null);
  const [isOwner, setIsOwner] = useState(false);
  const [err, setErr] = useState(null);
  const [signing, setSigning] = useState(null);
  const [note, setNote] = useState(null);

  const load = useCallback(async () => {
    try {
      let view = null;
      const mine = await api.pendleMine().catch(() => null);
      view = mine?.positions?.find((p) => p.position_id === id) || null;
      setIsOwner(!!view);
      setV(view || (await api.pendlePosition(id)));
      setErr(null);
    } catch (e) {
      setErr(e.message);
    }
  }, [id]);

  useEffect(() => {
    load();
    const t = setInterval(() => { if (!signing) load(); }, REFRESH_MS);
    return () => clearInterval(t);
  }, [load, signing]);

  const signIn = async () => (await currentAccount()) || (await connect());

  // Same shape as ZapPosition's runFlow: ask the server for the next step
  // (built fresh from live chain state every time), sign it, confirm, and
  // keep going until the position has nothing left to sign.
  const runFlow = async () => {
    setErr(null);
    setNote(null);
    try {
      const addr = await signIn();
      for (;;) {
        const st = await api.pendleStep(id);
        if (st.status !== 'sign') break;
        setSigning({ title: st.title });
        const hash = await sendTransaction(addr, st.tx);
        let res = await api.pendleStepSubmitted(id, hash);
        while (res.status === 'pending') res = await api.pendleStepSubmitted(id, hash);
      }
      setNote('All steps signed and confirmed.');
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setSigning(null);
      load();
    }
  };

  const withdraw = async () => {
    setErr(null);
    try {
      await signIn();
      setV(await api.pendleWithdraw(id));
      setNote('Withdraw queued. Sign it below.');
    } catch (e) {
      setErr(e.message || String(e));
    }
  };

  if (err && !v) return <section><p className="error">{err}</p><p><Link to="/earn">Earn</Link></p></section>;
  if (!v) return <section><p className="muted">Loading position…</p></section>;

  const principal = u6(v.principal_units);
  const pt = u6(v.pt_units);
  const redeemed = u6(v.redeemed_units);
  const proceeds = u6(v.proceeds_units);
  const rwa = u18(v.rwa_units);
  const pending = ['entering', 'exiting', 'buying_rwa'].includes(v.state);
  const closed = v.state === 'closed';
  const ask = (s) => `Using Sarf, ${s} (Pendle RWA position ${v.position_id}).`;

  return (
    <section className="zap">
      <Link className="backlink" to="/earn">← Earn</Link>

      <div className="market-head">
        <div className="pos-id">
          <div>
            <h1>
              RWA Fixed Yield
              <span className={`chip${pending ? ' accent' : ''}`}>{v.state.replace(/_/g, ' ')}</span>
            </h1>
            <p className="sub">{STATE_LABEL[v.state] || v.state} · proceeds buy {v.designated_rwa_symbol}</p>
          </div>
        </div>
        <div className="market-stats">
          <div><b>{amt(principal, 2)} {v.deposit_symbol}</b><span>principal</span></div>
          <div><b>{amt(pt, 2)}</b><span>PT-USDG held</span></div>
          <div><b>{v.designated_rwa_symbol}</b><span>what proceeds buy</span></div>
        </div>
      </div>

      {err && <p className="error" style={{ marginTop: 14 }}>{err}</p>}
      {note && <p className="ok" style={{ marginTop: 14 }}>{note}</p>}

      <div className="pos-grid">
        <div className="pos-main">
          <div className="card">
            <h3>Principal-protected by construction</h3>
            <p className="small" style={{ marginTop: 0 }}>
              Your {v.deposit_symbol} bought a Pendle fixed-yield Principal Token. At
              withdrawal, the principal always comes back as {v.deposit_symbol}. Only the
              proceeds actually earned — never the principal — buy {v.designated_rwa_symbol}.
              If there are no proceeds, nothing is bought; there is never a loss beyond the
              position itself.
            </p>
            <div className="dp-facts" style={{ marginTop: 20 }}>
              <Fact label="Principal" value={`${amt(principal, 2)} ${v.deposit_symbol}`} />
              <Fact label="PT held" value={amt(pt, 2)} sub="PT-USDG-29OCT2026" />
              {closed && (
                <>
                  <Fact label="Redeemed" value={`${amt(redeemed, 2)} ${v.deposit_symbol}`} />
                  <Fact label="Proceeds" value={`${amt(proceeds, 2)} ${v.deposit_symbol}`}
                        tone={proceeds > 0 ? 'ok' : undefined} />
                  <Fact label={`${v.designated_rwa_symbol} bought`}
                        value={rwa > 0 ? num(rwa, 8) : 'none'}
                        sub={rwa > 0 ? undefined : 'no proceeds this round'} />
                </>
              )}
            </div>
          </div>
        </div>

        <aside className="pos-side">
          {isOwner && pending && (
            <div className="card accent">
              <h3>{STATE_LABEL[v.state]}</h3>
              {signing ? (
                <p>
                  Confirm "{signing.title}" in your wallet…
                  <br /><span className="muted small">This page moves on by itself once it lands.</span>
                </p>
              ) : (
                <>
                  <button className="primary big" style={{ marginTop: 10 }} onClick={runFlow}>Sign next steps in wallet</button>
                  <OpenInChat text={ask('show me what is waiting to be signed on my Pendle RWA position and give me the link')} />
                </>
              )}
            </div>
          )}
          {!isOwner && pending && (
            <div className="card">
              <h3>Waiting on its owner</h3>
              <p>This position has steps waiting for a signature. Sign in with the owning wallet to continue.</p>
              <button style={{ marginTop: 10 }} onClick={() => load()}>I'm the owner, reload</button>
            </div>
          )}
          {isOwner && v.state === 'open' && (
            <div className="card">
              <h3>Withdraw</h3>
              <p className="small">Redeems the PT back to {v.deposit_symbol}. Principal returns as-is; any real proceeds buy {v.designated_rwa_symbol}.</p>
              <button className="danger" style={{ marginTop: 10 }} onClick={withdraw}>Withdraw now</button>
              <OpenInChat text={ask('withdraw my Pendle RWA position')} />
            </div>
          )}
          <div className="card">
            <h3>This position</h3>
            <div className="side-actions">
              <button className="btn ghost" onClick={() => { navigator.clipboard?.writeText(window.location.href); setNote('Link copied.'); }}>
                Copy share link
              </button>
            </div>
            <OpenInChat text={ask('show me my Pendle RWA position')} label="check it in chat" />
          </div>
        </aside>
      </div>

      <p className="disclosure" style={{ marginTop: 22 }}>
        USDG's reserves are cash and short-term US Treasury bills, attested monthly by Paxos.
        The fixed rate is Pendle's own live quote, not a Sarf guarantee, and only applies to a
        position held to maturity — an early exit sells the PT on Pendle's own market instead.
      </p>
    </section>
  );
}
