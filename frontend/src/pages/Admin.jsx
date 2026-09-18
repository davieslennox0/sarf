import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, ensureSession } from '../api.js';
import { connect, currentAccount, short, txUrl } from '../wallet.js';

/**
 * Operator console.
 *
 * Two things about this page are worth stating up front, because both look
 * like oversights from the outside.
 *
 * FIRST: it is gated by EMAIL, in a product whose entire identity model is
 * addresses. That is a deliberate exception with a narrow scope. The gate is
 * a Privy identity token whose signature the server verifies (see
 * server/sarf/privy_auth.py) — not the browser's own claim to be logged in,
 * which nothing here or anywhere else trusts. And what it opens is a page
 * that reads aggregates and revokes sessions. It cannot sign, cannot move
 * funds, cannot mint a session for another account and cannot raise a cap.
 * The passkey and the wallet still own everything that touches money.
 *
 * SECOND: nothing on this page is a security boundary. The tab is hidden from
 * non-admins as a courtesy, not as protection — every number shown here was
 * fetched from a route that independently refused everyone else. Editing a
 * boolean in devtools reveals an empty page and a row of 403s, which is the
 * property that makes hiding the tab a UI decision rather than a load-bearing
 * one.
 *
 * The layout follows the site's existing vocabulary (.stats strips, .card,
 * .kv, .orders tables) rather than inventing an admin skin: this is the same
 * product seen from the operator's side, and a console that looks like a
 * different application is one more thing to learn.
 */

const TABS = [
  { id: '', label: 'Overview' },
  { id: 'users', label: 'Accounts' },
  { id: 'orders', label: 'Orders' },
  { id: 'deposits', label: 'Deposits' },
  { id: 'grants', label: 'Session keys' },
  { id: 'audit', label: 'Audit log' },
];

// Overview refreshes on its own, because a console someone leaves open on a
// second monitor is worth more than one that shows the moment it was opened.
// Paused while the tab is hidden — polling a backgrounded tab is load nobody
// is reading.
const REFRESH_MS = 30000;

// --- formatting --------------------------------------------------------------

const usd = (v) =>
  v == null ? '—' : `$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;

const num = (v) => (v == null ? '—' : Number(v).toLocaleString());

/** Wei -> ETH. The gas drips are ETH on BASE, not OKB on X Layer: Sarf tops
 *  people up there so they can sign their own CCTP burn. Labelling it OKB
 *  would name the wrong chain's currency for the one balance that funds the
 *  deposit flow. */
const eth = (weiStr) => {
  try {
    const w = BigInt(weiStr || '0');
    if (w === 0n) return '0';
    // Six decimals via integer maths — Number(wei) loses precision above 2^53
    // and these totals are summed across every drip ever sent.
    const whole = w / 10n ** 18n;
    const frac = ((w % 10n ** 18n) * 10n ** 6n) / 10n ** 18n;
    return `${whole}.${frac.toString().padStart(6, '0')}`;
  } catch {
    return '—';
  }
};

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '—');

const ago = (ts) => {
  if (!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

const ORDER_TONE = {
  proposed: 'accent', awaiting_signature: 'accent',
  submitted: 'green', confirmed: 'green', executed: 'green',
  failed: 'red', expired: 'grey',
};

const DEPOSIT_TONE = { minted: 'green', pending: 'accent', failed: 'red' };

// --- small pieces ------------------------------------------------------------

function Stat({ label, value, tone }) {
  return (
    <div>
      <b className={tone ? `chip-${tone}` : undefined}
         style={tone === 'red' ? { color: 'var(--red)' }
              : tone === 'green' ? { color: 'var(--green)' } : undefined}>
        {value}
      </b>
      <span>{label}</span>
    </div>
  );
}

function Rows({ pairs }) {
  return (
    <div className="kv">
      {pairs.map(([k, v]) => (
        <div key={k}><span>{k}</span><b>{v}</b></div>
      ))}
    </div>
  );
}

/**
 * A destructive control that asks first.
 *
 * Inline rather than a modal: these act on one row, and a dialog that covers
 * the table hides the row you are trying to identify. The confirm state is
 * per-button, so two rows cannot end up sharing one "are you sure".
 */
function Danger({ label, confirmLabel, onRun, busy }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return undefined;
    const t = setTimeout(() => setArmed(false), 6000);
    return () => clearTimeout(t);
  }, [armed]);

  if (!armed) {
    return (
      <button className="btn ghost small" disabled={busy}
              onClick={() => setArmed(true)}>{label}</button>
    );
  }
  return (
    <span className="cta" style={{ margin: 0, gap: 6 }}>
      <button className="btn danger small" disabled={busy}
              onClick={async () => { setArmed(false); await onRun(); }}>
        {busy ? '…' : confirmLabel}
      </button>
      <button className="btn ghost small" onClick={() => setArmed(false)}>Cancel</button>
    </span>
  );
}

// --- gate states -------------------------------------------------------------

function NotConfigured({ reason }) {
  return (
    <section>
      <p className="eyebrow">Operator console</p>
      <h1>Not configured yet</h1>
      <p className="sub">
        The console is switched off on this deployment: <b>{reason}</b>. It fails
        closed rather than open, so until all three values are set every admin
        route refuses everyone — including you.
      </p>
      <div className="card">
        <h3>What to set in .env</h3>
        <p>
          The two Privy values come from the Privy dashboard under{' '}
          <b>Settings → Basics</b>, on the same app as{' '}
          <code>VITE_PRIVY_APP_ID</code>. The verification key is a public key,
          so it is configuration rather than a secret — it still lives in .env
          because it is per-deployment.
        </p>
        <div className="code-block">{`SARF_ADMIN_EMAILS=you@example.com
PRIVY_APP_ID=<same value as frontend/.env VITE_PRIVY_APP_ID>
PRIVY_VERIFICATION_KEY="-----BEGIN PUBLIC KEY-----
...
-----END PUBLIC KEY-----"`}</div>
        <p className="norm">
          Restart the server afterwards. The email must be the Google address you
          sign in with — it is matched against the address Google confirmed
          inside a Privy token this server verifies, never against anything the
          browser says about itself.
        </p>
      </div>
    </section>
  );
}

function NotAdmin({ address, reason }) {
  return (
    <section>
      <p className="eyebrow">Operator console</p>
      <h1>Not available on this account</h1>
      <p className="sub">
        This page is limited to the Google accounts on the operator allow-list,
        and this session — signed in as {short(address)} — did not present one.
      </p>
      <p className="muted small">
        If this is your deployment: the console keys off the <b>Google address
        you signed in with</b>, not off this wallet, so signing in with a
        different wallet will not change the answer.
      </p>
      {reason ? (
        <div className="card">
          <h3>Why it refused</h3>
          <p className="norm">The server reported: <b>{reason}</b>.</p>
          {reason.includes('no identity token') ? (
            <p className="norm">
              Nothing reached the server to check. That means Privy is not
              issuing an identity token for this login — sign in with{' '}
              <b>Google</b> rather than a wallet, and if you already did, turn on
              identity tokens for this app in the Privy dashboard under{' '}
              <b>Settings → Advanced</b>, then sign out and back in so a fresh
              token is stored.
            </p>
          ) : reason.includes('no allow-listed email') ? (
            <p className="norm">
              A valid Privy token arrived, but the Google address on it is not in{' '}
              <code>SARF_ADMIN_EMAILS</code>. Matching is exact after
              lowercasing, with no Gmail dot or plus folding — so{' '}
              <code>a.b@gmail.com</code> and <code>ab@gmail.com</code> are two
              different entries here. Sign in with the allow-listed address, or
              add the one you use.
            </p>
          ) : (
            <p className="norm">
              A token arrived but did not verify. Check that{' '}
              <code>PRIVY_APP_ID</code> is the same app as the frontend&apos;s{' '}
              <code>VITE_PRIVY_APP_ID</code> — a token minted for a different
              app is refused here by design — and that the server can reach{' '}
              <code>auth.privy.io</code> to fetch the key set.
            </p>
          )}
        </div>
      ) : null}
      <div className="cta"><Link className="btn ghost" to="/dashboard">Back to dashboard</Link></div>
    </section>
  );
}

// --- tab bodies --------------------------------------------------------------

function Overview({ data }) {
  const d = data;
  const stuck = d.deposits.stuck;
  const snapshotStale =
    d.snapshot.age_seconds != null &&
    d.snapshot.age_seconds > d.snapshot.refresh_seconds * 4;

  return (
    <>
      {/* Anything needing a human goes first and unprompted. A console whose
          problems are three scrolls down is a console that gets read once. */}
      {(stuck > 0 || snapshotStale || d.config.quote_transport === 'cli') && (
        <div className="card accent">
          <h3>Needs attention</h3>
          {stuck > 0 && (
            <p>
              <b>{stuck} deposit{stuck === 1 ? '' : 's'} stuck</b> — pending for
              over {Math.round(d.deposits.stuck_after_seconds / 60)} minutes.
              Attestation is normally seconds. See the Deposits tab, where each
              one can be handed back to the sweeper.
            </p>
          )}
          {d.config.quote_transport === 'cli' && (
            <p>
              <b>Quotes are on the CLI fallback.</b> The OKX DEX API credentials
              are not working, and the CLI transport builds orders{' '}
              <b>without the platform fee</b> — trades still settle, the fee is
              simply not collected.
            </p>
          )}
          {snapshotStale && (
            <p>
              <b>The TVL snapshot is stale</b> — last refreshed {ago(d.snapshot.updated_at)},
              against a {d.snapshot.refresh_seconds}s interval. The background
              job may have stopped.
            </p>
          )}
        </div>
      )}

      <div className="section-label">Accounts</div>
      <div className="stats">
        <Stat label="Total" value={num(d.users.total)} />
        <Stat label="New 24h" value={num(d.users.new_24h)} />
        <Stat label="New 7d" value={num(d.users.new_7d)} />
        <Stat label="Active 24h" value={num(d.users.active_24h)} />
        <Stat label="Live sessions" value={num(d.sessions.live)} />
        <Stat label="With a passkey"
              value={`${num(d.passkeys.accounts)}/${num(d.passkeys.accounts_total)}`} />
      </div>

      <div className="grid g2" style={{ marginTop: 24 }}>
        <div className="card">
          <h3>Where accounts came from</h3>
          <Rows pairs={[
            ['Signed in on the site', num(d.users.by_source.wallet)],
            ['First seen via MCP tools', num(d.users.by_source.mcp)],
            ['Sessions minted 24h', num(d.sessions.minted_24h)],
            ['Sessions revoked 24h', num(d.sessions.revoked_24h)],
          ]} />
        </div>
        <div className="card">
          <h3>What is connected</h3>
          {d.sessions.by_client.length === 0 ? (
            <p>No live sessions.</p>
          ) : (
            <Rows pairs={d.sessions.by_client.map((c) => [c.client, num(c.count)])} />
          )}
          <p className="norm">
            Client names are self-declared at registration (RFC 7591). They are
            labels, and nothing is authorised on the strength of one.
          </p>
        </div>
      </div>

      <div className="section-label">Trading</div>
      <div className="stats">
        <Stat label="Orders built" value={num(d.orders.total)} />
        <Stat label="Reached the chain" value={num(d.orders.settled_count)} />
        <Stat label="Volume" value={usd(d.orders.volume_usd)} />
        <Stat label="Volume 24h" value={usd(d.orders.volume_usd_24h)} />
        <Stat label="Fees (est.)" value={usd(d.fees.estimated_total_usd)} />
      </div>

      <div className="grid g2" style={{ marginTop: 24 }}>
        <div className="card">
          <h3>Order status</h3>
          {d.orders.by_status.length === 0 ? <p>No orders yet.</p> : (
            <Rows pairs={d.orders.by_status.map((s) => [s.status, num(s.count)])} />
          )}
          <p className="norm">
            {num(d.orders.unsigned_count)} built and never signed, quoting{' '}
            {usd(d.orders.unsigned_usd)}. Unsigned orders are normal — building
            one commits nothing — and they are excluded from volume above.
          </p>
        </div>
        <div className="card">
          <h3>Fee revenue</h3>
          <Rows pairs={[
            ['Per swap', usd(d.fees.per_swap_usd)],
            ['Estimated total', usd(d.fees.estimated_total_usd)],
            ['Estimated 24h', usd(d.fees.estimated_24h_usd)],
            ['Fee address set', d.config.fee_address_set ? 'yes' : 'no — no fee charged'],
          ]} />
          <p className="norm">
            An estimate, and shown as one: the fee is collected by the aggregator
            inside the user&rsquo;s own transaction, so this server never sees a fee
            transfer to count. It is settled orders × the flat fee, not a
            confirmed balance.
          </p>
        </div>
      </div>

      {d.orders.top_symbols.length > 0 && (
        <div className="card">
          <h3>Most traded</h3>
          <Rows pairs={d.orders.top_symbols.map((t) => [
            t.symbol, `${num(t.count)} orders · ${usd(t.usd)}`,
          ])} />
        </div>
      )}

      <div className="section-label">Money in</div>
      <div className="stats">
        <Stat label="Deposits minted" value={num(d.deposits.minted_count)} />
        <Stat label="Value minted" value={usd(d.deposits.minted_usd)} />
        <Stat label="Started 24h" value={num(d.deposits.count_24h)} />
        <Stat label="Stuck" value={num(d.deposits.stuck)}
              tone={d.deposits.stuck > 0 ? 'red' : 'green'} />
        <Stat label="Gas given (ETH, Base)" value={eth(d.gas.wei_total)} />
        <Stat label="Gas 24h" value={eth(d.gas.wei_24h)} />
      </div>

      <div className="grid g2" style={{ marginTop: 24 }}>
        <div className="card">
          <h3>Deposit pipeline</h3>
          {d.deposits.by_status.length === 0 ? <p>No deposits yet.</p> : (
            <Rows pairs={d.deposits.by_status.map((s) => [
              s.status, `${num(s.count)} · ${usd(s.usd)}`,
            ])} />
          )}
        </div>
        <div className="card">
          <h3>Relayer gas outflow</h3>
          <Rows pairs={[
            ['Top-ups sent', num(d.gas.drips)],
            ['Recipients', num(d.gas.recipients)],
            ['Last 24h', `${eth(d.gas.wei_24h)} ETH`],
            ['Last 7d', `${eth(d.gas.wei_7d)} ETH`],
            ['All time', `${eth(d.gas.wei_total)} ETH`],
          ]} />
          <p className="norm">
            ETH on Base, not OKB: this is the gas Sarf gives users so they can
            sign their own CCTP burn. It is value leaving the relayer, and the
            relayer is meant to hold gas and nothing else.
          </p>
        </div>
      </div>

      <div className="section-label">Server</div>
      <div className="grid g2">
        <div className="card">
          <h3>Configuration</h3>
          <Rows pairs={[
            ['Mode', d.config.env],
            ['Chain', `X Layer (${d.config.chain_id})`],
            ['Tradable assets', num(d.config.tradable_assets)],
            ['Quote transport', d.config.quote_transport || '—'],
            ['Session TTL', `${Math.round(d.config.session_ttl_seconds / 60)} min`],
            ['Max order', usd(d.config.max_order_usd)],
            ['Max price impact', `${d.config.max_price_impact_pct}%`],
            ['Passkey required', d.config.passkey_required ? 'yes' : 'no'],
            ['In-chat auto limit', usd(d.config.delegated_auto_usd)],
            ['Relayer configured', d.config.relayer_configured ? 'yes' : 'no'],
          ]} />
        </div>
        <div className="card">
          <h3>Session keys &amp; snapshot</h3>
          <Rows pairs={[
            ['Live grants', num(d.grants.live)],
            ['Grants ever issued', num(d.grants.total)],
            ['Revoked', num(d.grants.revoked)],
            ['Delegate', short(d.config.delegate_address)],
            ['Snapshot age', d.snapshot.updated_at ? ago(d.snapshot.updated_at) : 'never run'],
            ['Snapshot interval', `${d.snapshot.refresh_seconds}s`],
          ]} />
        </div>
      </div>

      {d.clients.length > 0 && (
        <div className="card">
          <h3>Registered MCP clients</h3>
          <Rows pairs={d.clients.map((c) => [c.client_name, when(c.created_at)])} />
        </div>
      )}
    </>
  );
}

function Users({ rows, query, setQuery, onRevoke, busy }) {
  return (
    <>
      <div className="input-row" style={{ marginTop: 18 }}>
        <input value={query} placeholder="Filter by address…"
               onChange={(e) => setQuery(e.target.value)} />
      </div>
      {rows.length === 0 ? <p className="muted">No accounts match.</p> : (
        <table className="orders">
          <thead>
            <tr><th>Address</th><th>Source</th><th>First seen</th><th>Last seen</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((u) => (
              <tr key={u.address}>
                <td><code>{short(u.address)}</code></td>
                <td><span className="chip grey">{u.source}</span></td>
                <td>{when(u.first_seen)}</td>
                <td>{ago(u.last_seen)}</td>
                <td>
                  <Danger label="Sign out" confirmLabel="Revoke sessions"
                          busy={busy === u.address}
                          onRun={() => onRevoke(u.address)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted small">
        Revoking ends every session and refresh family for an account. They are
        not locked out — they sign in again with their wallet, as after any
        expiry.
      </p>
    </>
  );
}

function Orders({ rows }) {
  if (rows.length === 0) return <p className="muted">No orders yet.</p>;
  return (
    <table className="orders">
      <thead>
        <tr>
          <th>When</th><th>Account</th><th>Action</th><th>Value</th>
          <th>Status</th><th>Transaction</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((o) => (
          <tr key={o.order_id}>
            <td>{when(o.created_at)}</td>
            <td><code>{short(o.address)}</code></td>
            <td><b>{o.side}</b> {o.symbol}</td>
            <td>{usd(o.est_usd)}</td>
            <td><span className={`chip ${ORDER_TONE[o.status] || 'grey'}`}>{o.status}</span></td>
            <td>
              {o.tx_hash
                ? <a href={txUrl(o.tx_hash)} target="_blank" rel="noreferrer">{short(o.tx_hash)} ↗</a>
                : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Deposits({ rows, onRetry, busy }) {
  if (rows.length === 0) return <p className="muted">No deposits yet.</p>;
  return (
    <>
      <table className="orders">
        <thead>
          <tr>
            <th>Started</th><th>Account</th><th>Amount</th><th>Status</th>
            <th>Tries</th><th>Last error</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.burn_tx}>
              <td>{when(d.created_at)}</td>
              <td><code>{short(d.address)}</code></td>
              <td>{usd(d.amount_usd)}</td>
              <td>
                <span className={`chip ${d.stuck ? 'red' : DEPOSIT_TONE[d.status] || 'grey'}`}>
                  {d.stuck ? 'stuck' : d.status}
                </span>
              </td>
              <td>{num(d.attempts)}</td>
              <td className="muted small" style={{ maxWidth: 260, wordBreak: 'break-word' }}>
                {d.last_error || '—'}
              </td>
              <td>
                {d.status === 'minted' ? '—' : (
                  <Danger label="Retry" confirmLabel="Re-queue"
                          busy={busy === d.burn_tx}
                          onRun={() => onRetry(d.burn_tx)} />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        Re-queueing hands a deposit back to the sweeper and resets its attempt
        count. It cannot misdeliver: the recipient is written into the message
        the user already signed on Base, so this decides only <i>when</i> the
        money arrives, never where.
      </p>
    </>
  );
}

function Grants({ rows, onRevoke, busy }) {
  if (rows.length === 0) return <p className="muted">No live session keys.</p>;
  return (
    <>
      <table className="orders">
        <thead>
          <tr>
            <th>Account</th><th>Session key</th><th>Expires</th>
            <th>Per trade</th><th>Daily</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((g) => (
            <tr key={g.address}>
              <td><code>{short(g.address)}</code></td>
              <td><code>{short(g.session_address)}</code></td>
              <td>{when(g.expiry)}</td>
              <td>{usd(g.per_trade_cap / 1e6)}</td>
              <td>{usd(g.daily_cap / 1e6)}</td>
              <td>
                <Danger label="Revoke" confirmLabel="Revoke locally"
                        busy={busy === g.address}
                        onRun={() => onRevoke(g.address)} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        This revoke is the <b>local half only</b>: Sarf stops signing with the
        key. The binding revocation is on-chain and can only be sent by the
        account&rsquo;s own wallet — until they send it, the contract still
        recognises the key.
      </p>
    </>
  );
}

function Audit({ rows }) {
  if (rows.length === 0) {
    return <p className="muted">No admin actions have been taken.</p>;
  }
  return (
    <table className="orders">
      <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th></tr></thead>
      <tbody>
        {rows.map((e, i) => (
          <tr key={`${e.created_at}-${i}`}>
            <td>{when(e.created_at)}</td>
            <td>{e.actor_email}</td>
            <td><span className="chip accent">{e.action}</span></td>
            <td><code>{e.target ? short(e.target) : '—'}</code></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// --- page --------------------------------------------------------------------

export default function Admin() {
  const { section } = useParams();
  const tab = TABS.some((t) => t.id === (section || '')) ? (section || '') : '';

  const [gate, setGate] = useState(null);     // whoami result
  const [data, setData] = useState(null);     // current tab's payload
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(null);     // row id mid-action
  const [note, setNote] = useState(null);
  const [query, setQuery] = useState('');
  // The filter the fetch actually uses, a beat behind the box. Typing "0xab"
  // is four keystrokes, and without this it was four round trips — each one
  // replacing the rows under a cursor that was still moving.
  const [applied, setApplied] = useState('');
  const [loadedAt, setLoadedAt] = useState(null);
  const alive = useRef(true);

  useEffect(() => () => { alive.current = false; }, []);

  useEffect(() => {
    const t = setTimeout(() => setApplied(query), 250);
    return () => clearTimeout(t);
  }, [query]);

  // The session has to exist before whoami can answer, and on a cold load of
  // /admin it may not yet — same opening move as every other account page.
  const openSession = useCallback(async () => {
    const addr = (await currentAccount()) || (await connect());
    await ensureSession(addr);
    return addr;
  }, []);

  const loadGate = useCallback(async () => {
    setErr(null);
    try {
      await openSession();
      const who = await api.adminWhoami();
      if (alive.current) setGate(who);
    } catch (e) {
      if (alive.current) setErr(e.message);
    }
  }, [openSession]);

  useEffect(() => { loadGate(); }, [loadGate]);

  const loadTab = useCallback(async (quiet = false) => {
    if (!gate?.is_admin) return;
    if (!quiet) setErr(null);
    try {
      const d = tab === '' ? await api.adminOverview()
        : tab === 'users' ? await api.adminUsers(applied)
        : tab === 'orders' ? await api.adminOrders()
        : tab === 'deposits' ? await api.adminDeposits()
        : tab === 'grants' ? await api.adminGrants()
        : await api.adminAudit();
      if (!alive.current) return;
      setData(d);
      setLoadedAt(Date.now());
    } catch (e) {
      // A failed background refresh must not blank a page that is currently
      // readable — it reports, and leaves the last good data in place.
      if (alive.current && !quiet) setErr(e.message);
    }
  }, [gate?.is_admin, tab, applied]);

  useEffect(() => { setData(null); }, [tab]);
  useEffect(() => { loadTab(); }, [loadTab]);

  // Auto-refresh, overview only, and never while the tab is hidden.
  useEffect(() => {
    if (tab !== '' || !gate?.is_admin) return undefined;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') loadTab(true);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [tab, gate?.is_admin, loadTab]);

  const act = async (id, run) => {
    setBusy(id);
    setNote(null);
    try {
      const res = await run();
      setNote(res?.note || 'Done.');
      await loadTab(true);
    } catch (e) {
      setErr(e.message);
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  if (err && !gate) {
    return (
      <section>
        <h1>Operator console</h1>
        <p className="error">{err}</p>
        <div className="cta"><button onClick={loadGate}>Try again</button></div>
      </section>
    );
  }
  if (!gate) return <section><h1>Operator console</h1><p className="muted">Checking…</p></section>;
  if (!gate.configured) return <NotConfigured reason={gate.reason} />;
  if (!gate.is_admin) return <NotAdmin address={gate.address} reason={gate.reason} />;

  return (
    <section>
      <p className="eyebrow">Operator console</p>
      <h1>How Sarf is doing</h1>
      <p className="sub">
        Signed in as <b>{gate.email}</b>. Everything here is server-side and
        aggregate — no balances, no portfolios, no keys. Actions are recorded in
        the audit log before they run.
      </p>

      <div className="cta" style={{ marginTop: 20 }}>
        {TABS.map((t) => (
          <Link key={t.id} to={t.id ? `/admin/${t.id}` : '/admin'}
                className={`btn ${tab === t.id ? '' : 'ghost'}`}>
            {t.label}
          </Link>
        ))}
        <button className="btn ghost" onClick={() => loadTab()}>Refresh</button>
      </div>

      {loadedAt && (
        <p className="muted small" style={{ marginTop: 4 }}>
          Loaded {new Date(loadedAt).toLocaleTimeString()}
          {tab === '' ? `, refreshing every ${REFRESH_MS / 1000}s` : ''}
        </p>
      )}
      {/* Only over readable data — a refresh that failed above rows that are
          still good. When there is nothing to show, the failure gets the body
          to itself below, rather than being said twice. */}
      {err && data && <p className="error">{err}</p>}
      {note && <p className="muted small">{note}</p>}

      {!data && err ? (
        // A failed load is not a slow one. Without this branch the body sits on
        // "Loading…" for as long as the tab is open, because `data` is cleared
        // on every tab change and only ever set again by a request that
        // succeeds — so one refused fetch reads as a page that never arrives.
        // Overview hid this by refreshing every 30s and healing itself; the
        // other tabs fetch once, so a single failure was permanent.
        <div className="card">
          <h3>That did not load</h3>
          <p className="norm">{err}</p>
          <button className="btn" onClick={() => loadTab()}>Try again</button>
        </div>
      ) : !data ? <p className="muted">Loading…</p>
        : tab === '' ? <Overview data={data} />
        : tab === 'users' ? (
          <Users rows={data.users} query={query} setQuery={setQuery} busy={busy}
                 onRevoke={(a) => act(a, () => api.adminRevokeSessions(a))} />
        )
        : tab === 'orders' ? <Orders rows={data.orders} />
        : tab === 'deposits' ? (
          <Deposits rows={data.deposits} busy={busy}
                    onRetry={(tx) => act(tx, () => api.adminRetryDeposit(tx))} />
        )
        : tab === 'grants' ? (
          <Grants rows={data.grants} busy={busy}
                  onRevoke={(a) => act(a, () => api.adminRevokeGrant(a))} />
        )
        : <Audit rows={data.entries} />}
    </section>
  );
}
