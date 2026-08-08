"""MCP Apps widgets — the reason a card renders in chat instead of plain text.

WHY THIS EXISTS
    A tool result is content blocks, and the host decides what to do with
    them. Returning ImageContent is valid and Sarf does emit it, but hosts are
    not obliged to display images that come back from a tool, and in practice
    the card arrived as JSON for the model to paraphrase — losing exactly the
    two lines the card existed to protect, the fee and the disclosure.

    MCP Apps is the supported path. A tool declares `_meta.ui.resourceUri`
    pointing at a `ui://` resource whose body is HTML with the MIME type
    `text/html;profile=mcp-app`. The host reads the resource, renders it in a
    sandboxed iframe, and pushes the tool's output to it as a
    `ui/notifications/tool-result` notification. The widget is then live UI in
    the conversation rather than text the model retells.

CONSTRAINTS THIS IMPOSES ON THE HTML BELOW
    - Self-contained. The iframe is sandboxed and there is no network to fetch
      a stylesheet or font from, so every rule is inline and the typeface is a
      monospace stack that resolves locally. Same amber-on-charcoal palette as
      the site, so a card in chat and the signer page it links to read as one
      product.
    - Defensive about data. The widget renders numbers Sarf computed, but it
      renders them into a DOM, so everything goes through textContent — never
      innerHTML with interpolated values. An asset name is attacker-influenced
      in principle (it comes from an on-chain `name()`), and a widget that
      pastes it into markup is a cross-site scripting hole in a surface that
      also shows the user's balances.
    - Degrades honestly. A host that does not implement MCP Apps ignores
      `_meta` entirely and shows the text card from card.py, which is why that
      still ships on every order. Nothing here is load-bearing for
      correctness — every fact in the widget is also in the JSON payload.
"""

from __future__ import annotations

# Site tokens (frontend/src/styles.css), inlined because the iframe cannot
# reach a stylesheet.
_CSS = """
:root{
  --bg:#0b0c09; --panel:#15160f; --panel2:#1c1d14; --line:#2c2d22;
  --amber:#e8a33d; --paper:#ede6d6; --dim:#a6a190; --green:#6b9e7d; --red:#b4534a;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--paper);
  font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:13px;line-height:1.5;padding:14px;
}
.card{border:1px solid var(--line);background:var(--panel);border-radius:6px;overflow:hidden}
.head{
  display:flex;justify-content:space-between;align-items:center;
  padding:10px 14px;border-bottom:1px solid var(--line);background:var(--panel2);
}
.brand{font-weight:600;letter-spacing:.06em;font-size:11px}
.brand span{color:var(--amber)}
.tag{font-size:10px;color:var(--dim);letter-spacing:.1em}
.title{padding:14px 14px 4px;font-size:20px;font-weight:600;color:var(--amber)}
.sub{padding:0 14px 12px;color:var(--dim);font-size:11px}
.rows{padding:0 14px}
.row{display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.k{color:var(--dim)}
.v{font-variant-numeric:tabular-nums}
.v.em{color:var(--amber);font-weight:600}
.notes{padding:12px 14px;border-top:1px solid var(--line);background:var(--panel2)}
.notes h4{font-size:10px;letter-spacing:.1em;color:var(--amber);margin-bottom:6px;font-weight:600}
.notes li{color:var(--dim);font-size:11px;margin:4px 0 4px 14px}
.foot{
  padding:10px 14px;border-top:1px solid var(--line);
  display:flex;justify-content:space-between;gap:10px;align-items:center;
  font-size:11px;color:var(--dim);flex-wrap:wrap;
}
a.btn{
  background:var(--amber);color:#141409;text-decoration:none;font-weight:600;
  padding:7px 14px;border-radius:4px;font-size:11px;
}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:var(--amber)}
.pos{display:flex;justify-content:space-between;padding:7px 0;border-top:1px solid var(--line)}
.pos:first-child{border-top:0}
.sym{font-weight:600}
.muted{color:var(--dim)}
.empty{padding:20px 14px;color:var(--dim);text-align:center}
"""

# Shared bootstrap. Listens for the host's tool-result notification, tolerates
# the payload arriving either as content blocks or already-parsed JSON, and
# hands a plain object to the page's render().
_BOOT = """
const $=(t,c)=>{const e=document.createElement(t);if(c)e.className=c;return e};
// Everything user-visible goes through textContent. Asset names come from an
// on-chain name() call, so treating them as markup would be an injection hole
// in a card that also displays balances.
const txt=(e,s)=>{e.textContent=s==null?'—':String(s);return e};
function row(k,v,em){
  const r=$('div','row'); r.appendChild(txt($('span','k'),k));
  const val=$('span','v'+(em?' em':'')); txt(val,v); r.appendChild(val); return r;
}
function payload(msg){
  const p=msg&&msg.params?msg.params:{};
  if(p.structuredContent) return p.structuredContent;
  const blocks=p.content||[];
  for(const b of blocks){
    if(b&&b.type==='text'){ try{ return JSON.parse(b.text); }catch(e){} }
  }
  return null;
}
window.addEventListener('message',(ev)=>{
  const m=ev.data;
  if(!m||m.method!=='ui/notifications/tool-result') return;
  const d=payload(m);
  if(d) try{ render(d); }catch(e){ document.body.textContent='could not render this result'; }
});
// Ask a host that missed us to resend, then announce we are ready.
window.parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}},'*');
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

  const head=$('div','head');
  const b=$('div','brand'); b.textContent='SARF '; const s=$('span'); s.textContent='/ X LAYER RWA';
  b.appendChild(s); head.appendChild(b);
  head.appendChild(txt($('div','tag'), d.executed?'SETTLED':'REVIEW & SIGN'));
  card.appendChild(head);

  const side=String(d.side||'').toUpperCase();
  txt(card.appendChild($('div','title')), side+' '+(d.symbol||''));
  txt(card.appendChild($('div','sub')), (d.name||'').replace(' xStock',''));

  const rows=$('div','rows');
  rows.appendChild(row('You pay', d.spending));
  rows.appendChild(row('You receive (est.)', d.receiving_estimated));
  if(d.minimum_received) rows.appendChild(row('Minimum received', d.minimum_received));
  if(d.estimated_usd!=null) rows.appendChild(row('Order value','$'+Number(d.estimated_usd).toFixed(2)));
  const fee=d.platform_fee||{};
  rows.appendChild(row('Platform fee',
    fee.charged?('$'+Number(fee.usd||0).toFixed(2)+' '+(fee.denominated_in||'')):'none', fee.charged));
  rows.appendChild(row('Network','X Layer · '+(d.chain_id||196)+' · gas in OKB'));
  card.appendChild(rows);

  const notes=(d.risk_notes||[]).filter(Boolean);
  if(notes.length){
    const n=$('div','notes'); const h=$('h4'); h.textContent='READ BEFORE SIGNING'; n.appendChild(h);
    const ul=$('ul'); notes.slice(0,4).forEach(t=>txt(ul.appendChild($('li')),t));
    n.appendChild(ul); card.appendChild(n);
  }

  const foot=$('div','foot');
  if(d.tx_hash){
    txt(foot.appendChild($('span')),'Settled on X Layer');
    const a=$('a','btn'); a.href=d.explorer_url||'#'; a.target='_blank';
    a.textContent='View transaction'; foot.appendChild(a);
  } else {
    txt(foot.appendChild($('span')),'Unsigned — Sarf holds no keys and cannot execute it.');
    if(d.sign_url){
      const a=$('a','btn'); a.href=d.sign_url; a.target='_blank';
      a.textContent='Review & sign'; foot.appendChild(a);
    }
  }
  card.appendChild(foot);
  root.appendChild(card);
}
""")


PORTFOLIO_CARD = _page("""
function render(d){
  const root=document.getElementById('root'); root.innerHTML='';
  const card=$('div','card');

  const head=$('div','head');
  const b=$('div','brand'); b.textContent='SARF '; const s=$('span'); s.textContent='/ HOLDINGS';
  b.appendChild(s); head.appendChild(b);
  txt(head.appendChild($('div','tag')),'X LAYER · 196'); card.appendChild(head);

  const total=d.total_value_usd!=null?d.total_value_usd:d.positions_value_usd;
  txt(card.appendChild($('div','title')), total!=null?('$'+Number(total).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})):'—');
  txt(card.appendChild($('div','sub')),
    (d.positions||[]).length+' position'+((d.positions||[]).length===1?'':'s')+
    ' · '+(d.usdt_balance||'0')+' USDT · '+(d.gas_balance_okb||'0')+' OKB gas');

  const rows=$('div','rows');
  const ps=(d.positions||[]).slice().sort((a,b)=>(b.value_usd||0)-(a.value_usd||0));
  if(!ps.length){ txt(card.appendChild($('div','empty')),'No tokenized stocks held in this wallet yet.'); }
  ps.slice(0,8).forEach(p=>{
    const r=$('div','pos');
    const l=$('div'); txt(l.appendChild($('div','sym')),p.symbol);
    txt(l.appendChild($('div','muted')),p.quantity);
    const rr=$('div'); rr.style.textAlign='right';
    txt(rr.appendChild($('div')), p.value_usd!=null?('$'+Number(p.value_usd).toFixed(2)):'unpriced');
    txt(rr.appendChild($('div','muted')), p.price_usdt!=null?('$'+Number(p.price_usdt).toFixed(2)+' ea'):'');
    const w=total?Math.round((p.value_usd||0)/total*100):0;
    const bar=$('div','bar'); const i=$('i'); i.style.width=Math.min(100,w)+'%'; bar.appendChild(i);
    rr.appendChild(bar);
    r.appendChild(l); r.appendChild(rr); rows.appendChild(r);
  });
  card.appendChild(rows);

  if((d.unpriced_positions||[]).length){
    const n=$('div','notes'); const h=$('h4'); h.textContent='NOT PRICED';
    n.appendChild(h);
    txt(n.appendChild($('li')), d.unpriced_positions.join(', ')+' — excluded from the total above.');
    card.appendChild(n);
  }
  const foot=$('div','foot');
  txt(foot.appendChild($('span')),'Synthetic exposure — no ownership, dividends, or voting rights.');
  card.appendChild(foot);
  root.appendChild(card);
}
""")


ANALYSIS_CARD = _page("""
function render(d){
  const root=document.getElementById('root'); root.innerHTML='';
  const card=$('div','card');

  const head=$('div','head');
  const b=$('div','brand'); b.textContent='SARF '; const s=$('span'); s.textContent='/ ANALYSIS';
  b.appendChild(s); head.appendChild(b);
  txt(head.appendChild($('div','tag')),'INFORMATIONAL ONLY'); card.appendChild(head);

  const c=d.concentration||{};
  txt(card.appendChild($('div','title')), c.effective_positions!=null?(c.effective_positions+' effective positions'):'Portfolio');
  txt(card.appendChild($('div','sub')),
    (d.position_count||0)+' held · largest '+(c.largest_position||'—')+
    ' at '+(c.largest_position_percent!=null?c.largest_position_percent+'%':'—'));

  const rows=$('div','rows');
  (d.composition&&d.composition.by_sector||[]).slice(0,5).forEach(x=>{
    const r=$('div','pos');
    txt(r.appendChild($('div','sym')),x.sector);
    const rr=$('div'); rr.style.textAlign='right'; rr.style.minWidth='90px';
    txt(rr.appendChild($('div')),x.weight_percent+'%');
    const bar=$('div','bar'); const i=$('i'); i.style.width=Math.min(100,x.weight_percent)+'%';
    bar.appendChild(i); rr.appendChild(bar);
    r.appendChild(rr); rows.appendChild(r);
  });
  card.appendChild(rows);

  const f=(d.findings||[]);
  if(f.length){
    const n=$('div','notes'); const h=$('h4'); h.textContent='OBSERVATIONS'; n.appendChild(h);
    const ul=$('ul');
    // Fact and norm stay joined by a dash and nothing else. The widget must
    // not editorialise them into a recommendation — that boundary is the
    // whole reason analyze_portfolio is allowed to exist.
    f.slice(0,5).forEach(x=>txt(ul.appendChild($('li')),
      x.observation+(x.reference_point?(' — '+x.reference_point):'')));
    n.appendChild(ul); card.appendChild(n);
  }

  const foot=$('div','foot');
  txt(foot.appendChild($('span')),
    'Not personalised investment advice. Sarf is not a licensed adviser and sees only this wallet.');
  card.appendChild(foot);
  root.appendChild(card);
}
""")


WIDGETS = {
    "ui://sarf/order-card": ("sarf_order_card", ORDER_CARD,
                             "Order review card — amounts, platform fee, risks, sign link"),
    "ui://sarf/portfolio-card": ("sarf_portfolio_card", PORTFOLIO_CARD,
                                 "Holdings card — positions, weights, cash and gas balances"),
    "ui://sarf/analysis-card": ("sarf_analysis_card", ANALYSIS_CARD,
                                "Portfolio analysis card — concentration, sectors, observations"),
}

UI_MIME = "text/html;profile=mcp-app"
