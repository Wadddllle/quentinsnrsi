import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  // Dev-only: proxy /api to the FastAPI backend (python -m sbg.ui --dev) so
  // relative fetch('/api/...') calls in components work identically in dev
  // and in production (where FastAPI serves the built bundle itself, so
  // /api is same-origin for free). Without this, Vite's dev server serves
  // its own index.html for any unmatched path, and fetch('/api/...') gets
  // back HTML instead of JSON.
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8010',
    },
  },
  // cityjson-threejs-loader spawns a Web Worker via new Worker(new URL(...)),
  // a pattern Vite's own dev transform pipeline understands but esbuild's
  // dependency pre-bundler does not -- excluding it from pre-bundling lets
  // Vite serve it natively instead of esbuild silently mishandling the
  // worker's relative URL (confirmed: pre-bundled, it 404s on the worker
  // file and the whole parse pipeline fails).
  optimizeDeps: {
    exclude: ['cityjson-threejs-loader'],
    // earcut is a plain CommonJS package (no "type": "module", no "exports"
    // field) pulled in transitively by cityjson-threejs-loader. Excluding
    // the parent from pre-bundling also skips esbuild's CJS-&gt;ESM interop
    // for earcut, since that interop is exactly what pre-bundling normally
    // provides -- confirmed via "does not provide an export named 'default'"
    // when earcut was left un-pre-bundled. Forcing it back in fixes just
    // that dependency without re-enabling the worker-URL problem above.
    include: ['earcut'],
  },
})
