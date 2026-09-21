import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from './api.js';
import { OpenInChat } from './handoff.jsx';
import { signMessage } from './wallet.js';

/**
 * The account actions that used to be chat-only, for the Portfolio page:
 * xPoints, sending to another address, and stop-loss / take-profit levels.
 * Each calls the same server code as the matching chat tool.
 */

export function useXPoints() {
  const [x, setX] = useState(null);
  const reload = () => api.xpoints().then(setX).catch(() => {});
  useEffect(() => { reload(); }, []);
  return x && { ...x, reload };
}

const fmtPts = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });

/** Official xStocks xPoints. Registering is one signature in the user's own
 *  wallet over xStocks' text: no transaction, no gas. Sarf cannot sign it. */
export function XPointsPanel({ xp, address }) {
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(null);
  const o = xp?.official;
  if (!o || o.status === 'disabled') return null;

  const run = (label, fn) => async () => {
    setErr(null); setBusy(label);
    try { await fn(); await xp.reload(); } catch (e) { setErr(e.message || String(e)); } finally { setBusy(null); }
  };
  const register = run('register', async () => {
    const m = await api.xpointsRegisterMessage();
    const signature = await signMessage(address, m.message);
    await api.xpointsRegister({ signature, timestamp: m.timestamp });
  });
  const link = run('link', () => api.xpointsLink());
  const unlink = run('unlink', () => api.xpointsUnlink());

  const b = o.breakdown || {};
  return (
    <div className="card" style={{ marginTop: 22 }}>
      <h3>xPoints</h3>
      {o.status === 'ok' && (
        <>
          <p>
            <b>{fmtPts(o.total_points)}</b> xPoints from the xStocks program
            {o.season ? ` (${o.season})` : ''}. xStocks counts them once a day from what this
            wallet holds, lends and provides as liquidity.
          </p>
          <p className="muted small">
            Holding {fmtPts(b.holding)} · Lending {fmtPts(b.lending)} · Liquidity {fmtPts(b.liquidity)}
            {o.next_snapshot ? ` · next snapshot ${new Date(o.next_snapshot).toLocaleString()}` : ''}
          </p>
          <button className="linkish" disabled={!!busy} onClick={unlink}>Stop showing xPoints here</button>
        </>
      )}
      {o.status === 'not_linked' && (
        <>
          <p>
            Earn xStocks xPoints for holding and providing liquidity for tokenized stocks.
            Registering takes one signature in your wallet: no transaction and no gas.
          </p>
          <div className="cta" style={{ marginBottom: 0 }}>
            <button className="primary" disabled={!!busy} onClick={register}>
              {busy === 'register' ? 'Check your wallet…' : 'Register for xPoints'}
            </button>
            <button className="btn" disabled={!!busy} onClick={link}>
              {busy === 'link' ? 'Checking…' : 'Already registered? Show my xPoints'}
            </button>
          </div>
        </>
      )}
      {o.status === 'not_registered' && (
        <p>This wallet is not registered with xStocks yet.
          <button className="linkish" disabled={!!busy} onClick={register}>Register for xPoints</button></p>
      )}
      {o.status === 'unavailable' && (
        <p className="muted">xPoints could not be read right now ({o.reason}). Try again shortly.</p>
      )}
      {err && <p className="error" style={{ marginTop: 10 }}>{err}</p>}
    </div>
  );
}

/** Send to another address: builds the transfer, then the signer page shows
 *  the recipient in full and asks your passkey and wallet. */
export function SendPanel({ holdings }) {
  const nav = useNavigate();
  const [symbol, setSymbol] = useState(holdings[0]?.symbol || 'USDT');
  const [amount, setAmount] = useState('');
  const [to, setTo] = useState('');
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const bal = holdings.find((h) => h.symbol === symbol)?.quantity;
  const valid = Number(amount) > 0 && /^0x[0-9a-fA-F]{40}$/.test(to.trim());

  const go = async () => {
    setErr(null); setBusy(true);
    try {
      const o = await api.transferPrepare({ symbol, amount, to_address: to.trim() });
      nav(`/sign?o=${encodeURIComponent(o.order_id)}`);
    } catch (e) { setErr(e.message || String(e)); } finally { setBusy(false); }
  };

  return (
    <div className="card">
      <h3>Send</h3>
      <p>Transfers go out from your own wallet and cannot be undone. The next screen shows the full address to check before you sign.</p>
      <div className="zap-fields">
        <label>Asset
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {holdings.map((h) => <option key={h.symbol} value={h.symbol}>{h.symbol}</option>)}
          </select>
        </label>
        <label>Amount{bal != null && <span className="muted"> · you hold {bal}</span>}
          <input inputMode="decimal" placeholder="0" value={amount} onChange={(e) => setAmount(e.target.value)} />
        </label>
      </div>
      <div className="zap-fields" style={{ gridTemplateColumns: '1fr' }}>
        <label>To address
          <input className="mono" placeholder="0x…" value={to} onChange={(e) => setTo(e.target.value)} spellCheck={false} />
        </label>
      </div>
      {err && <p className="error" style={{ marginTop: 10 }}>{err}</p>}
      <div className="cta" style={{ marginBottom: 0 }}>
        <button className="primary" disabled={!valid || busy} onClick={go}>{busy ? 'Building…' : 'Review and sign'}</button>
      </div>
      <OpenInChat text={`Using Sarf, send ${amount || '<amount>'} ${symbol} to ${to.trim() || '<address>'} on X Layer.`} />
    </div>
  );
}

/** Stop-loss / take-profit levels per xStock. */
export function LevelsPanel({ symbols }) {
  const [data, setData] = useState(null);
  const [form, setForm] = useState({ symbol: symbols[0] || '', stop_loss: '', take_profit: '' });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.levels().then(setData).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  useEffect(() => { if (!form.symbol && symbols[0]) setForm((f) => ({ ...f, symbol: symbols[0] })); }, [symbols.join(',')]);

  const save = async (body) => {
    setErr(null); setBusy(true);
    try { await api.setLevels(body); await load(); setForm((f) => ({ ...f, stop_loss: '', take_profit: '' })); }
    catch (e) { setErr(e.message || String(e)); } finally { setBusy(false); }
  };

  if (!symbols.length && !data?.levels?.length) {
    return (
      <div className="card">
        <h3>Stop-loss and take-profit</h3>
        <p>Levels apply to tokenized stocks you hold. Buy one on the Swap page and set its levels here.</p>
      </div>
    );
  }
  return (
    <div className="card">
      <h3>Stop-loss and take-profit</h3>
      <p>{data?.note || 'Set a level to sell at, per asset.'}</p>
      {data?.levels?.length > 0 && (
        <div className="kv">
          {data.levels.map((l) => (
            <div key={l.symbol}>
              <span>{l.symbol}{l.price != null && <> · now ${Number(l.price).toFixed(2)}</>}</span>
              <b>
                {l.stop_loss != null && <>stop ${l.stop_loss}</>}
                {l.stop_loss != null && l.take_profit != null && ' · '}
                {l.take_profit != null && <>take ${l.take_profit}</>}
                {' '}<button className="linkish" style={{ marginLeft: 8 }} disabled={busy}
                             onClick={() => save({ symbol: l.symbol })}>Clear</button>
              </b>
            </div>
          ))}
        </div>
      )}
      <div className="zap-fields">
        <label>Asset
          <select value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })}>
            {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>Stop-loss ($)
          <input inputMode="decimal" placeholder="optional" value={form.stop_loss} onChange={(e) => setForm({ ...form, stop_loss: e.target.value })} />
        </label>
        <label>Take-profit ($)
          <input inputMode="decimal" placeholder="optional" value={form.take_profit} onChange={(e) => setForm({ ...form, take_profit: e.target.value })} />
        </label>
      </div>
      {err && <p className="error" style={{ marginTop: 10 }}>{err}</p>}
      <div className="cta" style={{ marginBottom: 0 }}>
        <button className="primary" disabled={busy || !form.symbol || (!form.stop_loss && !form.take_profit)}
                onClick={() => save(form)}>Save level</button>
      </div>
      <OpenInChat text={`Using Sarf, set a stop-loss on ${form.symbol || '<asset>'} at $${form.stop_loss || '<price>'}.`} />
    </div>
  );
}
