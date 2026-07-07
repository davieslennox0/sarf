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

module.exports = {
  apps: [
    {
      name: 'sarf-txbuilder',
      cwd: __dirname + '/txbuilder',
      script: 'npx',
      args: 'tsx src/index.ts', // vendor SDK ships Bundler-style ESM; tsx is the supported runtime (see txbuilder/tsconfig.json)
      env,
      max_restarts: 10,
      restart_delay: 3000,
    },
    {
      name: 'sarf-server',
      cwd: __dirname + '/server',
      script: '.venv/bin/python',
      args: '-m sarf.main',
      env,
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
