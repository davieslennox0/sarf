import React, { createContext, useContext } from 'react';

/**
 * One wallet context for the whole app: the signed-in session, its address,
 * and whether it is an operator. Pages and tabs read this rather than each
 * asking the wallet extension again, so switching tabs never re-fetches who
 * you are.
 */
const Ctx = createContext({ session: null, address: null, signedIn: false, isAdmin: null, refresh: () => {} });

export const WalletCtx = Ctx.Provider;
export const useWallet = () => useContext(Ctx);
