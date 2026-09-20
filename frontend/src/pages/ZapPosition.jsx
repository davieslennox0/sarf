import React, { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount, sendTransaction, txUrl } from '../wallet.js';
import { OpenInChat } from '../handoff.jsx';
import { Fact, IL_TONE_LABEL, IlRing, PairMark, ilTone, num, pct, usd } from '../zapui.jsx';

/**
 * One zap position. Public and bookmarkable: anyone with the link sees IL,
 * value against holding, and the exit/re-entry history, which is all readable
 * on-chain anyway. Acting on it (signing steps, moving thresholds, exiting)
 * needs the owner's wallet session, and every action also offers the same
 * request as a prefilled chat.
 *
 * Laid out like a lending reserve page: what the position IS on the left,
 * ranked with the one number that matters made graphic, and what you can DO
 * about it in a panel that stays with you on the right.
 */

const REFRESH_MS = 15000;
const signed = (x) => (x == null ? '—' : `${x >= 0 ? '+' : '−'}$${Math.abs(x).toFixed(2)}`);
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

/** The linear view of the same thing the ring shows, with both lines on it. */
function IlMeter({ il }) {
  const max = Math.max(il.exit_threshold_bps * 1.5, (il.current_bps || 0) * 1.1, 1);
  const at = (v) => `${Math.min(100, (v / max) * 100)}%`;
  const tone = ilTone(il.current_bps, il.exit_threshold_bps);
  return (
    <>
      <div className="il-meter" aria-label="Impermanent loss against thresholds">
        <div className={`il-fill ${tone}`} style={{ width: at(il.current_bps || 0) }} />
        <div className="il-mark re" style={{ left: at(il.reentry_threshold_bps) }} title="re-entry line" />
        <div className="il-mark ex" style={{ left: at(il.exit_threshold_bps) }} title="exit line" />
      </div>
      <div className="il-legend">
        <span><i className="dot re" /> re-enter under {pct(il.reentry_threshold_bps)}</span>
        <span><i className="dot ex" /> exit past {pct(il.exit_threshold_bps)}</span>
      </div>
    </>
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
  const tone = ilTone(il.current_bps, il.exit_threshold_bps);
  // A round trip is only worth flagging when it could eat the loss it exists
  // to avoid: the band between the two lines is what one exit-and-re-entry
  // cycle is meant to save, so a cycle costing more than that band is the
  // number that decides whether acting now makes any sense.
  const roundTrip = c.estimated_exit_and_reentry_cost_bps;
  const band = il.exit_threshold_bps - il.reentry_threshold_bps;
  const roundTripBites = roundTrip != null && band != null && roundTrip >= band;
  const e = v.earnings || {};
  const acc = e.accrues_into_the_position || {};
  const paid = e.paid_separately;
  const closed = v.state === 'closed';
  // Yield never appears without the IL beside it. That is a rule about this
  // feature, not a layout preference: a yield figure on its own is the half
  // of the story that sells.
  const yieldValue = parked
    ? (y.aave_usdt_supply_apy_pct != null ? `${y.aave_usdt_supply_apy_pct}%` : '—')
    : (y.pool_fee_return_since_entry_pct != null ? `${y.pool_fee_return_since_entry_pct}%` : '—');

  return (
    <section className="zap">
      <Link className="backlink" to="/zap">← All zap pools</Link>

      <div className="market-head">
        <div className="pos-id">
          <PairMark a={pool.other.symbol} b={pool.rwa.symbol} />
          <div>
            <h1>
              {pool.pair}
              <span className={`chip${pending ? ' accent' : ''}`}>{v.state.replace(/_/g, ' ')}</span>
              {il.current_bps != null && (
                <span className={`status-pill ${tone}`}>{IL_TONE_LABEL[tone]}</span>
              )}
            </h1>
            <p className="sub">{v.state_label}</p>
          </div>
        </div>
        <div className="market-stats">
          <div><b>{usd(value.current_usd)}</b><span>{parked ? 'parked in Aave' : 'position value'}</span></div>
          <div><b className={il.current_bps > il.exit_threshold_bps ? 'bad' : ''}>{pct(il.current_bps)}</b><span>impermanent loss</span></div>
          <div><b>{yieldValue}</b><span>{parked ? 'Aave USDT APY' : 'pool fees since entry'}</span></div>
        </div>
      </div>

      {err && <p className="error" style={{ marginTop: 14 }}>{err}</p>}
      {note && <p className="ok" style={{ marginTop: 14 }}>{note}</p>}

      <div className="pos-grid">
        <div className="pos-main">
          <div className="card">
            <h3>Where this position stands</h3>
            <div className="il-block">
              <IlRing currentBps={il.current_bps} exitBps={il.exit_threshold_bps} />
              <div className="il-said">
                <p className="zap-headline">{v.headline}</p>
                <div className="kv tight">
                  <div><span>Entry price</span><b>{num(il.p_initial, 2)} {il.price_unit}</b></div>
                  <div><span>Pool price now</span><b>{num(il.p_current, 2)} {il.price_unit}</b></div>
                </div>
              </div>
            </div>
            <IlMeter il={il} />
            {roundTripBites && (
              <div className="callout warning" style={{ marginTop: 20 }}>
                <span className="callout-k">Round trip</span>
                <b className="callout-v">{pct(roundTrip)}</b>
                <span className="callout-s">out and back, in fees and tax</span>
              </div>
            )}
            <div className="dp-facts" style={{ marginTop: roundTripBites ? 10 : 20 }}>
              <Fact label="Exit line" value={pct(il.exit_threshold_bps)} sub="moves you to Aave" />
              <Fact label="Re-entry line" value={pct(il.reentry_threshold_bps)} sub="brings you back in" />
              {!roundTripBites && (
                <Fact label="Round trip" value={pct(roundTrip)} sub="out and back, in fees and tax" />
              )}
              <Fact label="Deposited" value={`${v.deposit.amount} ${v.deposit.asset}`}
                    sub={v.deposit.usd_at_deposit != null ? usd(v.deposit.usd_at_deposit) : undefined} />
            </div>
          </div>

          {/* What it has earned, split by whether it needs collecting. Fees and
              Aave interest accrue into the position and need no claim; the X
              Layer incentive is paid by OKX and does not. */}
          <div className="card" style={{ display: paid ? undefined : 'none' }}>
            <h3>What this is earning</h3>
            <div className="dp-facts" style={{ marginTop: 4 }}>
              <Fact label="Pool fees since entry"
                    value={acc.pool_fees_usd != null ? usd(acc.pool_fees_usd) : (acc.pool_fee_return_pct != null ? `${acc.pool_fee_return_pct}%` : '—')}
                    sub={acc.pool_fee_apr_pct_measured != null
                      ? `${acc.pool_fee_apr_pct_measured}% APR measured · IL ${pct(il.current_bps)}`
                      : `IL ${pct(il.current_bps)}`} />
              {parked && (
                <Fact label="Aave interest"
                      value={acc.aave_interest_usd != null ? usd(acc.aave_interest_usd) : '—'}
                      sub={`${acc.aave_supply_apy_pct ?? '—'}% APY · IL ${pct(il.current_bps)}`} />
              )}
              <Fact label={closed ? 'Realised' : 'Accrued so far'}
                    value={closed ? usd(e.realized_usd) : (e.total_accrued_usd != null ? usd(e.total_accrued_usd) : '—')}
                    sub={closed ? 'in your wallet' : 'already inside the position value'} />
              {paid && (
                <Fact label="Incentives received"
                      value={paid.received_usdg > 0 ? `${paid.received_usdg} USDG` : '0 USDG'}
                      sub={paid.drop_count > 0
                        ? `${paid.drop_count} payment${paid.drop_count === 1 ? '' : 's'} · IL ${pct(il.current_bps)}`
                        : `none has landed yet · IL ${pct(il.current_bps)}`} />
              )}
            </div>
            <p className="small">{acc.note}.</p>

            {paid && <div className="reward-panel">
              <div className="reward-head">
                <div>
                  <h4>{paid.programme}</h4>
                  <p className="muted small">{paid.pot}</p>
                </div>
                <a className="btn primary" href={paid.claim.url} target="_blank" rel="noreferrer">
                  Claim at OKX ↗
                </a>
              </div>
              {/* Received, not projected. Every figure here is a transfer
                  with a hash behind it. */}
              <div className="reward-total">
                <b>{paid.received_usdg > 0 ? `${paid.received_usdg} USDG` : 'Nothing yet'}</b>
                <span>
                  {paid.drop_count > 0
                    ? `received in ${paid.drop_count} payment${paid.drop_count === 1 ? '' : 's'} since you entered`
                    : 'no incentive payment has reached this wallet yet'}
                </span>
              </div>
              {paid.drops?.length > 0 && (
                <ol className="drop-list">
                  {paid.drops.map((d) => (
                    <li key={`${d.tx_hash}-${d.block}`}>
                      <b>+{d.amount} {d.symbol}</b>
                      <span className="muted">block {d.block}</span>
                      <a href={txUrl(d.tx_hash)} target="_blank" rel="noreferrer">tx ↗</a>
                    </li>
                  ))}
                </ol>
              )}
              <div className="kv tight">
                <div><span>Window</span><b>{paid.window}</b></div>
                <div><span>Your share of this pool</span>
                  <b>{paid.your_pool_share_pct != null ? `${paid.your_pool_share_pct}%` : '—'}</b></div>
                <div><span>Paid by</span><b>X Layer, in USDG, on OKX's side</b></div>
              </div>
              <p className="small">
                {paid.why_no_owed_amount}. {paid.claim.note}.
              </p>
              <ol className="claim-steps">
                {paid.claim.steps.map((st) => <li key={st}>{st}</li>)}
              </ol>
              <p className="muted small">
                <a href={paid.claim.instructions_url} target="_blank" rel="noreferrer">OKX's instructions ↗</a>
                {' · '}
                <a href={paid.claim.programme_url} target="_blank" rel="noreferrer">programme terms ↗</a>
              </p>
            </div>}
          </div>

          <div className="card">
            <h3>Against simply holding</h3>
            <div className="kv kv-figures">
              <div><span>This position now</span><b>{usd(value.current_usd)}</b></div>
              <div><span>Same two amounts, held (the IL benchmark)</span><b>{usd(value.hold_50_50_usd)}</b></div>
              <div><span>Difference</span><b className={value.vs_hold_50_50_usd < 0 ? 'error' : 'ok'}>{signed(value.vs_hold_50_50_usd)}</b></div>
              <div><span>Original {v.deposit.asset}, never zapped</span><b>{usd(value.hold_single_asset_usd)}</b></div>
              <div><span>Difference</span><b className={value.vs_hold_single_asset_usd < 0 ? 'error' : 'ok'}>{signed(value.vs_hold_single_asset_usd)}</b></div>
            </div>
            <p className="small">
              The first comparison is the impermanent-loss benchmark; the second is what you
              would have by never zapping at all. Pool value includes fees earned; incentive
              rewards are paid separately by X Layer.
            </p>
          </div>

          <div className="card">
            <h3>Costs of this pool</h3>
            <p className="small" style={{ marginTop: 0 }}>
              {c.paired_token_tax}.{' '}
              {c.swap_back_float_pct_of_reserve != null && (
                <>Pending swap-back float: {c.swap_back_float_pct_of_reserve}% of the pool's{' '}
                  {pool.other.symbol}. {c.swap_back_note}.</>
              )}
            </p>
            {c.warning && <p className="disclosure" style={{ marginTop: 10 }}>{c.warning}</p>}
          </div>

          <div className="section-label">History</div>
          <ol className="zap-history">
            {[...v.history].reverse().map((e, i) => {
              const step = e.kind === 'step_confirmed';
              const label = step
                ? e.step.replace(/_/g, ' ').replace(/^./, (ch) => ch.toUpperCase())
                : (EVENT_LABELS[e.kind] || e.kind);
              return (
                <li key={i} className={step ? 'is-step' : 'is-event'}>
                  <span className="when">{when(e.at)}</span>
                  <span className="what">
                    <b>{step ? '✓ ' : ''}{label}</b>
                    <span className="muted">
                      {e.tx_hash && <a href={txUrl(e.tx_hash)} target="_blank" rel="noreferrer">tx ↗</a>}
                      {e.il_bps != null && <> IL {pct(e.il_bps)}</>}
                      {e.il_bps_at_exit != null && <> IL at exit {pct(e.il_bps_at_exit)} · entry {Number(e.p_initial).toFixed(2)} → exit {Number(e.p_at_exit).toFixed(2)} · {e.parked}</>}
                      {e.deposited && <> {e.deposited}</>}
                    </span>
                  </span>
                </li>
              );
            })}
          </ol>
        </div>

        {/* Everything you can do, in one place that follows you down the page. */}
        <aside className="pos-side">
          {isOwner && pending && (
            <div className="card accent">
              {/* The server phrases this for chat, where "on the position
                  page" is the whole point. Here it is where you already are. */}
              <h3>{v.action_needed.replace(/ on the position page$/, '')}</h3>
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
            <div className="card">
              <h3>Waiting on its owner</h3>
              <p>This position has steps waiting for a signature. Sign in with the owning wallet to continue.</p>
              <button style={{ marginTop: 10 }} onClick={() => load()}>I'm the owner, reload</button>
            </div>
          )}

          {isOwner && (
            <div className="card">
              <h3>Your lines</h3>
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
              <div className="side-actions">
                <button disabled={!th.exit} onClick={() => act(() => api.zapThreshold(id, {
                  il_threshold_bps: Math.round(Number(th.exit) * 100),
                  reentry_threshold_bps: th.re === '' ? null : Math.round(Number(th.re) * 100),
                }), 'Thresholds updated.')}>Save lines</button>
                {v.state === 'in_pool' && (
                  <button className="danger" onClick={() => act(() => api.zapExit(id), 'Exit queued. Sign it above.')}>Exit to Aave now</button>
                )}
                {v.state === 'parked' && (
                  <button onClick={() => act(() => api.zapReenter(id), 'Re-entry queued. Sign it above.')}>Re-enter now</button>
                )}
                {v.state === 'entering' && v.flow?.step === 0 && (
                  <button className="danger" onClick={() => act(() => api.zapCancel(id), 'Cancelled.')}>Cancel</button>
                )}
                {['in_pool', 'parked'].includes(v.state) && (
                  <button className="danger" onClick={() => act(() => api.zapClose(id),
                    'Close queued. Sign it above and the proceeds land in your wallet.')}>
                    Close and withdraw
                  </button>
                )}
              </div>
              <OpenInChat text={v.state === 'in_pool'
                ? ask(`set my IL exit threshold to ${th.exit ? Math.round(Number(th.exit) * 100) : '<bps>'} bps, or exit it to Aave now if I say so`)
                : ask('change my IL thresholds')} />
            </div>
          )}

          <div className="card">
            <h3>This position</h3>
            <div className="side-actions">
              <button className="btn ghost" onClick={share}>{copied ? 'Link copied' : 'Copy share link'}</button>
              <a className="btn ghost" href={pool.explorer} target="_blank" rel="noreferrer">Pool on explorer ↗</a>
            </div>
            <OpenInChat text={ask('show me my zap position')} label="check it in chat" />
            <p className="muted small" style={{ marginTop: 12 }}>
              {v.auto_watch
                ? 'Sarf checks this against the pool about once a minute and queues exits and re-entries on its own. Your wallet signs them.'
                : 'Automatic IL watching is off on this server right now; IL shown here is live, and exits can be started by hand.'}
            </p>
          </div>
        </aside>
      </div>

      <p className="disclosure" style={{ marginTop: 22 }}>{v.disclosure}</p>
    </section>
  );
}
