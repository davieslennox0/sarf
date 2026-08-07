// Sarf under PM2. Both processes were PM2-managed once -- the orphans still carry
// PM2_HOME in their environment -- but were left running loose when the daemon died,
// so nothing has restarted them on failure since.
//
// The env block is not decoration. txbuilder/src/index.ts reads process.env directly
// and never calls dotenv, and the server runs from /root/sarf/server while .env lives
// one level up at /root/sarf/.env. The loose processes only had their config because
// they inherited it from the shell that launched them. Start them without this and
// they come back silently on defaults -- 127.0.0.1 hosts, public RPC URLs, no session
// secret -- rather than failing loudly, which is the worst way for a trading service
// to be wrong.
//
// Parsed from .env at load time rather than copied inline so SARF_SESSION_SECRET and
// friends never get written into a file that could be committed.
const fs = require('fs');

function envFrom(file) {
  const out = {};
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*)$/);
    if (m) out[m[1]] = m[2].trim().replace(/^["']|["']$/g, '');
  }
  return out;
}

const sarfEnv = envFrom('/root/sarf/.env');

module.exports = {
  apps: [
    {
      name: 'sarf-server',
      cwd: '/root/sarf/server',
      script: '/root/sarf/server/.venv/bin/python',
      args: '-m sarf.main',
      interpreter: 'none',
      max_memory_restart: '250M',
      env: { ...sarfEnv, PYTHONUNBUFFERED: '1' },
    },
    {
      // Stable fnm install dir, not the /root/.local/state/fnm_multishells/<pid>_<ts>
      // npx the old boot dump pointed at -- that path belongs to one shell instance and
      // is the same trap that left okx-a2a.service and pm2-root.service broken.
      name: 'sarf-txbuilder',
      cwd: '/root/sarf/txbuilder',
      script: '/root/.local/share/fnm/node-versions/v22.23.1/installation/bin/npx',
      args: 'tsx src/index.ts',
      interpreter: 'none',
      max_memory_restart: '250M',
      env: { ...sarfEnv },
    },
  ],
};
