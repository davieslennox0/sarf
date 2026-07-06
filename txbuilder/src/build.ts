/**
 * Unsigned-PTB builders for Current Finance lending actions.
 *
 * Every builder: (1) appends Move calls via the vendor SDK's populate*
 * methods, (2) builds unsigned bytes with the sender set, (3) dry-runs the
 * bytes against the fullnode, (4) computes before/after LTV + liquidation
 * prices. Nothing here signs or broadcasts on its own initiative;
 * broadcasting happens only in broadcast() with signatures produced by the
 * user's wallet.
 */
import { Transaction } from '@mysten/sui/transactions';
import { toBase64, fromBase64 } from '@mysten/sui/utils';
import { Decimal, Obligation, Market, coinMetadatas, isSuiCoinType, type TypeName } from '@current-finance/current-sdk';
import { HttpError, lending, loadObligationByCap, marketByName, rpc, with0x, coinDecimals } from './context.js';
import { priceFor, projectRisk, type RiskProjection } from './risk.js';

export type LendingAction = 'enter_deposit' | 'deposit' | 'borrow' | 'repay' | 'withdraw';

export interface BuildRequest {
  action: LendingAction;
  sender: string;
  coinType: string;         // full 0x... Move type, already whitelist-checked by the Python layer
  amountMinUnits: string;   // bigint as string
  marketName?: string;      // required for enter_deposit; derived from the cap otherwise
  obligationCapId?: string; // required for all actions except enter_deposit
}

export interface SimulationSummary {
  status: 'success' | 'failure';
  error: string | null;
  gasUsedMist: string;
  gasUsedSui: number;
  balanceChanges: Array<{ owner: string; coinType: string; amount: string }>;
}

export interface BuildResponse {
  txBytesBase64: string;
  simulation: SimulationSummary;
  risk: RiskProjection | null;
  estUsd: number | null;
  capOwner?: string | null;
  obligationId?: string | null;
  marketName: string;
}

/** Source the input coin for deposit/repay. SUI comes off the gas coin. */
function sourceCoin(tx: Transaction, coinType: string, amount: bigint) {
  if (isSuiCoinType(coinType)) {
    const [c] = tx.splitCoins(tx.gas, [amount]);
    return c;
  }
  return tx.coin({ type: coinType, balance: amount });
}

export async function simulate(txBytes: Uint8Array): Promise<SimulationSummary> {
  const dry = await rpc.dryRunTransactionBlock({ transactionBlock: txBytes });
  const eff: any = dry.effects;
  const gas = eff?.gasUsed ?? { computationCost: '0', storageCost: '0', storageRebate: '0' };
  const gasMist =
    BigInt(gas.computationCost ?? 0) + BigInt(gas.storageCost ?? 0) - BigInt(gas.storageRebate ?? 0);
  return {
    status: eff?.status?.status === 'success' ? 'success' : 'failure',
    error: eff?.status?.error ?? null,
    gasUsedMist: gasMist.toString(),
    gasUsedSui: Number(gasMist) / 1e9,
    balanceChanges: (dry.balanceChanges ?? []).map((b: any) => ({
      owner: typeof b.owner === 'object' && 'AddressOwner' in b.owner ? b.owner.AddressOwner : JSON.stringify(b.owner),
      coinType: b.coinType,
      amount: b.amount,
    })),
  };
}

export async function buildLendingAction(req: BuildRequest): Promise<BuildResponse> {
  const amount = BigInt(req.amountMinUnits);
  if (amount <= 0n) throw new HttpError(400, 'amount must be positive');
  const coinType = with0x(req.coinType);

  const tx = new Transaction();
  tx.setSender(req.sender);

  let market: Market;
  let obligationBefore: Obligation;
  let marketName: string;
  let capOwner: string | null = null;
  let obligationId: string | null = null;
  let riskAction: 'deposit' | 'withdraw' | 'borrow' | 'repay';

  if (req.action === 'enter_deposit') {
    if (!req.marketName) throw new HttpError(400, 'marketName required for enter_deposit');
    const info = marketByName(req.marketName);
    marketName = info.name;
    const snapshot = await lending.getEmodeGroupMarketSnapshot(info.type, 0);
    market = new Market(info.type, info.objectId, snapshot.assets, snapshot.emodeGroups, coinMetadatas);
    obligationBefore = Obligation.emptyObligation();
    riskAction = 'deposit';

    const coin = sourceCoin(tx, coinType, amount);
    lending.populateEnterMarketAndDepositTxn(tx as any, info.objectId, info.type, coinType, coin, req.sender);
  } else {
    if (!req.obligationCapId) throw new HttpError(400, 'obligationCapId required');
    const loaded = await loadObligationByCap(req.obligationCapId);
    market = loaded.market;
    obligationBefore = loaded.obligation;
    marketName = loaded.marketInfo.name;
    capOwner = loaded.capOwner;
    obligationId = loaded.obligationId;
    const info = loaded.marketInfo;

    switch (req.action) {
      case 'deposit': {
        riskAction = 'deposit';
        const coin = sourceCoin(tx, coinType, amount);
        lending.populatedDepositTxn(tx as any, info.objectId, info.type, req.obligationCapId, coinType, coin);
        break;
      }
      case 'repay': {
        riskAction = 'repay';
        const coin = sourceCoin(tx, coinType, amount);
        lending.populateRepayTxn(tx as any, info.objectId, info.type, req.obligationCapId, coinType, coin);
        break;
      }
      case 'borrow': {
        riskAction = 'borrow';
        const allAssets = lending.query.getAllAssetsInMarket(info.type);
        await lending.populateBorrowTransactionWithAllAssets(
          tx as any, info.objectId, info.type, req.obligationCapId, coinType, allAssets, amount, req.sender,
        );
        break;
      }
      case 'withdraw': {
        riskAction = 'withdraw';
        // The protocol takes ctoken amounts for withdrawals; convert the
        // requested underlying amount using the obligation's live exchange
        // rate (same conversion as the SDK's withdraw example).
        const deposit = obligationBefore.getDeposit(coinType);
        if (deposit.amount() === 0n) throw new HttpError(400, `no ${coinType} deposit in this obligation`);
        const ctokenAmount =
          amount >= deposit.amount() ? deposit.ctokenAmount() : (deposit.ctokenAmount() * amount) / deposit.amount();
        const allAssets = lending.query.getAllAssetsInMarket(info.type);
        await lending.populateWithdrawTransactionWithAssets(
          tx as any, info.objectId, info.type, req.obligationCapId, coinType, allAssets, ctokenAmount,
        );
        break;
      }
      default:
        throw new HttpError(400, `unknown action: ${req.action}`);
    }
  }

  const txBytes = await tx.build({ client: rpc });
  const simulation = await simulate(txBytes);

  // Risk + USD sizing. Price fetch failures must not block a repay/deposit
  // proposal, so they degrade to nulls rather than erroring the build. The
  // market snapshot keys assets by bare struct tag (no 0x), so resolve the
  // projection key from the market's own asset list.
  let risk: RiskProjection | null = null;
  let estUsd: number | null = null;
  const marketKey =
    market.assets().find((a) => with0x(a.coinType) === coinType)?.coinType ?? coinType;
  try {
    const prices = await lending.fetchPythPrices([
      ...new Set<string>([marketKey, ...obligationBefore.depositAssets(), ...obligationBefore.borrowedAssets()]),
    ]);
    const p = priceFor(prices, coinType);
    if (p) estUsd = (Number(amount) / 10 ** coinDecimals(coinType)) * p.asNumber();
    risk = projectRisk(obligationBefore, market, riskAction, marketKey, amount, prices);
  } catch (e) {
    risk = null;
  }

  return {
    txBytesBase64: toBase64(txBytes),
    simulation,
    risk,
    estUsd,
    capOwner,
    obligationId,
    marketName,
  };
}

export interface BroadcastResult {
  digest: string;
  status: 'success' | 'failure';
  error: string | null;
  balanceChanges: Array<{ owner: string; coinType: string; amount: string }>;
  createdObjects: Array<{ objectId: string; objectType: string }>;
}

/** Broadcast bytes signed elsewhere (the user's wallet). No keys here, ever. */
export async function broadcast(txBytesBase64: string, signatures: string[]): Promise<BroadcastResult> {
  const res = await rpc.executeTransactionBlock({
    transactionBlock: fromBase64(txBytesBase64),
    signature: signatures,
    options: { showEffects: true, showBalanceChanges: true, showObjectChanges: true },
  });
  const eff: any = res.effects;
  return {
    digest: res.digest,
    status: eff?.status?.status === 'success' ? 'success' : 'failure',
    error: eff?.status?.error ?? null,
    balanceChanges: (res.balanceChanges ?? []).map((b: any) => ({
      owner: typeof b.owner === 'object' && 'AddressOwner' in b.owner ? b.owner.AddressOwner : JSON.stringify(b.owner),
      coinType: b.coinType,
      amount: b.amount,
    })),
    createdObjects: (res.objectChanges ?? [])
      .filter((c: any) => c.type === 'created')
      .map((c: any) => ({ objectId: c.objectId, objectType: c.objectType })),
  };
}
