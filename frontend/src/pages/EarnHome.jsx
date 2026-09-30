import React from 'react';
import { Link } from 'react-router-dom';

/**
 * Earn, held back.
 *
 * There is no deposit or withdraw execution behind Earn yet —
 * earn_allocator.py computes a real, measured allocation but signs nothing
 * and moves nothing. Showing the vault list with live APY numbers next to
 * disabled Deposit buttons looked finished enough to be mistaken for a live
 * product, so it is taken down rather than left up with a caveat. This page
 * is deliberately just a holding notice: nothing to click, nothing to
 * misread as "it works, just needs a click".
 *
 * Not linked from the nav (see App.jsx) — only reachable by a direct link —
 * so it is a landing spot for anyone who already has one, not a discovery
 * path into something that cannot yet do anything.
 */
export default function EarnHome() {
  return (
    <section className="earn-home">
      <div className="market-head">
        <div>
          <div className="eyebrow tick">X Layer · single-asset savings</div>
          <h1>Earn</h1>
          <p className="sub">Coming soon.</p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 22, maxWidth: 560 }}>
        <h3>Not open yet</h3>
        <p>
          Earn will let you deposit one asset and have Sarf spread it across verified venues for
          a blended return — no pools to pick, no thresholds to set. The deposit and withdraw
          paths that would actually move funds are still being built and tested; nothing here
          takes a deposit today.
        </p>
        <p style={{ marginBottom: 0 }}>
          <Link to="/zap">Zap</Link> is live now if you want to put an asset to work on X Layer
          in the meantime.
        </p>
      </div>
    </section>
  );
}
