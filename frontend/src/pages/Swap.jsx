import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, ensureSession, getSession } from '../api.js';
import { connect, currentAccount } from '../wallet.js';
import { OpenInChat } from '../handoff.jsx';
import { ASSET_COUNT, SearchIcon, TokenMark } from '../market.jsx';

/**
 * Swap on the website, for anyone who would rather not go through a chat.
 *
 * The form quotes live as you type. "Review and sign" builds the order with
 * the same code the chat's swap tool runs, then hands it to the /sign page,
 * which shows the full terms and has your wallet sign it. Every order can
 * also be taken to Claude or ChatGPT with the same request written out.
 */

const OKB_RESERVE = 0.003; // gas coin: the build refuses to leave under 0.002
const SLIPPAGES = [0.5, 1, 2];

function TokenPicker({ tokens, value, onPick, balances, exclude }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    document.addEventListener('click', away);
    return () => document.removeEventListener('click', away);
  }, [open]);
  const t = tokens.find((x) => x.symbol === value);
  const list = tokens.filter((x) => x.symbol !== exclude && (
    !q || x.symbol.toLowerCase().includes(q.toLowerCase()) || x.name.toLowerCase().includes(q.toLowerCase())));
  return (
    <span className="picker" ref={ref}>
      <button type="button" className="picker-btn" onClick={() => setOpen((v) => !v)}>
        {t && <TokenMark asset={t} small />}
        <span>{value || 'Select'}</span>
        <span className="caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <span className="picker-menu" role="listbox">
          <label className="search" style={{ minWidth: 0 }}>
            <SearchIcon />
            <input autoFocus placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
          <span className="picker-list">
            {list.map((x) => (
              <button type="button" key={x.symbol} className="picker-item"
                      onClick={() => { onPick(x.symbol); setOpen(false); setQ(''); }}>
                <TokenMark asset={x} small />
                <span className="row-id">
                  <span className="sym">{x.symbol}</span>
                  <span className="name">{x.name.replace(' xStock', '')}</span>
                </span>
                <span className="muted small" style={{ marginLeft: 'auto' }}>{balances[x.symbol] ?? ''}</span>
              </button>
            ))}
          </span>
        </span>
      )}
    </span>
  );
}

export default function Swap() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [tokens, setTokens] = useState([]);
  const [from, setFrom] = useState(params.get('from') || 'USDT');
  const [to, setTo] = useState(params.get('to') || 'SPCXx');
  const [amount, setAmount] = useState('');
  const [slip, setSlip] = useState(1);
  const [quote, setQuote] = useState(null);
  const [quoting, setQuoting] = useState(false);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [balances, setBalances] = useState({});

  useEffect(() => { api.swapTokens().then((r) => setTokens(r.tokens)).catch((e) => setErr(e.message)); }, []);

  const loadBalances = () => {
    if (!getSession()) return;
    api.portfolio().then((p) => {
      const b = {};
      for (const x of p.positions || []) b[x.symbol] = x.quantity;
      if (p.usdt) b.USDT = p.usdt.quantity;
      if (p.usdc) b.USDC = p.usdc.quantity;
      if (p.okb) b.OKB = p.okb.quantity;
      setBalances(b);
    }).catch(() => {});
  };
  useEffect(loadBalances, []);

  // Live quote, debounced so typing does not fire a request per keystroke.
  useEffect(() => {
    setQuote(null);
    if (!(Number(amount) > 0) || from === to) return undefined;
    setQuoting(true);
    const t = setTimeout(() => {
      api.swapQuote(from, to, amount)
        .then((q) => { setQuote(q); setErr(null); })
        .catch((e) => setErr(e.message))
        .finally(() => setQuoting(false));
    }, 450);
    return () => { clearTimeout(t); setQuoting(false); };
  }, [from, to, amount]);

  const flip = () => { setFrom(to); setTo(from); setAmount(quote ? String(+quote.amount_out.toPrecision(6)) : ''); };
  const max = () => {
    const b = Number(balances[from] || 0);
    const v = from === 'OKB' ? Math.max(0, b - OKB_RESERVE) : b;
    setAmount(v ? String(v) : '');
  };

  const review = async () => {
    setErr(null);
    setBusy(true);
    try {
      const addr = (await currentAccount()) || (await connect());
      await ensureSession(addr);
      const o = await api.swapBuild({ from_symbol: from, to_symbol: to, amount, slippage_percent: slip });
      nav(`/sign?o=${encodeURIComponent(o.order_id)}`);
    } catch (e) {
      setErr(e.message || String(e));
      loadBalances();
    } finally {
      setBusy(false);
    }
  };

  const fromBal = balances[from];
  const over = fromBal != null && Number(amount) > Number(fromBal);
  const valid = Number(amount) > 0 && from !== to && !over;
  const chatText = `Using Sarf, swap ${amount || '<amount>'} ${from} for ${to} on X Layer.`;
  const impact = quote?.price_impact_percent;
  const tokenList = useMemo(() => tokens, [tokens]);

  return (
    <section className="swap-page">
      <div className="eyebrow tick">Swap on X Layer · signed in your wallet</div>
      <h1>Swap</h1>
      <p className="sub">
        Trade any of the {ASSET_COUNT} tokenized stocks and ETFs against USDT, USDC or OKB, or one
        stock straight into another. Same prices, checks and fee as asking Sarf in chat.
      </p>

      <div className="swap-card">
        <div className="swap-leg">
          <div className="swap-leg-top">
            <span className="muted small">You pay</span>
            {fromBal != null && (
              <button type="button" className="linkish small" onClick={max}>Balance {fromBal} · Max</button>
            )}
          </div>
          <div className="swap-leg-row">
            <input className="swap-amount" inputMode="decimal" placeholder="0"
                   value={amount} onChange={(e) => setAmount(e.target.value.replace(',', '.'))} />
            <TokenPicker tokens={tokenList} value={from} onPick={setFrom} balances={balances} exclude={to} />
          </div>
        </div>

        <button type="button" className="swap-flip" onClick={flip} aria-label="Swap direction">↓↑</button>

        <div className="swap-leg">
          <div className="swap-leg-top">
            <span className="muted small">You receive (estimated)</span>
            {balances[to] != null && <span className="muted small">Balance {balances[to]}</span>}
          </div>
          <div className="swap-leg-row">
            <span className="swap-amount out">
              {quoting ? '···' : quote ? (+quote.amount_out.toPrecision(8)).toString() : '0'}
            </span>
            <TokenPicker tokens={tokenList} value={to} onPick={setTo} balances={balances} exclude={from} />
          </div>
        </div>

        <div className="swap-details">
          <div><span>Rate</span><b>{quote?.rate ? `1 ${from} = ${(+quote.rate.toPrecision(6))} ${to}` : '—'}</b></div>
          <div><span>Price impact</span>
            <b className={impact != null && Math.abs(impact) > 1 ? 'error' : ''}>{impact == null ? '—' : `${Math.abs(impact).toFixed(2)}%`}</b></div>
          <div><span>Platform fee</span><b>$0.01 per swap</b></div>
          <div>
            <span>Slippage tolerance</span>
            <span className="seg small-seg">
              {SLIPPAGES.map((s) => (
                <button type="button" key={s} className={slip === s ? 'on' : ''} onClick={() => setSlip(s)}>{s}%</button>
              ))}
            </span>
          </div>
        </div>

        {over && <p className="error" style={{ marginTop: 12 }}>You hold {fromBal} {from}.</p>}
        {err && <p className="error" style={{ marginTop: 12 }}>{err}</p>}
        <button className="primary big" style={{ marginTop: 16 }} disabled={!valid || busy} onClick={review}>
          {busy ? 'Building your order…' : getSession() ? 'Review and sign' : 'Connect wallet to swap'}
        </button>
        <p className="muted small" style={{ marginTop: 10, textAlign: 'center' }}>
          Next you see the exact terms, minimum received included, and sign in your own wallet.
        </p>
        <OpenInChat text={chatText} label="or do it in chat" />
      </div>
    </section>
  );
}
