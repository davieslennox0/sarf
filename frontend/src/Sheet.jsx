import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';

/** Bottom sheet on phones, centred panel on wider screens. Closes on the
 *  backdrop, the close button, or Escape. Children mount only while open.
 *  Rendered into <body>: opened from inside the header, whose backdrop-filter
 *  would otherwise trap a fixed overlay inside the 72px bar. */
export default function Sheet({ open, title, onClose, children }) {
  // A ref, so a new onClose each render does not re-run the effect (which
  // would record the already-locked overflow as the value to restore).
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return undefined;
    const esc = (e) => { if (e.key === 'Escape') close.current(); };
    document.addEventListener('keydown', esc);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', esc); document.body.style.overflow = prev; };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="sheet-head">
          <b>{title}</b>
          <button className="sheet-close" aria-label="Close" onClick={onClose}>✕</button>
        </div>
        <div className="sheet-body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
