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

// dev:mock only: serves /a/<id>/<slug> from the sample pages in src/mock/artifacts, so previews render.
// `<slug>.v<N>.html` stands in for older versions: `?v=N` takes the nearest one at or above N.
const mockArtifacts = (): Plugin => ({
  name: 'mock-artifacts',
  apply: (_, env) => env.command === 'serve' && env.mode === 'mock',
  configureServer(server) {
    const dir = path.join(import.meta.dirname, 'src/mock/artifacts')
    server.middlewares.use((req, res, next) => {
      const url = new URL(req.url ?? '', 'http://x')
      const m = /^\/a\/[a-z2-7]+\/([\w-]+)\/?$/.exec(url.pathname)
      if (!m) return next()
      const files = fs.readdirSync(dir)
      const old = Number(url.searchParams.get('v')) || 0
      const variant = files.map((f) => new RegExp(`^${m[1]}\\.v(\\d+)\\.html$`).exec(f)).filter((x) => x && Number(x[1]) >= old).sort((a, b) => Number(a![1]) - Number(b![1]))[0]
      const file = old && variant ? variant[0] : `${m[1]}.html`
      if (!files.includes(file)) return next()
      res.setHeader('Content-Type', 'text/html; charset=utf-8')
      if (url.searchParams.has('download')) res.setHeader('Content-Disposition', `attachment; filename="${m[1]}.html"`)
      res.end(fs.readFileSync(path.join(dir, file)))
    })
  },
})

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss(), noMockWorker(), mockArtifacts()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, './src') } },
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
