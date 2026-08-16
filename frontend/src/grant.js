/**
 * Session-grant liveness, on the clock rather than on a page load.
 *
 * The grant this deployment issues lasts one hour. The server retires it the
 * moment it lapses — the row is revoked and its signing key destroyed — but a
 * page that fetched `/api/grant` at minute 3 kept rendering that answer at
 * minute 90: an expired session key, its caps and its address, all presented
 * as the account's current authority. Nothing was actually authorised (every
 * execution path checks expiry, and the contract enforces it on-chain), which
 * is precisely why it was so misleading: the screen was the only place the
 * dead key still looked alive.
 *
 * So liveness is derived from `expires_at` against a ticking clock, and the
 * grant is re-fetched when the clock runs out. A user watching the page at the
 * hour mark sees it flip to expired on its own.
 */

import { useEffect, useState } from 'react';

/** Seconds left on a grant, or null when there is nothing counting down. */
export function secondsLeft(grant, now = Date.now()) {
  if (!grant?.expires_at) return null;
  return Math.max(0, Math.floor((grant.expires_at * 1000 - now) / 1000));
}

export function formatLeft(seconds) {
  if (seconds == null) return '';
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, '0');
  return `${m}m ${s}s`;
}

/**
 * -> { live, left, expiredJustNow }
 *
 * `live` is the whole liveness test in one place: the server said the grant is
 * active, the delegation is installed on-chain, AND the expiry has not passed
 * while this page was open. `onExpire` fires once, so the caller can refetch
 * and pick up the server's own account of what happened.
 */
export function useGrantClock(status, onExpire) {
  const [now, setNow] = useState(Date.now());
  const grant = status?.grant || null;
  const left = secondsLeft(grant, now);

  useEffect(() => {
    // Only tick while something is counting down. A page with no grant should
    // not hold a timer open for the life of the tab.
    if (!grant?.expires_at) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [grant?.expires_at]);

  const lapsed = grant != null && left === 0;
  useEffect(() => {
    if (lapsed && onExpire) onExpire();
  }, [lapsed]);

  return {
    live: Boolean(grant && grant.active && status?.delegation_installed && !lapsed),
    left,
    lapsed,
  };
}
