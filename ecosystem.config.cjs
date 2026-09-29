// pm2 process file — same pattern as the other Vultr deployments.
// Load order matters only in that the Python server retries the sidecar
// lazily, so no wait-for hooks are needed.
//
// .env is parsed here (not via pm2's env_file) so behaviour doesn't depend
// on the pm2 version, and inline "# comments" never leak into values that
// the Python config int()/float()s.
const fs = require('fs');

function dotenv(file) {
  const env = {};
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return env; // no .env yet — apps fall back to their built-in defaults
  }
  for (const raw of text.split('\n')) {
    const m = raw.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue;
    const quoted = m[2].match(/^(['"])(.*)\1\s*$/);
    env[m[1]] = quoted ? quoted[2] : m[2].replace(/\s+#.*$/, '').trim();
  }
  return env;
}

const env = dotenv(__dirname + '/.env');

// X Layer is an ordinary EVM chain reached over JSON-RPC, and the DEX
// aggregator is an HTTP API — so there is no Node sidecar any more. The Sui
// build needed one only to host the Current Finance SDK.
// Earn is the same code as sarf-server, deliberately deployed twice.
//
// It gets its own port and its own DATABASE, which is what keeps it from
// touching anything the live app is responsible for: separate positions,
// separate sessions, separate watcher. That isolation has one visible cost —
// an Earn session is a different credential, so the site asks for one more
// wallet signature on /earn — and it buys the thing that matters, which is
// that a bug in the new build cannot reach an open position in the old one.
//
// Only ONE watcher may ever run against a given database. sarf-server watches
// data/sarf.db; this watches data/earn.db; neither can see the other's rows.
const earnEnv = {
  ...env,
  SARF_PORT: '8761',
  SARF_DB_PATH: './data/earn.db',
  // Signing links and the connector URL have to point at the Earn paths, not
  // the main app's, or a user would be sent to a server that has never heard
  // of their position.
  SARF_PUBLIC_URL: (env.SARF_PUBLIC_URL || 'https://getsarf.xyz') + '/earn',
  // The 7702 keeper stays off here too. Deployed is not enabled.
  ZAP_7702_ENABLED: 'false',
};

module.exports = {
  apps: [
    {
      name: 'sarf-server',
      cwd: __dirname + '/server',
      script: '.venv/bin/python',
      args: '-m sarf.main',
      env,
      max_restarts: 10,
      restart_delay: 3000,
    },
    {
      name: 'earn-server',
      cwd: __dirname + '/server',
      script: '.venv/bin/python',
      args: '-m sarf.main',
      env: earnEnv,
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
