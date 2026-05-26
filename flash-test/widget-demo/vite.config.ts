import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const HEX_DIR = join(__dirname, '..', 'test-hexes');

// Serve /test-hexes/* from the sibling test-hexes/ dir without copying.
function serveHexes() {
  return {
    name: 'serve-hexes',
    configureServer(server: any) {
      server.middlewares.use('/test-hexes/', (req: any, res: any, next: any) => {
        const m = /^\/([\w.-]+\.hex)$/.exec(req.url || '');
        if (!m) return next();
        const file = join(HEX_DIR, m[1]);
        if (!existsSync(file)) {
          res.statusCode = 404;
          res.end('not built — see ../build-test-hexes.mjs');
          return;
        }
        res.setHeader('Content-Type', 'text/plain; charset=utf-8');
        res.end(readFileSync(file, 'utf-8'));
      });
    },
  };
}

// The widget package exports raw .ts/.svelte sources, so Svelte + TS plugins
// are required to compile-on-demand at dev-server time. No build step needed
// for the widget itself — Vite reads its src/ directly via the package link.
export default defineConfig({
  plugins: [svelte(), serveHexes()],
  server: {
    port: 5179,
    host: '127.0.0.1',
  },
  resolve: {
    // The widget uses Svelte 5; force a single copy so component context
    // matches.
    dedupe: ['svelte'],
  },
});
