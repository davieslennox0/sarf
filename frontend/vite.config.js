import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Served by FastAPI from the root in production; /api proxied in dev.
//
// base is '/' because the site moved off /dashboard. Leaving it there made
// every chunk load through the legacy 308 redirect, which Privy's ~246
// code-split chunks turn into 246 extra round trips.
export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8760',
    },
  },
});
