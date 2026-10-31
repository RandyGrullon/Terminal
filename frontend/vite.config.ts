import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = process.env.NEXUS_BACKEND ?? 'http://127.0.0.1:8420'

// Third-party code in long-lived chunks of its own (an app change does not invalidate them), each one downloaded only
// with the sections that use it: React with the first load, lightweight-charts with the sections that draw charts, the
// CodeMirror editor stack only with PyChart (Studio). The icons in use go together (~8 kB gzip, with the first load):
// left to Rollup they become a dozen sub-kB files per section. The app code is split along the lazy pages (App.tsx).
const VENDOR_CHUNKS: readonly (readonly [chunk: string, packages: RegExp])[] = [
  ['vendor-react', /\/node_modules\/(?:react|react-dom|scheduler)\//],
  ['vendor-icons', /\/node_modules\/lucide-react\//],
  ['vendor-charts', /\/node_modules\/(?:lightweight-charts|fancy-canvas)\//],
  ['vendor-codemirror', /\/node_modules\/(?:@codemirror|@lezer|@uiw|@marijn)\/|\/node_modules\/(?:codemirror|crelt|style-mod|w3c-keyname)\//],
]

/** Rollup `manualChunks`: the vendor chunk of a module id (Windows or POSIX path, CommonJS proxy ids too), if any. */
export function vendorChunk(id: string): string | undefined {
  const path = id.replace(/\\/g, '/')
  return VENDOR_CHUNKS.find(([, packages]) => packages.test(path))?.[0]
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: false },
      '/ws': { target: BACKEND.replace(/^http/, 'ws'), ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: { output: { manualChunks: vendorChunk } },
    // kB per chunk. The largest one is vendor-codemirror (~480 kB, PyChart only); all the others stay under 200 kB.
    // A chunk past this limit is a regression to look at, not something to silence by raising it.
    chunkSizeWarningLimit: 500,
  },
})
