import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { useWallet } from '../walletctx.jsx';
import { useGrantClock } from '../grant.js';

/** Session key, passkey and live connections for the signed-in account.
 *  Fetched when a section that needs them first mounts. */
export default function useAccountData() {
  const { session } = useWallet();
  const [data, setData] = useState({});
  const [err, setErr] = useState(null);
  const load = async () => {
    setErr(null);
    try {
      const [grant, passkey, connections] = await Promise.all([
        api.grant().catch(() => null),
        api.passkeyStatus().catch(() => null),
        api.connections().catch(() => null),
      ]);
      setData({ grant, passkey, connections });
    } catch (e) { setErr(e.message || String(e)); }
  };
  const loadGrant = async () => {
    const grant = await api.grant().catch(() => null);
    setData((d) => ({ ...d, grant }));
  };
  useEffect(() => { if (session) load(); }, [session?.address]);
  const clock = useGrantClock(data.grant, loadGrant);
  return { session, ...data, ...clock, err, reload: load, loadGrant };
}
