/**
 * Shared clients + market registry for the Sarf tx-builder sidecar.
 *
 * Why a Node sidecar instead of pysui: the Current SDK embeds protocol logic
 * that would be risky to reimplement — the Pyth oracle refresh (VAA fetch +
 * updatePriceFeeds via pyth-sui-js), underlying→ctoken conversion for
 * withdrawals, and the flash-loan leverage PTB assembly across 8 DEX
 * adapters. pysui can build PTBs, but we would be hand-coding Move targets
 * and duplicating math that upstream already maintains. The sidecar keeps the
 * vendor SDK authoritative; Python keeps the security boundary (validation,
 * proposals, audit). This process NEVER holds key material: it builds
 * unsigned bytes, dry-runs them, and relays signatures produced elsewhere.
 */
import { SuiJsonRpcClient } from '@mysten/sui/jsonRpc';
import {
  LendingClient,
  Market,
  Obligation,
  coinMetadatas,
  getNetworkConfig,
  type MarketWithEmodes,
  type ObligationData,
  type TypeName,
} from '@current-finance/current-sdk';

export const SUI_RPC_URL = process.env.SUI_RPC_URL ?? 'https://fullnode.mainnet.sui.io:443';
export const PYTH_HERMES_URL = process.env.PYTH_HERMES_URL ?? 'https://hermes.pyth.network';
export const QUOTE_SERVER_URL = process.env.CURRENT_QUOTE_SERVER_URL ?? '';

export const lending = LendingClient.fromConfig({
  network: 'mainnet',
  suiEndpoint: SUI_RPC_URL,
  pythEndpoint: PYTH_HERMES_URL,
});

// JSON-RPC client for object queries, dry-runs and broadcasting; the classic
// fullnode surface (getObject/getOwnedObjects/dryRunTransactionBlock/...).
export const rpc = new SuiJsonRpcClient({ url: SUI_RPC_URL, network: 'mainnet' });

export const networkConfig = getNetworkConfig('mainnet');

export function marketByName(name: string): MarketWithEmodes {
  const m = networkConfig.markets.find((x) => x.name === name);
  if (!m) throw new HttpError(400, `unknown market: ${name}`);
  return m;
}

/** 0x-normalize a Move type coming from config lists (stored without 0x). */
export function with0x(t: string): string {
  return t.startsWith('0x') ? t : `0x${t}`;
}

export function coinDecimals(coinType: TypeName): number {
  const meta = coinMetadatas.get(with0x(coinType)) ?? coinMetadatas.get(coinType.replace(/^0x/, ''));
  if (!meta) throw new HttpError(400, `no coin metadata for ${coinType}`);
  return meta.decimals;
}

export function coinSymbol(coinType: TypeName): string {
  const meta = coinMetadatas.get(with0x(coinType));
  return meta?.symbol ?? coinType.split('::').pop() ?? coinType;
}

export class HttpError extends Error {
  constructor(public statusCode: number, message: string) {
    super(message);
  }
}

export interface LoadedObligation {
  obligationId: string;
  marketInfo: MarketWithEmodes;
  market: Market;          // snapshot at the obligation's emode group
  obligation: Obligation;
  data: ObligationData;
}

/**
 * Resolve an ObligationOwnerCap to its obligation + a Market snapshot at the
 * obligation's own emode group (LTV params differ per emode). Also returns
 * the cap's current on-chain owner so callers can enforce ownership.
 */
/** Fetch cap details, rejecting objects that are not ObligationOwnerCaps. */
export async function getCapDetailChecked(capId: string): Promise<{ obligationId: string; marketType: string; marketId: string }> {
  let detail;
  try {
    detail = await lending.query.getObligationOwnerCapDetail(capId);
  } catch (e: any) {
    throw new HttpError(400, `object ${capId} could not be read: ${e.message}`);
  }
  if (typeof detail.obligationId !== 'string' || typeof detail.marketType !== 'string') {
    throw new HttpError(400, `object ${capId} is not an ObligationOwnerCap`);
  }
  return detail;
}

export async function loadObligationByCap(capId: string): Promise<LoadedObligation & { capOwner: string | null }> {
  const detail = await getCapDetailChecked(capId);
  const marketType = with0x(detail.marketType);
  const marketInfo = networkConfig.markets.find((m) => with0x(m.type) === marketType);
  if (!marketInfo) throw new HttpError(400, `cap ${capId} belongs to unknown market type ${marketType}`);

  const [capOwner, overview] = await Promise.all([
    getObjectOwner(capId),
    lending.query.getObligationOverview(marketType, detail.obligationId),
  ]);

  const snapshot = await lending.getEmodeGroupMarketSnapshot(marketInfo.type, overview.emodeGroupId);
  const market = new Market(marketInfo.type, marketInfo.objectId, snapshot.assets, snapshot.emodeGroups, coinMetadatas);
  const data = await lending.getObligationDetail(detail.obligationId, market);
  return {
    obligationId: detail.obligationId,
    marketInfo,
    market,
    obligation: new Obligation(data),
    data,
    capOwner,
  };
}

/** On-chain owner address of an owned object (null if shared/immutable/missing). */
export async function getObjectOwner(objectId: string): Promise<string | null> {
  const res = await rpc.getObject({ id: objectId, options: { showOwner: true, showType: true } });
  const owner = res.data?.owner as unknown;
  if (owner && typeof owner === 'object' && 'AddressOwner' in (owner as Record<string, unknown>)) {
    return (owner as { AddressOwner: string }).AddressOwner;
  }
  return null;
}
