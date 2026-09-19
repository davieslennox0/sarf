import React, { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount, sendTransaction, txUrl } from '../wallet.js';
import { OpenInChat } from '../handoff.jsx';

/**
 * One zap position. Public and bookmarkable: anyone with the link sees IL,
 * value against holding, and the exit/re-entry history, which is all readable
 * on-chain anyway. Acting on it (signing steps, moving thresholds, exiting)
 * needs the owner's wallet session, and every action also offers the same
 * request as a prefilled chat.
 */

const REFRESH_MS = 15000;
const usd = (x) => (x == null ? '—' : `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const signed = (x) => (x == null ? '—' : `${x >= 0 ? '+' : '−'}$${Math.abs(x).toFixed(2)}`);
const pctBps = (b) => (b == null ? '—' : `${(Number(b) / 100).toFixed(2)}%`);
const when = (t) => new Date(t * 1000).toLocaleString();

const EVENT_LABELS = {
  created: 'Position created',
  step_confirmed: 'Step confirmed',
  step_failed: 'Step reverted, nothing moved',
  entered: 'Entered the pool',
  reentered: 'Re-entered the pool, entry price reset',
  exit_triggered: 'IL crossed the exit line',
  exit_trigger_cleared: 'IL fell back before the exit was signed',
  exit_requested: 'Exit requested',
  exit_started: 'Exit signing started',
  exited: 'Exited to Aave',
  reentry_triggered: 'IL back under the re-entry line',
  reentry_trigger_cleared: 'IL rose again before re-entry was signed',
  reentry_requested: 'Re-entry requested',
  reenter_started: 'Re-entry signing started',
  thresholds_changed: 'Thresholds changed',
  cancelled: 'Cancelled',
};

function IlMeter({ il }) {
  // Scale to 1.5x the exit line so both markers and the current value fit.
  const max = Math.max(il.exit_threshold_bps * 1.5, (il.current_bps || 0) * 1.1, 1);
  const at = (v) => `${Math.min(100, (v / max) * 100)}%`;
  const over = il.current_bps != null && il.current_bps > il.exit_threshold_bps;
  return (
    <div className="il-meter" aria-label="Impermanent loss against thresholds">
      <div className={`il-fill${over ? ' over' : ''}`} style={{ width: at(il.current_bps || 0) }} />
      <div className="il-mark re" style={{ left: at(il.reentry_threshold_bps) }} title="re-entry line" />
      <div className="il-mark ex" style={{ left: at(il.exit_threshold_bps) }} title="exit line" />
    </div>
  );
}

export default function ZapPosition() {
  const { id } = useParams();
  const [v, setV] = useState(null);
  const [isOwner, setIsOwner] = useState(false);
  const [err, setErr] = useState(null);
  const [signing, setSigning] = useState(null); // { title, index, count } while a flow runs
  const [note, setNote] = useState(null);
  const [th, setTh] = useState({ exit: '', re: '' });
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      let view = null;
      if (getSession()) {
        const mine = await api.zapMine().catch(() => null);
        view = mine?.positions?.find((p) => p.position_id === id) || null;
      }
      setIsOwner(!!view);
      setV(view || (await api.zapPosition(id)));
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

  const signIn = async () => {
    const addr = (await currentAccount()) || (await connect());
    await ensureSession(addr);
    return addr;
  };

  // Walk the wallet through every step the flow still needs. Each step is
  // built by the server from live state when it is asked for, and advanced
  // only on the amounts the mined receipt actually credited.
  const runFlow = async () => {
    setErr(null);
    setNote(null);
    try {
      const addr = await signIn();
      for (;;) {
        const st = await api.zapStep(id);
        if (st.status === 'awaiting_confirmation') {
          setSigning({ title: 'Waiting for the last transaction to confirm…' });
          await api.zapStepSubmitted(id, st.tx_hash);
          continue;
        }
        if (st.status !== 'sign') break;
        setSigning({ title: st.title, index: st.step_index, count: st.step_count });
        const hash = await sendTransaction(addr, st.tx);
        let res = await api.zapStepSubmitted(id, hash);
        while (res.status === 'pending') res = await api.zapStepSubmitted(id, hash);
        if (res.status === 'failed') throw new Error(res.detail);
      }
      setNote('All steps signed and confirmed.');
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setSigning(null);
      load();
    }
  };

  const act = async (fn, msg) => {
    setErr(null);
    try {
      await signIn();
      setV(await fn());
      setIsOwner(true);
      if (msg) setNote(msg);
    } catch (e) {
      setErr(e.message || String(e));
    }
  };

  if (err && !v) return <section><p className="error">{err}</p><p><Link to="/zap">All zap pools</Link></p></section>;
  if (!v) return <section><p className="muted">Loading position…</p></section>;

  const { il, value, pool } = v;
  const y = v.yield;
  const c = v.costs;
  const parked = ['parked', 'reentry_pending'].includes(v.state);
  const pending = !!v.action_needed;
  const share = () => {
    navigator.clipboard?.writeText(window.location.href);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  const ask = (s) => `Using Sarf, ${s} (zap position ${v.position_id}).`;

  return (
    <section className="zap">
      <div className="eyebrow tick"><Link to="/zap">Zap</Link> · {pool.pair} · Uniswap V2 on X Layer</div>
      <h1 style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        {pool.pair}
        <span className={`chip${pending ? ' accent' : ''}`}>{v.state.replace('_', ' ')}</span>
      </h1>
      <p className="sub">{v.state_label}</p>
      <p className="zap-headline">{v.headline}</p>

      {err && <p className="error" style={{ marginTop: 14 }}>{err}</p>}
      {note && <p className="ok" style={{ marginTop: 14 }}>{note}</p>}

      <div className="stats zap-stats">
        <div><b>{pctBps(il.current_bps)}</b><span>impermanent loss now</span></div>
        <div><b>{pctBps(il.exit_threshold_bps)}</b><span>exit line · re-enter under {pctBps(il.reentry_threshold_bps)}</span></div>
        <div><b>{usd(value.current_usd)}</b><span>{parked ? 'parked in Aave' : 'position value'}</span></div>
        {/* Yield is never shown without the IL beside it: this tile sits in
            the same row as the IL tile on purpose. */}
        <div>
          <b>{parked ? `${y.aave_usdt_supply_apy_pct ?? '—'}%` : (y.pool_fee_return_since_entry_pct != null ? `${y.pool_fee_return_since_entry_pct}%` : '—')}</b>
          <span>{parked ? 'Aave USDT APY' : 'pool fees since entry'} · IL {pctBps(il.current_bps)}</span>
        </div>
      </div>
      <IlMeter il={il} />

      {isOwner && pending && (
        <div className="card accent" style={{ marginTop: 24 }}>
          <h3>{v.action_needed}</h3>
          {signing ? (
            <p>
              {signing.count ? `Step ${signing.index + 1} of ${signing.count}: ` : ''}{signing.title}
              <br /><span className="muted small">Confirm in your wallet. This page moves on by itself once each step lands.</span>
            </p>
          ) : (
            <>
              <button className="primary big" style={{ marginTop: 10 }} onClick={runFlow}>Sign next steps in wallet</button>
              <OpenInChat text={ask('show me what is waiting to be signed on my zap position and give me the link')} />
            </>
          )}
          {v.flow && (
            <ol className="zap-steps">
              {v.flow.steps.map((s, i) => (
                <li key={s} className={i < v.flow.step ? 'done' : i === v.flow.step ? 'now' : ''}>{s.replace(/_/g, ' ')}</li>
              ))}
            </ol>
          )}
        </div>
      )}
      {!isOwner && pending && (
        <p className="muted small" style={{ marginTop: 18 }}>
          This position has steps waiting for its owner's signature. Sign in with the owning wallet to continue.
          <button className="linkish" style={{ marginLeft: 8 }} onClick={() => load()}>I'm the owner, reload</button>
        </p>
      )}

      <div className="grid g2" style={{ marginTop: 24 }}>
        <div className="card">
          <h3>Against holding</h3>
          <div className="kv">
            <div><span>Now</span><b>{usd(value.current_usd)}</b></div>
            <div><span>Same two amounts, held (IL benchmark)</span><b>{usd(value.hold_50_50_usd)}</b></div>
            <div><span>Difference</span><b className={value.vs_hold_50_50_usd < 0 ? 'error' : 'ok'}>{signed(value.vs_hold_50_50_usd)}</b></div>
            <div><span>Original {v.deposit.asset}, untouched</span><b>{usd(value.hold_single_asset_usd)}</b></div>
            <div><span>Difference</span><b className={value.vs_hold_single_asset_usd < 0 ? 'error' : 'ok'}>{signed(value.vs_hold_single_asset_usd)}</b></div>
          </div>
          <p className="small">
            The first comparison is the impermanent-loss benchmark; the second is what
            you'd have by never zapping. Pool value includes fees earned; incentive
            rewards are paid separately by X Layer.
          </p>
        </div>
        <div className="card">
          <h3>Entry and costs</h3>
          <div className="kv">
            <div><span>Deposited</span><b>{v.deposit.amount} {v.deposit.asset}{v.deposit.usd_at_deposit != null ? ` (${usd(v.deposit.usd_at_deposit)})` : ''}</b></div>
            <div><span>Entry price</span><b>{il.p_initial != null ? `${Number(il.p_initial).toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—'}</b></div>
            <div><span>Current price</span><b>{il.p_current != null ? `${Number(il.p_current).toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—'}</b></div>
            <div><span>Price unit</span><b>{il.price_unit}</b></div>
            <div><span>Exit + re-entry cost</span><b>≈ {pctBps(c.estimated_exit_and_reentry_cost_bps)}</b></div>
          </div>
          <p className="small">{c.paired_token_tax}. {c.swap_back_float_pct_of_reserve != null && <>Pending swap-back float: {c.swap_back_float_pct_of_reserve}% of the pool's {pool.other.symbol}. {c.swap_back_note}.</>}</p>
          {c.warning && <p className="disclosure" style={{ marginTop: 10 }}>{c.warning}</p>}
        </div>
      </div>

      {isOwner && (
        <div className="card" style={{ marginTop: 18 }}>
          <h3>Control</h3>
          <div className="zap-fields">
            <label>Exit when IL exceeds (%)
              <input inputMode="decimal" placeholder={(il.exit_threshold_bps / 100).toString()} value={th.exit}
                     onChange={(e) => setTh({ ...th, exit: e.target.value })} />
            </label>
            <label>Re-enter under (%)
              <input inputMode="decimal" placeholder={(il.reentry_threshold_bps / 100).toString()} value={th.re}
                     onChange={(e) => setTh({ ...th, re: e.target.value })} />
            </label>
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 12 }}>
            <button disabled={!th.exit} onClick={() => act(() => api.zapThreshold(id, {
              il_threshold_bps: Math.round(Number(th.exit) * 100),
              reentry_threshold_bps: th.re === '' ? null : Math.round(Number(th.re) * 100),
            }), 'Thresholds updated.')}>Save thresholds</button>
            {v.state === 'in_pool' && (
              <button className="danger" onClick={() => act(() => api.zapExit(id), 'Exit queued. Sign it above.')}>Exit to Aave now</button>
            )}
            {v.state === 'parked' && (
              <button onClick={() => act(() => api.zapReenter(id), 'Re-entry queued. Sign it above.')}>Re-enter now</button>
            )}
            {v.state === 'entering' && v.flow?.step === 0 && (
              <button className="danger" onClick={() => act(() => api.zapCancel(id), 'Cancelled.')}>Cancel</button>
            )}
          </div>
          <OpenInChat text={v.state === 'in_pool'
            ? ask(`set my IL exit threshold to ${th.exit ? Math.round(Number(th.exit) * 100) : '<bps>'} bps, or exit it to Aave now if I say so`)
            : ask('change my IL thresholds')} />
        </div>
      )}

      <div className="section-label" style={{ marginTop: 32 }}>History</div>
      <ol className="zap-history">
        {[...v.history].reverse().map((e, i) => {
          const step = e.kind === 'step_confirmed';
          const label = step
            ? e.step.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
            : (EVENT_LABELS[e.kind] || e.kind);
          return (
            <li key={i} className={step ? 'is-step' : 'is-event'}>
              <span className="when">{when(e.at)}</span>
              <span className="what">
                <b>{step ? '\u2713 ' : ''}{label}</b>
                <span className="muted">
                  {e.tx_hash && <a href={txUrl(e.tx_hash)} target="_blank" rel="noreferrer">tx ↗</a>}
                  {e.il_bps != null && <> IL {pctBps(e.il_bps)}</>}
                  {e.il_bps_at_exit != null && <> IL at exit {pctBps(e.il_bps_at_exit)} · entry {Number(e.p_initial).toFixed(2)} → exit {Number(e.p_at_exit).toFixed(2)} · {e.parked}</>}
                  {e.deposited && <> {e.deposited}</>}
                </span>
              </span>
            </li>
          );
        })}
      </ol>

      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginTop: 28 }}>
        <button className="btn ghost" onClick={share}>{copied ? 'Link copied' : 'Copy share link'}</button>
        <a className="btn ghost" href={pool.explorer} target="_blank" rel="noreferrer">Pool on explorer ↗</a>
        <OpenInChat text={ask('show me my zap position')} label="check it in chat" />
      </div>
      <p className="muted small" style={{ marginTop: 10 }}>
        {v.auto_watch
          ? 'Sarf checks this position against the pool about once a minute and queues exits and re-entries on its own. Your wallet signs them.'
          : 'Automatic IL watching is switched off on this server right now; IL shown here is live, and exits can be started by hand.'}
        {' '}Owner {v.owner}.
      </p>
      <p className="disclosure" style={{ marginTop: 18 }}>{v.disclosure}</p>
    </section>
  );
}
