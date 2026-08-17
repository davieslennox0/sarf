/**
 * The setup guide, in one place.
 *
 * This used to live entirely inside pages/How.jsx, which was fine while the
 * only thing that showed it was that page. It is now shown twice — as the page,
 * and as the list behind "How it works" in the header once you are signed in —
 * and two copies of a setup step is one of them being wrong after the next
 * edit. The header reads the titles from here; the page reads the same array
 * and renders each body underneath its own title.
 *
 * The anchor `id` on each step is load-bearing rather than decorative: the menu
 * links to /how#step-connect and friends, so renaming one silently breaks a
 * link that still looks fine.
 */
import React from 'react';

/**
 * The MCP endpoint. The ONLY correct one.
 *
 * The transport is served on the sarf-mcp host, and nothing but /mcp and health
 * is reachable there. The main site host does not serve /mcp at all, so a
 * connector pointed at it fails with a 404 that reads like the server being
 * down rather than like a wrong address.
 */
export const MCP_URL = 'https://sarf-mcp.managerx.xyz/mcp';

/**
 * Where each client keeps its connector list.
 *
 * Neither Claude nor ChatGPT accepts a prefilled "add this MCP server" deep
 * link — there is no documented URL that carries a name and endpoint into the
 * dialog, and inventing one produces a link that silently lands on a settings
 * page with an empty form. So the button does the two things that ARE
 * possible, in the order that makes the paste work: put the endpoint on the
 * clipboard first, then open the page where it has to go. The user arrives
 * with the URL already copied and one field to fill.
 */
export const CLIENTS = [
  {
    id: 'claude',
    label: 'Add to Claude',
    href: 'https://claude.ai/settings/connectors',
    where: 'Settings → Connectors → Add custom connector',
  },
  {
    id: 'chatgpt',
    label: 'Add to ChatGPT',
    href: 'https://chatgpt.com/#settings/Connectors',
    where: 'Settings → Connectors → Add (developer mode, Plus/Pro)',
  },
];

/**
 * Copy the endpoint, then open the client's settings.
 *
 * Copy BEFORE navigating away: a clipboard write is a user-gesture permission
 * and window.open can steal the gesture's tail on some browsers. If the
 * clipboard is unavailable the tab still opens, because the endpoint is on
 * screen to copy by hand.
 */
export async function openClient(client) {
  let copied = false;
  try {
    await navigator.clipboard?.writeText(MCP_URL);
    copied = true;
  } catch { /* no clipboard: the endpoint is rendered as selectable text */ }
  window.open(client.href, '_blank', 'noopener,noreferrer');
  return copied;
}

/**
 * The five steps.
 *
 * `hint` is the one line the header menu shows — it has to say what the step is
 * for on its own, because in the menu there is no body underneath it to
 * explain. `body` is what the page renders.
 */
export const STEPS = [
  {
    id: 'step-connectors',
    num: '01',
    title: 'Open Settings → Connectors',
    hint: 'In Claude or ChatGPT, add a custom connector',
    body: (
      <>
        <p>
          In Claude or ChatGPT, choose to add a custom connector. The buttons
          at the top of this page take you straight there.
        </p>
        <ul className="where">
          {CLIENTS.map((c) => (
            <li key={c.id}>
              <b>{c.label.replace('Add to ', '')}</b> — {c.where}
            </li>
          ))}
        </ul>
      </>
    ),
  },
  {
    id: 'step-endpoint',
    num: '02',
    title: 'Paste the Sarf MCP URL',
    hint: 'One endpoint, pasted into the connector dialog',
    body: (
      <p>
        Your client will send you here to approve the connection. That approval
        is one signature proving you control the address — it authorizes no
        transaction and moves no funds.
      </p>
    ),
  },
  {
    id: 'step-passkey',
    num: '03',
    title: 'Add a passkey',
    hint: 'Face ID, Touch ID or a device PIN — approves every trade',
    body: (
      <p>
        One touch of Face ID, Touch ID, or your device PIN. It is what approves
        every transaction from then on — nothing gets signed without it, and it
        never leaves your device.
      </p>
    ),
  },
  {
    id: 'step-limits',
    num: '04',
    title: 'Choose how it asks',
    hint: 'Always Ask, or autonomous up to a limit you set',
    body: (
      <p>
        <b>Always Ask</b> — every trade needs your passkey, whatever the size.
        <br />
        <b>Autonomous</b> — trades up to a limit you set go through without a
        prompt; anything above it still asks. Changing that limit needs your
        passkey again, so the agent can never raise it on its own.
      </p>
    ),
  },
  {
    id: 'step-ask',
    num: '05',
    title: 'Start asking',
    hint: '"what can I buy?", "price of NVDAx", "how am I balanced?"',
    body: (
      <p>
        Try "what can I buy?", "price of NVDAx", or "how is my portfolio
        balanced?" right in the chat.
      </p>
    ),
  },
];
