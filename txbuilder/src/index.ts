/**
 * SuiFlow tx-builder sidecar.
 *
 * SECURITY: binds to loopback ONLY (TXBUILDER_HOST, default 127.0.0.1). It is
 * an internal service for the Python MCP server, performs no authentication
 * of its own, and must never be reverse-proxied to the internet. It holds no
 * keys and cannot move funds: outputs are unsigned transaction bytes, and the
 * broadcast endpoint only relays signatures created in the user's wallet.
 */
import Fastify from 'fastify';
import {
  Decimal,
  Market,
  coinMetadatas,
} from '@current-finance/current-sdk';
import {
  HttpError,
  coinSymbol,
  coinDecimals,
  getCapDetailChecked,
  getObjectOwner,
  lending,
  loadObligationByCap,
  marketByName,
  networkConfig,
  with0x,
} from './context.js';
import { buildLendingAction, broadcast, type BuildRequest } from './build.js';
import { buildLeverageOpen, type LeverageBuildRequest } from './leverage.js';
import { ltvSummary, collateralLiquidationPrice, debtLiquidationPrice } from './risk.js';

const HOST = process.env.TXBUILDER_HOST ?? '127.0.0.1';
const PORT = Number(process.env.TXBUILDER_PORT ?? 8761);

const app = Fastify({ logger: true, bodyLimit: 256 * 1024 });

app.setErrorHandler((err: Error, _req, reply) => {
  const status = err instanceof HttpError ? err.statusCode : ((err as any).statusCode ?? 500);
  reply.status(typeof status === 'number' ? status : 500).send({ error: err.message });
});

app.get('/health', async () => ({ ok: true, service: 'suiflow-txbuilder' }));

/** Static market/asset registry (names, coin types, symbols, decimals). */
app.get('/markets', async () => ({
  markets: networkConfig.markets.map((m) => ({
    name: m.name,
    type: m.type,
    objectId: m.objectId,
    assets: (m.emodeGroups.find((g) => g.emodeId === 0)?.assets ?? []).map((a) => ({
      coinType: with0x(a),
      symbol: coinSymbol(a),
      decimals: coinDecimals(a),
    })),
  })),
}));

/** Live rates + risk params for one market (emode group 0 = default params). */
app.get<{ Querystring: { market?: string } }>('/market-info', async (req) => {
  const info = marketByName(req.query.market ?? 'MainMarket');
  const snapshot = await lending.getEmodeGroupMarketSnapshot(info.type, 0);
  const market = new Market(info.type, info.objectId, snapshot.assets, snapshot.emodeGroups, coinMetadatas);

  const assets = market.assets().map((cfg) => {
    const util = market.utilizationRate(cfg.coinType);
    const emode = market.findAssetEmodeParams(0, cfg.coinType);
    // Per-second rates on-chain; annualize for display.
    const secondsPerYear = 365 * 24 * 3600;
    return {
      coinType: with0x(cfg.coinType),
      symbol: coinSymbol(cfg.coinType),
      decimals: coinDecimals(cfg.coinType),
      priceUsd: cfg.depositUsage.price().asNumber(),
      utilization: util.asNumber(),
      borrowAprPct: cfg.borrowInterestRate(util).asNumber() * secondsPerYear * 100,
      supplyAprPct: cfg.depositInterestRate(util, Decimal.zero()).asNumber() * secondsPerYear * 100,
      collateralFactor: emode?.collateralFactor.asNumber() ?? null,
      liquidationFactor: emode?.liquidationFactor.asNumber() ?? null,
      borrowWeight: emode?.borrowWeight.asNumber() ?? null,
      paused: {
        deposit: cfg.depositPaused,
        borrow: cfg.borrowPaused,
        withdraw: cfg.withdrawPaused,
      },
      totalDepositedUsd: cfg.depositUsage.usdValue.asNumber(),
      totalBorrowedUsd: cfg.borrowUsage.usdValue.asNumber(),
    };
  });

  return { market: info.name, marketType: info.type, assets };
});

/** Cap resolution + on-chain owner — the Python layer's ownership check. */
app.get<{ Params: { capId: string } }>('/cap/:capId', async (req) => {
  const detail = await getCapDetailChecked(req.params.capId);
  const owner = await getObjectOwner(req.params.capId);
  return {
    capId: req.params.capId,
    owner,
    obligationId: detail.obligationId,
    marketType: with0x(detail.marketType),
    marketId: detail.marketId,
  };
});

/** Full position view for an address: owned obligation caps + balances + LTVs. */
app.get<{ Params: { address: string } }>('/portfolio/:address', async (req) => {
  const address = req.params.address;
  // Obligation caps are owned objects from the protocol package; filter by
  // package then by type name so we don't depend on the exact module path.
  const owned = await (await import('./context.js')).rpc.getOwnedObjects({
    owner: address,
    filter: { Package: networkConfig.protocolPackageId },
    options: { showType: true },
  });
  const caps = (owned.data ?? []).filter((o: any) =>
    (o.data?.type ?? '').includes('ObligationOwnerCap'),
  );

  const positions = [];
  for (const cap of caps) {
    const capId = (cap as any).data.objectId as string;
    try {
      const loaded = await loadObligationByCap(capId);
      const summary = ltvSummary(loaded.obligation, loaded.market);
      positions.push({
        obligationCapId: capId,
        obligationId: loaded.obligationId,
        market: loaded.marketInfo.name,
        emodeGroupId: loaded.data.emodeGroupId,
        ltv: summary,
        deposits: loaded.data.deposits.map((d) => ({
          coinType: with0x(d.coinType),
          symbol: coinSymbol(d.coinType),
          amountMinUnits: d.amount().toString(),
          amount: Number(d.amount()) / 10 ** coinDecimals(d.coinType),
          usdValue: d.usdValue.asNumber(),
          liquidationPriceUsd: collateralLiquidationPrice(loaded.obligation, loaded.market, d.coinType).asNumber(),
        })),
        borrows: loaded.data.borrows.map((b) => ({
          coinType: with0x(b.coinType),
          symbol: coinSymbol(b.coinType),
          amountMinUnits: b.amount().toString(),
          amount: Number(b.amount()) / 10 ** coinDecimals(b.coinType),
          usdValue: b.usdValue.asNumber(),
          liquidationPriceUsd: debtLiquidationPrice(loaded.obligation, loaded.market, b.coinType).asNumber(),
        })),
      });
    } catch (e: any) {
      positions.push({ obligationCapId: capId, error: e.message });
    }
  }
  return { address, positions };
});

app.post<{ Body: BuildRequest }>('/build', async (req) => buildLendingAction(req.body));

app.post<{ Body: LeverageBuildRequest }>('/build/leverage', async (req) => buildLeverageOpen(req.body));

app.post<{ Body: { txBytesBase64: string; signatures: string[] } }>('/broadcast', async (req) => {
  const { txBytesBase64, signatures } = req.body;
  if (!txBytesBase64 || !Array.isArray(signatures) || signatures.length === 0) {
    throw new HttpError(400, 'txBytesBase64 and signatures[] required');
  }
  return broadcast(txBytesBase64, signatures);
});

app.listen({ host: HOST, port: PORT }).then(() => {
  app.log.info(`txbuilder listening on ${HOST}:${PORT} (loopback-only by design)`);
});
