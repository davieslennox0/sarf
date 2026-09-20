import React, { Suspense, lazy, useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

const AgentsSession = lazy(() => import('../sections/AgentsSession.jsx'));
const Credentials = lazy(() => import('../sections/Credentials.jsx'));

export const ACCOUNT_SECTIONS = [
  { id: 'agents', title: 'Agents & session', blurb: 'What is connected, what it may spend, revoke', Body: AgentsSession },
  { id: 'credentials', title: 'Credentials', blurb: 'Wallet, passkey, session key, export', Body: Credentials },
];

/** The account's two sections. Each one mounts, and fetches, only once opened;
 *  the URL hash says which is open, so /account#credentials is linkable. */
export default function Account() {
  const { hash } = useLocation();
  const navigate = useNavigate();
  const fromHash = hash.replace('#', '');
  const [open, setOpen] = useState(ACCOUNT_SECTIONS.some((s) => s.id === fromHash) ? fromHash : 'agents');
  useEffect(() => { if (ACCOUNT_SECTIONS.some((s) => s.id === fromHash)) setOpen(fromHash); }, [fromHash]);
  const toggle = (id) => {
    const next = open === id ? null : id;
    setOpen(next);
    navigate(next ? `/account#${next}` : '/account', { replace: true });
  };
  return (
    <section>
      <h1>Account</h1>
      <p className="sub">What is connected to this wallet, what it may do, and the keys it holds.</p>
      <div className="folds">
        {ACCOUNT_SECTIONS.map(({ id, title, blurb, Body }) => (
          <div className={`fold${open === id ? ' on' : ''}`} key={id} id={id}>
            <button className="fold-head" onClick={() => toggle(id)} aria-expanded={open === id}>
              <span className="fold-main"><b>{title}</b><span className="muted small">{blurb}</span></span>
              <span className="fold-go" aria-hidden="true">{open === id ? '−' : '+'}</span>
            </button>
            {open === id && (
              <div className="fold-body">
                <Suspense fallback={<p className="muted small">Loading…</p>}><Body /></Suspense>
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
