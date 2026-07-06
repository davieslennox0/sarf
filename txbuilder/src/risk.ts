/**
 * LTV / liquidation-price math for proposal risk notes.
 *
 * collateralLiquidationPrice / debtLiquidationPrice are ported from the
 * vendor SDK's examples/lending/liquidation-price.ts (same formulas the
 * protocol UI uses); projections reuse Obligation.applyOperation so the
 * "after" numbers come from the SDK's own accounting, not our re-derivation.
 */
import {
  Decimal,
  Market,
  Obligation,
  Operation,
  OperationName,
  parseCoinDecimals,
  type TypeName,
} from '@current-finance/current-sdk';

/** Price lookup tolerant of 0x-prefixed vs bare struct tags. */
export function priceFor(prices: Map<TypeName, Decimal>, coinType: string): Decimal | undefined {
  return (
    prices.get(coinType) ??
    prices.get(coinType.startsWith('0x') ? coinType.slice(2) : `0x${coinType}`)
  );
}

function liquidationWeightedCollateral(
  obligation: Obligation,
  market: Market,
  excludeType?: TypeName,
): Decimal {
  let total = Decimal.zero();
  for (const asset of obligation.depositAssets()) {
    if (excludeType !== undefined && asset === excludeType) continue;
    const emode = market.findAssetEmodeParams(obligation.getEmodeGroupId(), asset);
    if (!emode || emode.collateralFactor.equals(Decimal.zero())) continue;
    total = total.add(obligation.getDeposit(asset).usdValue.mul(emode.liquidationFactor));
  }
  return total;
}

/** USD price of `collateralType` at which the obligation becomes liquidatable (price falling). */
export function collateralLiquidationPrice(
  obligation: Obligation,
  market: Market,
  collateralType: TypeName,
): Decimal {
  const totalBorrow = obligation.totalBorrowUsdWeighted(market);
  const otherCollateral = liquidationWeightedCollateral(obligation, market, collateralType);
  const liquidationFactor = market.assetEmodeParams(obligation.getEmodeGroupId(), collateralType).liquidationFactor;
  const amount = Decimal.fromBigInt(obligation.getDeposit(collateralType).amount());
  const decimals = parseCoinDecimals(market.coinDecimal(collateralType));
  const mul = amount.divDecimal(decimals).mul(liquidationFactor);
  return mul.isZero() ? Decimal.zero() : totalBorrow.sub(otherCollateral).divDecimal(mul);
}

/** USD price of `debtType` at which the obligation becomes liquidatable (price rising). */
export function debtLiquidationPrice(
  obligation: Obligation,
  market: Market,
  debtType: TypeName,
): Decimal {
  const maxBorrowValue = liquidationWeightedCollateral(obligation, market);
  const otherDebt = obligation.totalBorrowUsdWeighted(market, new Set([debtType]));
  const borrowWeight = market.assetEmodeParams(obligation.getEmodeGroupId(), debtType).borrowWeight;
  const amount = Decimal.fromBigInt(obligation.getBorrow(debtType, market).amount());
  const decimals = parseCoinDecimals(market.coinDecimal(debtType));
  if (amount.isZero()) return Decimal.zero();
  return maxBorrowValue.sub(otherDebt).mul(decimals).divDecimal(amount).divDecimal(borrowWeight);
}

export interface LtvSummary {
  currentLtv: number;
  maxLtv: number;
  liquidationLtv: number;
  netValueUsd: number;
}

export function ltvSummary(obligation: Obligation, market: Market): LtvSummary {
  return {
    currentLtv: obligation.currentLTV(market).asNumber(),
    maxLtv: obligation.maxLTV(market).asNumber(),
    liquidationLtv: obligation.liquidationLTV(market).asNumber(),
    netValueUsd: obligation.netValue().asNumber(),
  };
}

const OP_BY_ACTION: Record<string, OperationName> = {
  deposit: OperationName.Deposit,
  withdraw: OperationName.Withdraw,
  borrow: OperationName.Borrow,
  repay: OperationName.Repay,
};

export interface RiskProjection {
  before: LtvSummary;
  after: LtvSummary;
  liquidationPrices: Array<{
    coinType: string;
    side: 'collateral' | 'debt';
    liquidationPriceUsd: number;
    currentPriceUsd: number | null;
  }>;
}

/**
 * Project the obligation state after applying `action(amount)` and report
 * before/after LTVs plus per-asset liquidation prices on the "after" state.
 */
export function projectRisk(
  obligation: Obligation,
  market: Market,
  action: 'deposit' | 'withdraw' | 'borrow' | 'repay',
  coinType: TypeName,
  amountMinUnits: bigint,
  currentPrices: Map<TypeName, Decimal>,
): RiskProjection {
  const after = obligation.applyOperation(
    Operation.from(OP_BY_ACTION[action], coinType, Decimal.fromBigInt(amountMinUnits)),
    market,
  );

  const liquidationPrices: RiskProjection['liquidationPrices'] = [];
  for (const asset of after.depositAssets()) {
    liquidationPrices.push({
      coinType: asset,
      side: 'collateral',
      liquidationPriceUsd: collateralLiquidationPrice(after, market, asset).asNumber(),
      currentPriceUsd: priceFor(currentPrices, asset)?.asNumber() ?? null,
    });
  }
  for (const asset of after.borrowedAssets()) {
    liquidationPrices.push({
      coinType: asset,
      side: 'debt',
      liquidationPriceUsd: debtLiquidationPrice(after, market, asset).asNumber(),
      currentPriceUsd: priceFor(currentPrices, asset)?.asNumber() ?? null,
    });
  }

  return {
    before: ltvSummary(obligation, market),
    after: ltvSummary(after, market),
    liquidationPrices,
  };
}
