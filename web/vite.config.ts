import fs from 'node:fs'
import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import type { Plugin } from 'vite'
import { defineConfig } from 'vitest/config'

// The mock's service worker lives in public/ for `dev:mock` only; drop it from builds.
const noMockWorker = (): Plugin => ({
  name: 'no-mock-worker', apply: 'build',
  writeBundle: (o) => fs.rmSync(path.join(o.dir!, 'mockServiceWorker.js'), { force: true }),
})

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss(), noMockWorker()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: {
    port: 5174, strictPort: true,
    // dev: the local web adapter. dev:mock: MSW in the browser, no proxy.
    proxy: mode === 'mock'
      ? {}
      : Object.fromEntries(['/api', '/auth'].map((p) => [p, { target: process.env.DAIMON_WEB_API ?? 'http://localhost:8780' }])),
  },
  test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], include: ['src/**/*.test.{ts,tsx}'], restoreMocks: true },
  build: {
    target: 'es2022', outDir: process.env.DAIMON_WEB_OUT ?? 'dist', emptyOutDir: true,
    // The chat entry is what everyone loads; a chunk past this fails the review. The layout worker is an asset, not a chunk.
    chunkSizeWarningLimit: 600,
  },
}))
