// Native zkLogin ephemeral-session scaffold (config-gated).
//
// Working default today: users sign with a wallet via dapp-kit, which already
// includes zkLogin wallets (Slush et al.) — the zk machinery lives in the
// wallet. This module is the path to a *native* in-page zkLogin session
// (Google OAuth -> ephemeral key -> zk proof -> zkLoginSignature assembled
// here), which needs operator config that does not exist in this deployment
// yet:
//   VITE_GOOGLE_CLIENT_ID  – OAuth client for the id_token nonce flow
//   VITE_ZKLOGIN_PROVER_URL – a ZK prover endpoint (Mysten's mainnet prover
//                             requires enrollment; Enoki is the managed path)
// Until both are set, isNativeZkLoginConfigured() is false and the UI never
// offers this path.
//
// Ephemeral key lifecycle (enforced here, see SECURITY.md):
//   create : Ed25519 keypair generated in-browser on login
//   store  : sessionStorage only, with an explicit expiry epoch — never
//            localStorage, never any server, never cookies
//   use    : signs transaction bytes locally; the zk proof binds it to the
//            user's address for max_epoch epochs
//   expire : checked on every access; expired material is wiped immediately
//   clear  : endEphemeralSession() wipes it on demand ("End session" button)

import { Ed25519Keypair } from '@mysten/sui/keypairs/ed25519';

const KEY = 'sarf.zklogin.ephemeral';
export const SESSION_MINUTES = 30;

export function isNativeZkLoginConfigured() {
  return Boolean(import.meta.env.VITE_GOOGLE_CLIENT_ID && import.meta.env.VITE_ZKLOGIN_PROVER_URL);
}

export function createEphemeralSession() {
  const kp = new Ed25519Keypair();
  const record = {
    secretKey: kp.getSecretKey(), // bech32 suiprivkey — browser memory/sessionStorage only
    createdAt: Date.now(),
    expiresAt: Date.now() + SESSION_MINUTES * 60 * 1000,
  };
  sessionStorage.setItem(KEY, JSON.stringify(record));
  return { publicKey: kp.getPublicKey().toBase64(), expiresAt: record.expiresAt };
}

export function getEphemeralSession() {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const rec = JSON.parse(raw);
    if (Date.now() > rec.expiresAt) {
      endEphemeralSession();
      return null;
    }
    return rec;
  } catch {
    return null;
  }
}

export function endEphemeralSession() {
  // Overwrite before removal so the serialized key does not linger in the
  // storage snapshot longer than necessary.
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ secretKey: '', expiresAt: 0 }));
  } finally {
    sessionStorage.removeItem(KEY);
  }
}
