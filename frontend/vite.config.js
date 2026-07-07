import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Served by FastAPI at /dashboard/ in production; /api proxied in dev.
export default defineConfig({
  plugins: [react()],
  base: '/dashboard/',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8760',
    },
  },
});
