import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { currentAccount, short } from '../wallet.js';

/**
 * Portfolio: holdings plus the analysis, for any address.
 *
 * Two sources feed the same view. A pasted address goes through the public
 * read-only endpoint — no session, no signature, nothing granted. Your own
 * address, once signed in, goes through the session-bound one. Same renderer
 * either way, because they are the same numbers read from the same chain.
 *
 * On the wording: every finding here is rendered as an observation next to the
 * reference point it is measured against, and never as an instruction. That is
 * not a copy decision made on this page — analysis.py emits `observation` and
 * `reference_point` as separate fields precisely so no renderer can join them
 * into "sell NVDAx". Sarf is not a licensed adviser and this is not advice.
 */
export default function Portfolio() {
  const [params, setParams] = useSearchParams();
  const queried = params.get('a') || '';

  const [input, setInput] = useState(queried);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showAll, setShowAll] = useState(false);

  // Own holdings: only when signed in and no address is being inspected.
  const loadMine = async () => {
    setErr(null); setBusy(true);
    try {
      const addr = await currentAccount();
      if (!addr) throw new Error('Sign in to see your own holdings, or paste an address above.');
      await ensureSession(addr);
      setData(await api.portfolio());
    } catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(false); }
  };

  const loadPublic = async (addr) => {
    setErr(null); setBusy(true); setData(null);
    try {
      setData(await api.publicPortfolio(addr));
    } catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    if (queried) loadPublic(queried);
    else if (getSession()) loadMine();
    else { setData(null); setErr(null); }
  }, [queried]);

  const submit = (e) => {
    e.preventDefault();
    const a = input.trim();
    // Reflected in the URL so a read is shareable — the whole point of a
    // lookup that needs no account is that you can send someone the result.
    if (a) setParams({ a });
    else setParams({});
  };

  const analysis = data?.analysis;
  const weights = analysis?.weights || [];
  const findings = analysis?.findings || [];
  const conc = analysis?.concentration || {};
  const positions = data?.positions || [];
  const unpriced = data?.unpriced_positions || [];

  // Meter reads top-3 share of the equity sleeve; it is a measurement, not a
  // score, so the label under it names the number rather than a verdict.
  const top3 = Number(conc.top_3_percent ?? 0);
  const visible = showAll ? weights : weights.slice(0, 8);

  return (
    <section>
      <div className="eyebrow tick">Paste an address, get a read</div>
      <h1>What's in the portfolio.</h1>
      <p className="sub">
        Sarf reads any X Layer address and measures it — concentration, sector
        mix, cash buffer — against the reference points those measures are
        normally judged by. No wallet connection needed to run it.
      </p>

      <form className="input-row" onSubmit={submit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Paste an X Layer address (0x…)"
          spellCheck="false"
        />
        <button className="go" type="submit" disabled={busy}>
          {busy ? '…' : 'Analyze'}
        </button>
      </form>
      <div className="hint">
        Read-only. Reading an address grants nothing and moves nothing — it is
        the same public state any block explorer will show you.
        {queried && getSession() && (
          <> · <a href="#" onClick={(e) => { e.preventDefault(); setInput(''); setParams({}); }}>
            back to my holdings
          </a></>
        )}
      </div>

      {err && <p className="error" style={{ marginTop: 18 }}>{err}</p>}
      {busy && !data && <p className="muted small" style={{ marginTop: 18 }}>Reading X Layer…</p>}

      {data && (
        <>
          <p className="muted small" style={{ marginTop: 18 }}>
            {short(data.address)} · read live from X Layer
            {data.analysis ? '' : ' · sign in for the full analysis'}
          </p>

          <div className="stats">
            <div>
              <b>{data.total_value_usd != null ? `$${Number(data.total_value_usd).toLocaleString()}` : '—'}</b>
              <span>total value</span>
            </div>
            <div><b>${Number(data.positions_value_usd || 0).toLocaleString()}</b><span>positions</span></div>
            <div><b>{data.usdt_balance}</b><span>USDT</span></div>
            <div><b>{Number(data.gas_balance_okb || 0).toFixed(5)}</b><span>OKB gas</span></div>
          </div>

          {unpriced.length > 0 && (
            // Never let a pricing outage read as "these are worth nothing".
            <div className="disclosure">
              <b>{unpriced.join(', ')}</b> could not be priced right now, so the total
              above excludes them. They are still held — this is a quote outage, not a
              zero balance.
            </div>
          )}

          {weights.length > 0 && (
            <div className="risk-band">
              <span className="risk-label">Top 3 concentration</span>
              <div className="risk-meter" style={{ '--fill': `${Math.min(100, top3)}%` }} />
              <span className="risk-value">{top3.toFixed(0)}%</span>
            </div>
          )}

          {findings.length > 0 && (
            <>
              <div className="section-label">What the numbers show</div>
              {findings.map((f, i) => (
                <div className={`card${f.reference_point ? ' accent' : ''}`} key={i}>
                  <p>{f.observation}</p>
                  {f.reference_point && <div className="norm">Measured against: {f.reference_point}</div>}
                </div>
              ))}
            </>
          )}

          {weights.length > 0 && (
            <>
              <div className="section-label">Holdings</div>
              <div className="ledger">
                {visible.map((w) => (
                  <div className="row" key={w.symbol}>
                    <div className="row-left">
                      <span className="sym">{w.symbol}</span>
                      <span className="name">{w.sector || w.name || ''}</span>
                    </div>
                    <div className="row-right">
                      <span className="price">${Number(w.value_usd).toLocaleString()}</span>
                      <span className="weight">{Number(w.weight_percent).toFixed(0)}%</span>
                      {/* States the band, not a verdict on what to do about it. */}
                      <span className={`band${Number(w.weight_percent) > 20 ? ' over' : ''}`}>
                        {Number(w.weight_percent) > 20 ? 'above 15–20% band' : 'within band'}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
              {weights.length > 8 && (
                <button className="see-all" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? 'Show fewer' : `Show all ${weights.length} positions →`}
                </button>
              )}
            </>
          )}

          {weights.length === 0 && positions.length === 0 && (
            <p className="muted small" style={{ marginTop: 20 }}>
              No tokenized stock positions at this address.
            </p>
          )}

          {analysis && (
            <p className="fine" style={{ marginTop: 24 }}>
              <strong style={{ color: 'var(--paper)' }}>Informational only.</strong>{' '}
              {analysis.disclosure} {analysis.missing_context}
            </p>
          )}
        </>
      )}

      {!data && !busy && !err && (
        <p className="muted small" style={{ marginTop: 24 }}>
          Paste an address above, or sign in to read your own.
        </p>
      )}
    </section>
  );
}
