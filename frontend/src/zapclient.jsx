/**
 * Which backend a zap page talks to.
 *
 * The Zap and Earn pages are the same code against two different servers:
 * Zap is the original app on its own database, Earn is the new build on its
 * own. Rather than fork the pages, they read their client from here, so a
 * change to either page lands on both and neither can drift.
 *
 * The default is Sarf, so anything that renders these pages without a
 * provider keeps the behaviour it had.
 */

import React, { createContext, useContext } from 'react';

import { api, ensureSession, getSession } from './api.js';
import { earnApi, ensureEarnSession, getEarnSession } from './earn.js';

const SARF = {
  api,
  ensureSession,
  getSession,
  basePath: '/zap',
  brand: 'Zap',
  // Earn's server reports the backtest gate; the original app does not, so
  // the pages hide those columns rather than printing a row of dashes.
  hasBacktest: false,
};

export const EARN = {
  api: earnApi,
  ensureSession: ensureEarnSession,
  getSession: getEarnSession,
  basePath: '/earn',
  brand: 'Earn',
  hasBacktest: true,
};

const Ctx = createContext(SARF);

export function useZapClient() {
  return useContext(Ctx);
}

export function ZapClientProvider({ value, children }) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
