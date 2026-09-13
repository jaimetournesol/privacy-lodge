import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const HUB = 'http://127.0.0.1:4400';

export default defineConfig({
  root: 'web',
  plugins: [react()],
  optimizeDeps: {
    // Dynamically imported libs must be pre-bundled up front — discovering
    // them mid-session re-optimizes with a new hash and wedges the module graph.
    include: [
      'react',
      'react-dom/client',
      'react-markdown',
      'remark-gfm',
      'fast-json-patch',
      'chart.js/auto',
      'modern-screenshot',
      'shiki',
      'mermaid',
    ],
  },
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: 'ws://127.0.0.1:4400', ws: true },
      '^/api/': { target: HUB },
      '/assets': HUB,
      '/apps': HUB,
      '/upload': HUB,
      '/__surface': HUB,
      '/debug': HUB,
      '/healthz': HUB,
    },
  },
  // assetsDir is NOT 'assets': the hub owns /assets/<token>/ for registered files.
  build: { outDir: 'dist', emptyOutDir: true, assetsDir: 'shell' },
});
