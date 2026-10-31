// Dev server for a manual visual check of the validation card (never builds dist): own port (8518, or VAL_VITE_PORT),
// own Vite cache (backend/.tmp/validation/, or VAL_VITE_CACHE), API proxied to a backend started on port 8517 (or
// NEXUS_BACKEND).
// From frontend/:  node_modules/.bin/vite --config tests/imp-validation.vite.config.mjs
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const BACKEND = process.env.NEXUS_BACKEND ?? 'http://127.0.0.1:8517'

export default defineConfig({
  root: fileURLToPath(new URL('..', import.meta.url)),
  cacheDir: process.env.VAL_VITE_CACHE || fileURLToPath(new URL('../../backend/.tmp/validation/vite-cache', import.meta.url)),
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: Number(process.env.VAL_VITE_PORT || 8518),
    strictPort: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: false },
      '/ws': { target: BACKEND.replace(/^http/, 'ws'), ws: true },
    },
  },
})
