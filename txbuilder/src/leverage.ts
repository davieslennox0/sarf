/**
 * Leverage (Current Multiply) position builder.
 *
 * Requires two pieces of operator configuration that are NOT derivable from
 * the vendor SDK alone:
 *   - CURRENT_QUOTE_SERVER_URL: the protocol quote backend (QuoteClient).
 *   - LEVERAGE_PAIRS: JSON mapping collateral symbol -> quote-server pair
 *     params, e.g. {"HASUI": {"leverageMarketIndex": 0, "tokenPairId": 1,
 *     "isLong": true, "collateralCoinType": "0x..", "borrowCoinType": "0x.."}}
 *     tokenPairId is defined by the quote server, so it must be verified
 *     against that service — there is no on-repo registry to read it from.
 * Without both, this endpoint returns 503 and the MCP tool degrades
 * gracefully instead of guessing pair IDs.
 */
import { Transaction } from '@mysten/sui/transactions';
import { toBase64 } from '@mysten/sui/utils';
import {
  BestMultiTokenExchange,
  CetusAggregator,
  EEarn,
  ESui,
  EThird,
  HaSui,
  LeverageMarketClient,
  LeverageObligation,
  Market,
  QuoteClient,
  SpringSui,
  StSui,
  VSui,
  coinMetadatas,
} from '@current-finance/current-sdk';
import { HttpError, QUOTE_SERVER_URL, lending, networkConfig, with0x, coinDecimals } from './context.js';
import { simulate, type SimulationSummary } from './build.js';
import { collateralLiquidationPrice, ltvSummary, type LtvSummary } from './risk.js';

interface PairConfig {
  leverageMarketIndex: number;
  tokenPairId: number;
  isLong: boolean;
  collateralCoinType: string;
  borrowCoinType: string;
}

function pairConfigs(): Record<string, PairConfig> {
  const raw = process.env.LEVERAGE_PAIRS ?? '';
  if (!raw.trim()) return {};
  return JSON.parse(raw);
}

let leverageClient: LeverageMarketClient | null = null;
function getLeverageClient(): LeverageMarketClient {
  if (!QUOTE_SERVER_URL) {
    throw new HttpError(503, 'leverage disabled: CURRENT_QUOTE_SERVER_URL is not configured');
  }
  if (!leverageClient) {
    const grpc = lending.provider;
    const exchange = new BestMultiTokenExchange(
      CetusAggregator.newInstance(),
      new SpringSui(grpc),
      new StSui(grpc),
      new VSui(grpc),
      new HaSui(grpc),
      new ESui(grpc),
      new EThird(grpc),
      new EEarn(grpc),
    );
    const quote = new QuoteClient({ baseUrl: QUOTE_SERVER_URL });
    leverageClient = new LeverageMarketClient(quote, exchange, lending, 'MainMarket');
  }
  return leverageClient;
}

export interface LeverageBuildRequest {
  sender: string;
  collateralSymbol: string;   // key into LEVERAGE_PAIRS
  principalMinUnits: string;  // deposit that seeds the position
  multiplier: number;         // already capped by the Python validation layer
  swapSlippage?: number;      // default 0.005 (0.5%)
}

export interface LeverageBuildResponse {
  txBytesBase64: string;
  simulation: SimulationSummary;
  quote: {
    totalCollateral: string;
    totalDebt: string;
    dexPriceImpact: number;
  };
  projected: LtvSummary | null;
  liquidationPriceUsd: number | null;
  currentPriceUsd: number | null;
  collateralCoinType: string;
  borrowCoinType: string;
}

export async function buildLeverageOpen(req: LeverageBuildRequest): Promise<LeverageBuildResponse> {
  const client = getLeverageClient();
  const pairs = pairConfigs();
  const pair = pairs[req.collateralSymbol];
  if (!pair) {
    throw new HttpError(
      503,
      `leverage pair for ${req.collateralSymbol} not configured; set LEVERAGE_PAIRS (available: ${Object.keys(pairs).join(', ') || 'none'})`,
    );
  }
  const principal = BigInt(req.principalMinUnits);
  if (principal <= 0n) throw new HttpError(400, 'principal must be positive');
  const slippage = req.swapSlippage ?? 0.005;

  const quoted = await client.quoteIncreaseSize({
    tokenPairId: pair.tokenPairId,
    isLong: pair.isLong,
    inputCoin: true,
    amount: principal,
    leverage: req.multiplier,
    swapSlippage: slippage,
    leverageMarketId: pair.leverageMarketIndex,
  });

  const lm = networkConfig.leverageMarkets[pair.leverageMarketIndex];
  if (!lm) throw new HttpError(400, `leverage market index ${pair.leverageMarketIndex} not found`);

  const collateralType = with0x(pair.collateralCoinType);
  const borrowType = with0x(pair.borrowCoinType);

  const prices = await lending.fetchPythPrices([collateralType, borrowType]);
  const oraclePrices = new Map<string, number>();
  const decimalPlaces = new Map<string, number>();
  for (const t of [collateralType, borrowType]) {
    const p = prices.get(t) ?? prices.get(t.replace(/^0x/, ''));
    if (p) oraclePrices.set(t, p.asNumber());
    decimalPlaces.set(t, coinDecimals(t));
  }

  const tx = new Transaction();
  tx.setSender(req.sender);
  const coin = tx.coin({ type: collateralType, balance: principal });

  await client.openAndApplyOperation(
    tx as any,
    pair.leverageMarketIndex,
    coin,
    quoted.operation,
    quoted.dexQuote.exchange,
    slippage,
    req.sender,
    oraclePrices,
    decimalPlaces,
  );

  const txBytes = await tx.build({ client: (await import('./context.js')).rpc });
  const simulation = await simulate(txBytes);

  // Projected position risk from the quote (SDK's own mocked-obligation math).
  let projected: LtvSummary | null = null;
  let liquidationPriceUsd: number | null = null;
  try {
    const snapshot = await lending.getEmodeGroupMarketSnapshot(lm.lendingMarketType, lm.emodeId);
    const marketInfo = networkConfig.markets.find((m) => m.type === lm.lendingMarketType)!;
    const market = new Market(marketInfo.type, marketInfo.objectId, snapshot.assets, snapshot.emodeGroups, coinMetadatas);
    const mocked = LeverageObligation.createMockedFromQuote(quoted, principal, collateralType, market);
    projected = ltvSummary(mocked.lendingMarketObligation, market);
    liquidationPriceUsd = collateralLiquidationPrice(
      mocked.lendingMarketObligation, market, quoted.operation.collateralCoin,
    ).asNumber();
  } catch {
    projected = null;
  }

  return {
    txBytesBase64: toBase64(txBytes),
    simulation,
    quote: {
      totalCollateral: quoted.operation.totalCollateral.toString(),
      totalDebt: quoted.operation.totalDebt.toString(),
      dexPriceImpact: quoted.dexQuote.priceImpact,
    },
    projected,
    liquidationPriceUsd,
    currentPriceUsd: oraclePrices.get(collateralType) ?? null,
    collateralCoinType: collateralType,
    borrowCoinType: borrowType,
  };
}
