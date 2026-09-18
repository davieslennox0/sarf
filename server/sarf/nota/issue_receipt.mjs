// Thin Node shim around the real Nota SDK (github.com/davieslennox0/nota,
// package @sykeclone/nota-sdk). Sarf's server is Python; the SDK is not, and
// there is no write-capable REST API to call instead (nota-gateway is
// read-only -- see nota_client.py). This script is invoked as a subprocess by
// nota_client.issue_receipt(), one JSON request on argv[2], one JSON result on
// stdout, so the Python side never has to embed a JS runtime.
//
// NOT run automatically: Sarf's config defaults NOTA_ENABLED=false, and even
// enabled this requires `npm install @sykeclone/nota-sdk` in this directory
// plus a funded Sui signer -- neither is provisioned by this change. See the
// comment on Settings.nota_enabled in config.py.

import { WalrusReceipts } from '@sykeclone/nota-sdk';

async function main() {
  const req = JSON.parse(process.argv[2]);
  const nota = new WalrusReceipts({
    suiRpcUrl: req.suiRpcUrl,
    privateKey: req.privateKey,
    packageId: req.packageId,
    registryId: req.registryId,
    walrusAggregatorUrl: req.walrusAggregatorUrl,
    walrusPublisherUrl: req.walrusPublisherUrl,
    namespace: req.namespace,
  });

  // Idempotent on Nota's side per their docs ("register once"); calling it
  // every issuance is a deliberate simplification for a 30-minute pass, not a
  // load-bearing assumption -- tighten to register-once-and-cache if this
  // ships for real.
  await nota.registerProtocol();

  const receipt = await nota.issue({
    txHash: req.txHash,
    asset: req.asset,
    action: req.action,
    amount: req.amount,
    currency: req.currency,
    recipient: req.recipient,
  });

  process.stdout.write(JSON.stringify({
    ok: true,
    receipt_id: receipt.receiptId ?? receipt.receipt_id ?? null,
    blob_id: receipt.blobId,
    view_url: receipt.viewUrl,
    tx_digest: receipt.txDigest,
  }));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
  process.exit(1);
});
