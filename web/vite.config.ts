import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Static bundle for AliCloud OSS + CDN. `base: './'` → relative asset paths so the
// build can live under any CDN prefix/bucket path. The API base is configured at
// runtime via VITE_API_BASE (see src/lib/api.ts), NOT baked into the host.
//
// NOTE: the `server.proxy` block below is LOCAL-DEV / live-test only (uncommitted).
// It routes API + SSE calls to the local board on :8080 so the whole demo is reachable
// through a single public port (:5173) — no need to expose :8080. `VITE_API_BASE=''`
// (.env.local) makes the client use relative paths that hit this proxy.
const _api = 'http://localhost:8080';
const _p = (ws = false) => ({ target: _api, changeOrigin: true, ws });

export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: { outDir: 'dist', sourcemap: false },
  server: {
    host: true,
    proxy: {
      '/negotiate_text': _p(), '/negotiate': _p(), '/confirm': _p(), '/refine': _p(),
      '/replan': _p(), '/place_card': _p(), '/place_photo': _p(), '/hotel_geo': _p(),
      '/cancel': _p(), '/trips': _p(), '/session': _p(), '/preferences': _p(),
      '/emergencies': _p(), '/aftercare': _p(), '/health': _p(), '/reconsider_leg': _p(),
      // SSE: stream must not be websocket-upgraded; http-proxy passes the event stream through.
      '/stream': { target: _api, changeOrigin: true, ws: false },
    },
  },
});
