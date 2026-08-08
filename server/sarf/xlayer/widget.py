"""MCP Apps widgets — the reason a card renders in chat instead of plain text.

WHY THIS EXISTS
    A tool result is content blocks, and the host decides what to do with
    them. Returning ImageContent is valid and Sarf does emit it, but hosts are
    not obliged to display images that come back from a tool, and in practice
    the card arrived as JSON for the model to paraphrase — losing exactly the
    two lines the card existed to protect, the fee and the disclosure.

    MCP Apps is the supported path. A tool declares `_meta.ui.resourceUri`
    pointing at a `ui://` resource whose body is HTML with the MIME type
    `text/html;profile=mcp-app`. The host renders it in a sandboxed iframe and
    pushes the tool's output in as `ui/notifications/tool-result`.

PROTOCOL (spec 2026-01-26), implemented in _BOOT below
    widget -> host   ui/initialize                 (request, awaits a result)
    widget -> host   ui/notifications/initialized  (host waits for this before
                                                    sending any tool data)
    host   -> widget ui/notifications/tool-result  (the payload to render)
    widget -> host   ui/notifications/size-changed  (so the frame fits exactly)
    widget -> host   ui/message                     (a chip becomes a user turn)

NO SCROLLBARS, EVER
    The card is meant to read as one image in the transcript. A frame sized to
    a guess gets an inner scrollbar, which looks like a bug and hides the
    disclosure below the fold — the one line that must not be scrollable away.
    So the page never scrolls: overflow is hidden, and the real height is
    measured after layout and reported via size-changed. A ResizeObserver
    re-reports on every reflow, because the first measurement happens before
    fonts settle and would otherwise be a few pixels short.

INTERACTIVITY
    Each card ends in suggestion chips. A chip posts `ui/message` with
    role "user", so the host inserts it as if the user had typed it and the
    model responds in the normal flow — no hidden state, and the user can see
    exactly what was asked on their behalf.

    Chips on the analysis card are deliberately explanatory ("Explain
    effective positions"), never directive ("Sell NVDAx"). A one-tap button
    that puts a trade instruction in the user's mouth would walk straight
    through the boundary analyze_portfolio exists to hold.

XSS
    Every value is written with textContent, never innerHTML. Asset names come
    from an on-chain `name()` call, so a widget that interpolated them into
    markup would be an injection hole in a surface that also shows balances.
"""

from __future__ import annotations

# Site tokens (frontend/src/styles.css), inlined because a sandboxed iframe
# cannot reach a stylesheet.
_CSS = """
:root{
  --bg:#0b0c09; --panel:#15160f; --panel2:#1c1d14; --line:#2c2d22;
  --amber:#e8a33d; --paper:#ede6d6; --dim:#a6a190; --green:#6b9e7d; --red:#b4534a;
}
*{box-sizing:border-box;margin:0;padding:0}
/* The page itself must never scroll: the host frame is resized to fit. */
html,body{overflow:hidden}
body{
  background:var(--bg);color:var(--paper);
  font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:13px;line-height:1.5;padding:12px;
}
.card{border:1px solid var(--line);background:var(--panel);border-radius:6px;overflow:hidden}
.head{display:flex;justify-content:space-between;align-items:center;
  padding:9px 14px;border-bottom:1px solid var(--line);background:var(--panel2)}
.brand{font-weight:600;letter-spacing:.06em;font-size:11px}
.brand span{color:var(--amber)}
.tag{font-size:10px;color:var(--dim);letter-spacing:.1em}
.tag.ok{color:var(--green)}
.title{padding:13px 14px 2px;font-size:19px;font-weight:600;color:var(--amber)}
.sub{padding:0 14px 11px;color:var(--dim);font-size:11px}
.rows{padding:0 14px}
.row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.k{color:var(--dim);white-space:nowrap}
.v{font-variant-numeric:tabular-nums;text-align:right;word-break:break-word}
.v.em{color:var(--amber);font-weight:600}
.notes{padding:11px 14px;border-top:1px solid var(--line);background:var(--panel2)}
.notes h4{font-size:10px;letter-spacing:.1em;color:var(--amber);margin-bottom:5px;font-weight:600}
.notes li{color:var(--dim);font-size:11px;margin:4px 0 4px 15px}
.tips{padding:10px 14px;border-top:1px solid var(--line)}
.tip{display:flex;gap:8px;font-size:11px;color:var(--dim);margin:4px 0}
.tip b{color:var(--amber);font-weight:600;flex:none}
.chips{padding:10px 14px 12px;border-top:1px solid var(--line);
  display:flex;flex-wrap:wrap;gap:6px;background:var(--panel2)}
button.chip{
  font:inherit;font-size:11px;cursor:pointer;border:1px solid var(--line);
  background:var(--panel);color:var(--paper);padding:5px 10px;border-radius:999px;
}
button.chip:hover{border-color:var(--amber);color:var(--amber)}
button.chip.go{background:var(--amber);color:#141409;border-color:var(--amber);font-weight:600}
a.btn{background:var(--amber);color:#141409;text-decoration:none;font-weight:600;
  padding:6px 13px;border-radius:4px;font-size:11px}
.foot{padding:10px 14px;border-top:1px solid var(--line);display:flex;
  justify-content:space-between;gap:10px;align-items:center;
  font-size:11px;color:var(--dim);flex-wrap:wrap}
.bar{height:5px;background:var(--panel2);border-radius:3px;overflow:hidden;margin-top:3px}
.bar i{display:block;height:100%;background:var(--amber)}
.pos{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--line)}
.pos:first-child{border-top:0}
.sym{font-weight:600}
.muted{color:var(--dim)}
.empty{padding:18px 14px;color:var(--dim);text-align:center}
"""

_BOOT = """
const $=(t,c)=>{const e=document.createElement(t);if(c)e.className=c;return e};
// Everything user-visible goes through textContent. Asset names come from an
// on-chain name() call, so treating them as markup would be an injection hole
// in a card that also displays balances.
const txt=(e,s)=>{e.textContent=(s===null||s===undefined)?'\\u2014':String(s);return e};
let _id=100;
const post=(m)=>window.parent.postMessage(Object.assign({jsonrpc:'2.0'},m),'*');

function row(k,v,em){
  const r=$('div','row'); r.appendChild(txt($('span','k'),k));
  const val=$('span','v'+(em?' em':'')); txt(val,v); r.appendChild(val); return r;
}
function tips(list){
  const w=$('div','tips');
  list.filter(Boolean).forEach(([label,body])=>{
    const t=$('div','tip'); const b=$('b'); b.textContent=label; t.appendChild(b);
    txt(t.appendChild($('span')),body); w.appendChild(t);
  });
  return w;
}
// A chip becomes a visible user turn rather than a hidden action, so nothing
// is ever sent on the user's behalf that they cannot see in the transcript.
function chips(items){
  const w=$('div','chips');
  items.filter(Boolean).forEach(([label,text,go])=>{
    const b=$('button','chip'+(go?' go':'')); b.textContent=label;
    b.onclick=()=>post({id:++_id,method:'ui/message',
      params:{role:'user',content:{type:'text',text:text}}});
    w.appendChild(b);
  });
  return w;
}

// Report the true content height so the host frame fits exactly and no inner
// scrollbar appears. Measured after layout, and again on every reflow: the
// first measurement lands before fonts settle and is a few pixels short.
function fit(){
  const h=Math.ceil(document.documentElement.scrollHeight);
  const w=Math.ceil(document.documentElement.scrollWidth);
  post({method:'ui/notifications/size-changed',params:{width:w,height:h}});
}
const _fit=()=>requestAnimationFrame(()=>requestAnimationFrame(fit));
if(window.ResizeObserver) new ResizeObserver(_fit).observe(document.documentElement);
window.addEventListener('load',_fit);

function payload(p){
  if(!p) return null;
  if(p.structuredContent) return p.structuredContent;
  for(const b of (p.content||[])){
    if(b&&b.type==='text'){ try{ return JSON.parse(b.text); }catch(e){} }
  }
  return null;
}
window.addEventListener('message',(ev)=>{
  const m=ev.data; if(!m) return;
  if(m.method==='ui/notifications/tool-result'){
    const d=payload(m.params);
    if(d){ try{ render(d); }catch(e){ txt(document.getElementById('root'),
      'This result could not be rendered; the details are in the message above.'); } }
    _fit();
  }
});
// Handshake: initialize, then announce ready. The host holds tool data until
// the initialized notification arrives, so skipping it means a blank card.
post({id:1,method:'ui/initialize',params:{capabilities:{},
  clientInfo:{name:'sarf',version:'1'},protocolVersion:'2026-01-26'}});
post({method:'ui/notifications/initialized'});
_fit();
"""


def _page(body_js: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<style>{_CSS}</style></head><body><div id='root'></div>"
        f"<script>{_BOOT}\n{body_js}</script></body></html>"
    )


ORDER_CARD = _page("""
function render(d){
  const root=document.getElementById('root'); root.innerHTML='';
  const card=$('div','card');
  const done=!!d.tx_hash;

  const head=$('div','head');
  const b=$('div','brand'); b.textContent='SARF '; const s=$('span');
  s.textContent='/ X LAYER RWA'; b.appendChild(s); head.appendChild(b);
  const tag=$('div','tag'+(done?' ok':'')); txt(tag,done?'SETTLED':'REVIEW & SIGN');
  head.appendChild(tag); card.appendChild(head);

  txt(card.appendChild($('div','title')),
      String(d.side||'').toUpperCase()+' '+(d.symbol||''));
  txt(card.appendChild($('div','sub')), (d.name||'').replace(' xStock',''));

  const fee=d.platform_fee||{};
  const rows=$('div','rows');
  rows.appendChild(row('You pay', d.spending));
  rows.appendChild(row(done?'You received':'You receive (est.)', d.receiving_estimated));
  if(d.minimum_received&&!done) rows.appendChild(row('Minimum received', d.minimum_received));
  if(d.estimated_usd!=null) rows.appendChild(row('Order value','$'+Number(d.estimated_usd).toFixed(2)));
  rows.appendChild(row('Platform fee',
    fee.charged?('$'+Number(fee.usd||0).toFixed(2)+' '+(fee.denominated_in||'')):'none', fee.charged));
  if(d.price_impact_percent!=null)
    rows.appendChild(row('Price impact', Number(d.price_impact_percent).toFixed(2)+'%'));
  rows.appendChild(row('Network','X Layer \\u00b7 '+(d.chain_id||196)+' \\u00b7 gas in OKB'));
  card.appendChild(rows);

  const notes=(d.risk_notes||[]).filter(Boolean);
  if(notes.length){
    const n=$('div','notes'); const h=$('h4'); h.textContent='READ BEFORE SIGNING';
    n.appendChild(h); const ul=$('ul');
    notes.slice(0,4).forEach(t=>txt(ul.appendChild($('li')),t));
    n.appendChild(ul); card.appendChild(n);
  }

  // Tips react to this order rather than being boilerplate — a fixed tip
  // strip is wallpaper users learn to skip.
  const t=[];
  if(d.price_impact_percent!=null&&Number(d.price_impact_percent)>=1)
    t.push(['Impact','This order moves the pool by more than 1%. Splitting it into '+
      'smaller trades usually fills closer to the quote.']);
  if(d.minimum_received&&!done)
    t.push(['Floor','If the fill would land under '+d.minimum_received+
      ', the transaction reverts and you keep your funds. Slippage costs you a '+
      'failed trade, never a bad one.']);
  if(!done) t.push(['Expiry','Quotes go stale. If you leave this and come back, '+
    'ask again rather than signing an old one.']);
  if(fee.charged) t.push(['Fee','The $'+Number(fee.usd||0).toFixed(2)+
    ' is taken inside this same transaction — there is no second transfer and no '+
    'custody step.']);
  if(t.length) card.appendChild(tips(t.slice(0,3)));

  const foot=$('div','foot');
  if(done){
    txt(foot.appendChild($('span')),'Settled on X Layer');
    if(d.explorer_url){const a=$('a','btn');a.href=d.explorer_url;a.target='_blank';
      a.textContent='View transaction';foot.appendChild(a);}
  } else {
    txt(foot.appendChild($('span')),'Unsigned \\u2014 Sarf holds no keys and cannot execute it.');
    if(d.sign_url){const a=$('a','btn');a.href=d.sign_url;a.target='_blank';
      a.textContent='Review & sign';foot.appendChild(a);}
  }
  card.appendChild(foot);

  const sym=d.symbol||'';
  card.appendChild(chips(done
    ? [['Check settlement','Check the settlement status of '+d.tx_hash],
       ['My holdings','Show my portfolio'],
       ['Analyse it','Analyse my portfolio']]
    : [d.can_execute?['Execute now','Execute order '+d.order_id,true]:null,
       ['Half the size','Redo that order at half the size'],
       ['Why this price?','Explain how this price and the minimum received were worked out'],
       ['My holdings','Show my portfolio']]));

  root.appendChild(card); _fit();
}
""")


PORTFOLIO_CARD = _page("""
function render(d){
  const root=document.getElementById('root'); root.innerHTML='';
  const card=$('div','card');

  const head=$('div','head');
  const b=$('div','brand'); b.textContent='SARF '; const s=$('span');
  s.textContent='/ HOLDINGS'; b.appendChild(s); head.appendChild(b);
  txt(head.appendChild($('div','tag')),'X LAYER \\u00b7 196'); card.appendChild(head);

  const ps=(d.positions||[]).slice().sort((a,b)=>(b.value_usd||0)-(a.value_usd||0));
  const total=d.total_value_usd!=null?d.total_value_usd:d.positions_value_usd;
  txt(card.appendChild($('div','title')), total!=null
    ? '$'+Number(total).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})
    : '\\u2014');
  txt(card.appendChild($('div','sub')),
    ps.length+' position'+(ps.length===1?'':'s')+' \\u00b7 '+
    (d.usdt_balance||'0')+' USDT \\u00b7 '+(d.gas_balance_okb||'0')+' OKB gas');

  if(!ps.length){
    txt(card.appendChild($('div','empty')),'No tokenized stocks in this wallet yet.');
  } else {
    const rows=$('div','rows');
    ps.slice(0,8).forEach(p=>{
      const r=$('div','pos');
      const l=$('div');
      txt(l.appendChild($('div','sym')),p.symbol);
      txt(l.appendChild($('div','muted')),p.quantity);
      const rr=$('div'); rr.style.textAlign='right'; rr.style.minWidth='110px';
      txt(rr.appendChild($('div')),p.value_usd!=null?('$'+Number(p.value_usd).toFixed(2)):'unpriced');
      txt(rr.appendChild($('div','muted')),
        p.price_usdt!=null?('$'+Number(p.price_usdt).toFixed(2)+' ea'):'');
      const w=total?Math.round((p.value_usd||0)/total*100):0;
      const bar=$('div','bar'); const i=$('i'); i.style.width=Math.min(100,w)+'%';
      bar.appendChild(i); rr.appendChild(bar);
      r.appendChild(l); r.appendChild(rr); rows.appendChild(r);
    });
    card.appendChild(rows);
    if(ps.length>8) txt(card.appendChild($('div','sub')),'+ '+(ps.length-8)+' more');
  }

  const t=[];
  if(parseFloat(d.gas_balance_okb||'0')<=0)
    t.push(d.gas_sponsored
      ? ['Gas','No OKB here, but trades you place in chat run under your session '+
         'grant and Sarf pays the gas. Signing one yourself would still need OKB.']
      : ['Gas','This wallet has no OKB. Gas on X Layer is paid in OKB, so no '+
         'transaction can be sent until some is topped up \\u2014 whatever the holdings are worth.']);
  if((d.unpriced_positions||[]).length)
    t.push(['Not priced',(d.unpriced_positions||[]).join(', ')+
      ' could not be quoted just now and are excluded from the total.']);
  if(ps.length===1) t.push(['Breadth','One position means the whole wallet moves with '+
    'one price. Concentration is measurable \\u2014 ask for an analysis.']);
  if(t.length) card.appendChild(tips(t.slice(0,2)));

  const foot=$('div','foot');
  txt(foot.appendChild($('span')),
    'Synthetic exposure \\u2014 no ownership, dividends, or voting rights.');
  card.appendChild(foot);

  card.appendChild(chips([
    ps.length?['Analyse this','Analyse my portfolio']:null,
    ['What can I buy?','What tokenized stocks can I buy on X Layer?'],
    ps.length?['Recent orders','Show my recent orders']:null,
    ['Prices','What are the current prices of the assets I hold?']
  ]));
  root.appendChild(card); _fit();
}
""")


ANALYSIS_CARD = _page("""
function render(d){
  const root=document.getElementById('root'); root.innerHTML='';
  const card=$('div','card');

  const head=$('div','head');
  const b=$('div','brand'); b.textContent='SARF '; const s=$('span');
  s.textContent='/ ANALYSIS'; b.appendChild(s); head.appendChild(b);
  txt(head.appendChild($('div','tag')),'INFORMATIONAL ONLY'); card.appendChild(head);

  const c=d.concentration||{};
  txt(card.appendChild($('div','title')),
    c.effective_positions!=null?(c.effective_positions+' effective positions'):'Portfolio');
  txt(card.appendChild($('div','sub')),
    (d.position_count||0)+' held \\u00b7 largest '+(c.largest_position||'\\u2014')+
    (c.largest_position_percent!=null?(' at '+c.largest_position_percent+'%'):''));

  const sectors=(d.composition&&d.composition.by_sector)||[];
  if(sectors.length){
    const rows=$('div','rows');
    sectors.slice(0,5).forEach(x=>{
      const r=$('div','pos');
      txt(r.appendChild($('div','sym')),x.sector);
      const rr=$('div'); rr.style.textAlign='right'; rr.style.minWidth='90px';
      txt(rr.appendChild($('div')),x.weight_percent+'%');
      const bar=$('div','bar'); const i=$('i');
      i.style.width=Math.min(100,x.weight_percent)+'%'; bar.appendChild(i);
      rr.appendChild(bar); r.appendChild(rr); rows.appendChild(r);
    });
    card.appendChild(rows);
  }

  const f=(d.findings||[]);
  if(f.length){
    const n=$('div','notes'); const h=$('h4'); h.textContent='OBSERVATIONS';
    n.appendChild(h); const ul=$('ul');
    // Fact and norm are joined by a dash and nothing else. The widget must not
    // editorialise them into a recommendation — that boundary is the whole
    // reason analyze_portfolio is allowed to exist.
    f.slice(0,5).forEach(x=>txt(ul.appendChild($('li')),
      x.observation+(x.reference_point?(' \\u2014 '+x.reference_point):'')));
    n.appendChild(ul); card.appendChild(n);
  }

  card.appendChild(tips([
    ['Reading it','Effective positions is 1/HHI \\u2014 how many equally sized holdings '+
      'would give this much concentration. It is what diversification a wallet has, '+
      'not how many tickers it lists.'],
    ['Missing','This sees only on-chain holdings \\u2014 not your income, other accounts, '+
      'goals, horizon or risk tolerance, which are what decide whether any allocation '+
      'suits you.']
  ]));

  const foot=$('div','foot');
  txt(foot.appendChild($('span')),
    'Not personalised investment advice. Sarf is not a licensed adviser.');
  card.appendChild(foot);

  // Explanatory, never directive. A one-tap "Sell NVDAx" would put a trade
  // instruction in the user's mouth and walk straight through the boundary
  // this tool exists to hold.
  card.appendChild(chips([
    ['Explain the maths','Explain how effective positions and the Herfindahl index are calculated'],
    ['Sector detail','Break down my holdings by sector in more detail'],
    ['My holdings','Show my portfolio'],
    ['What else is tradable?','What tokenized stocks can I buy on X Layer?']
  ]));
  root.appendChild(card); _fit();
}
""")


WIDGETS = {
    "ui://sarf/order-card": ("sarf_order_card", ORDER_CARD,
                             "Order card — amounts, platform fee, risks, tips, sign or execute"),
    "ui://sarf/portfolio-card": ("sarf_portfolio_card", PORTFOLIO_CARD,
                                 "Holdings card — positions, weights, cash and gas, next steps"),
    "ui://sarf/analysis-card": ("sarf_analysis_card", ANALYSIS_CARD,
                                "Analysis card — concentration, sectors, observations, how to read it"),
}

UI_MIME = "text/html;profile=mcp-app"
