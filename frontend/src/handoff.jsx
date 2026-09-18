import React from 'react';

/**
 * Website -> chat handoff. Every action on the zap pages can also be done by
 * asking Sarf in Claude or ChatGPT, so each one carries the same request as a
 * sentence, prefilled into a new chat. Both clients read `?q=`. Nothing is
 * sent anywhere until the person presses enter in their chat. The text names
 * the position id, so the assistant has the context without re-explaining.
 */
export function chatLinks(text) {
  const q = encodeURIComponent(text);
  return {
    claude: `https://claude.ai/new?q=${q}`,
    chatgpt: `https://chatgpt.com/?q=${q}`,
  };
}

export function OpenInChat({ text, label = 'or ask Sarf' }) {
  const l = chatLinks(text);
  return (
    <div className="handoff">
      <span className="muted small">{label}</span>
      <a className="btn ghost small" href={l.claude} target="_blank" rel="noreferrer">Open in Claude ↗</a>
      <a className="btn ghost small" href={l.chatgpt} target="_blank" rel="noreferrer">Open in ChatGPT ↗</a>
    </div>
  );
}
