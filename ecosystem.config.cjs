// pm2 process file — same pattern as the other Vultr deployments.
// Load order matters only in that the Python server retries the sidecar
// lazily, so no wait-for hooks are needed.
module.exports = {
  apps: [
    {
      name: 'suiflow-txbuilder',
      cwd: __dirname + '/txbuilder',
      script: 'npx',
      args: 'tsx src/index.ts', // vendor SDK ships Bundler-style ESM; tsx is the supported runtime (see txbuilder/tsconfig.json)
      env_file: __dirname + '/.env',
      max_restarts: 10,
      restart_delay: 3000,
    },
    {
      name: 'suiflow-server',
      cwd: __dirname + '/server',
      script: '.venv/bin/python',
      args: '-m suiflow.main',
      env_file: __dirname + '/.env',
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
