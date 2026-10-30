import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = process.env.NEXUS_BACKEND ?? 'http://127.0.0.1:8420'

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
    chunkSizeWarningLimit: 3000,
  },
})
