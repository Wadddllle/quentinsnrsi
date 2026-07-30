import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev-only: proxy /api to the lean v2 FastAPI backend
// (python -m sbg.onemap_native.ui --dev --port 8011). In production FastAPI
// serves the built dist/ itself, so /api is same-origin for free.
export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': 'http://127.0.0.1:8011' } },
  // earcut is plain CommonJS -- pre-bundle it so its default export resolves
  // as an ESM import (the same reason v1's config needed this).
  optimizeDeps: { include: ['earcut'] },
})
