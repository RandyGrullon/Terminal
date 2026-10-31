// Regression tests of fe-perf (improvement 13: the 1.35 MB bundle split so the first load only brings what the first
// page needs). No network, no browser, nothing written to frontend/dist:
// - module graph (esbuild metafile of src/main.tsx): the first load (main.tsx and its static imports) has no page, no
//   CodeMirror and no lightweight-charts; App.tsx imports every page with import(); the CodeMirror stack is reachable
//   only from PyChart (StudioPage), through components/studio/CodeEditor.tsx;
// - vite.config.ts: the manualChunks vendor groups (Windows / POSIX / CommonJS ids) and an honest warning limit;
// - App.tsx, bundled with the pages and the heavy shell parts replaced by stubs: the section in the URL starts
//   downloading when the app loads, lazy pages render through Suspense, prefetch and visit share one download, a
//   failed download is a PageLoadError the page boundary explains (with a reload), render errors keep "Reintentar",
//   the delayed loading state, the menu prefetch (hover / focus) only while the server is reachable;
// - styles: the sections' CSS stays in the first-load stylesheet in the pre-split order (styles.css last).
// The real Vite build is measured with tests/imp-fe-perf.measure.mjs (not part of npm test); here only its refusal of an
// output folder that Vite would wipe is checked (it exits before building).
// Run: cd frontend && node --test tests/imp-fe-perf.test.mjs
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SRC = path.join(ROOT, 'src')
const posix = (p) => p.replace(/\\/g, '/')
const toDataUrl = (code) => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const flush = async (rounds = 5) => {
  for (let i = 0; i < rounds; i++) await new Promise((r) => setTimeout(r, 0))
}

// Browser globals touched at import time by store.ts / auth/session.ts (nothing is mounted). The app "opens" on
// #/studio: that section must start downloading as soon as App.tsx is evaluated.
const memory = new Map()
const reloads = []
globalThis.window = globalThis
globalThis.addEventListener ??= () => {}
globalThis.removeEventListener ??= () => {}
globalThis.navigator ??= { userAgent: 'node', language: 'es-ES' }
globalThis.location ??= { hash: '#/studio', reload: () => void reloads.push(Date.now()) }
globalThis.history ??= { state: null, replaceState() {}, pushState() {} }
globalThis.localStorage ??= {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null),
  setItem: (k, v) => void memory.set(k, String(v)),
  removeItem: (k) => void memory.delete(k),
}
globalThis.__impPagesLoaded = []

// ----------------------------------------------------------------------------- module graph of the real sources

const CODEMIRROR = /^node_modules\/(?:@codemirror|@lezer|@uiw|@marijn|codemirror|crelt|style-mod|w3c-keyname)\//
const CHARTS = /^node_modules\/(?:lightweight-charts|fancy-canvas)\//
const PAGE_FILES = ['BrainPage', 'ChartPage', 'MarketsPage', 'PaperPage', 'ScannerPage', 'SettingsPage', 'SimulationPage', 'StudioPage'].map(
  (p) => `src/pages/${p}.tsx`,
)

const graph = await build({
  entryPoints: [path.join(SRC, 'main.tsx')],
  absWorkingDir: ROOT,
  bundle: true,
  write: false,
  metafile: true,
  format: 'esm',
  platform: 'browser',
  jsx: 'automatic',
  outdir: path.join(tmpdir(), 'imp-fe-perf-graph'), // nothing is written (write: false)
  loader: { '.css': 'css' },
  define: { 'import.meta.env': '{}', 'process.env.NODE_ENV': '"production"' },
  logLevel: 'silent',
})
const inputs = Object.fromEntries(Object.entries(graph.metafile.inputs).map(([id, v]) => [posix(id), v]))

/** Modules a file brings with it before running: its static imports, transitively (import() not followed). */
function staticClosure(start) {
  const seen = new Set()
  const stack = [start]
  while (stack.length) {
    const id = stack.pop()
    if (seen.has(id) || !inputs[id]) continue
    seen.add(id)
    for (const imp of inputs[id].imports) if (!imp.external && imp.kind !== 'dynamic-import') stack.push(posix(imp.path))
  }
  return seen
}

test('first load: the shell without any section, CodeMirror or lightweight-charts', () => {
  const first = [...staticClosure('src/main.tsx')]
  for (const shell of ['src/App.tsx', 'src/store.ts', 'src/components/terminal/Terminal.tsx', 'src/components/macro/MarketPulse.tsx']) {
    assert.ok(first.includes(shell), `${shell} is part of the first load`)
  }
  assert.deepEqual(first.filter((f) => f.startsWith('src/pages/')), [])
  assert.deepEqual(first.filter((f) => CODEMIRROR.test(f)), [])
  assert.deepEqual(first.filter((f) => CHARTS.test(f)), [])
})

test('App.tsx loads every section with import(), none statically', () => {
  const pageImports = inputs['src/App.tsx'].imports.filter((i) => posix(i.path).startsWith('src/pages/'))
  assert.deepEqual(pageImports.filter((i) => i.kind !== 'dynamic-import'), [])
  assert.deepEqual(pageImports.map((i) => posix(i.path)).sort(), PAGE_FILES)
})

test('the CodeMirror stack only comes with PyChart (Studio), through CodeEditor.tsx', () => {
  for (const page of PAGE_FILES) {
    const cm = [...staticClosure(page)].filter((f) => CODEMIRROR.test(f))
    if (page.endsWith('/StudioPage.tsx')) assert.ok(cm.some((f) => f.startsWith('node_modules/@codemirror/view/')), 'Studio brings the editor')
    else assert.deepEqual(cm, [], `${page} must not pull CodeMirror`)
  }
  // Type-only imports (reference.ts, diagnostics.ts) are erased: the one runtime importer in src/ is the editor.
  const importers = Object.entries(inputs)
    .filter(([id, v]) => id.startsWith('src/') && v.imports.some((i) => !i.external && CODEMIRROR.test(posix(i.path))))
    .map(([id]) => id)
  assert.deepEqual(importers, ['src/components/studio/CodeEditor.tsx'])
})

test('first-load stylesheet: the sections keep their pre-split place in the cascade (styles.css last)', () => {
  const app = readFileSync(path.join(SRC, 'App.tsx'), 'utf8')
  const css = [...app.matchAll(/^import '(\.\/components\/[^']+\.css)'$/gm)].map((m) => m[1])
  assert.deepEqual(css, [
    './components/brain/brain.css',
    './components/nexus/nexus.css',
    './components/chart/chart.css',
    './components/markets/markets.css',
    './components/paper/paper.css',
    './components/scanner/scanner.css',
    './components/settings/settings.css',
    './components/sim/sim.css',
    './components/studio/studio.css',
  ])
  for (const f of css) assert.ok(existsSync(path.join(SRC, f)), `${f} exists`)
  // Where the page imports used to be: after the shell's own styles (macro, symbol search, terminal), before store.ts.
  const at = (s) => app.indexOf(s)
  assert.ok(at("import Terminal from './components/terminal/Terminal'") < at(`import '${css[0]}'`))
  assert.ok(at(`import '${css.at(-1)}'`) < at("import { runUiAction, useApp } from './store'"))
  const main = readFileSync(path.join(SRC, 'main.tsx'), 'utf8')
  assert.ok(main.indexOf("import App from './App'") < main.indexOf("import './styles.css'"), 'styles.css still comes last')
  const first = staticClosure('src/main.tsx')
  const known = ['brain', 'nexus', 'chart', 'markets', 'paper', 'scanner', 'settings', 'sim', 'studio', 'macro', 'terminal']
  for (const name of known) assert.ok(first.has(`src/components/${name}/${name}.css`), `${name}.css is in the first load`)
  assert.ok(first.has('src/components/markets/symbolSearch.css') && first.has('src/styles.css'))
})

// ----------------------------------------------------------------------------- vite.config.ts

const viteStubs = {
  name: 'imp-fe-perf-vite-stubs',
  setup(b) {
    b.onResolve({ filter: /^(?:vite|@vitejs\/plugin-react)$/ }, (args) => ({ path: args.path, namespace: 'imp-fe-perf-vite' }))
    b.onLoad({ filter: /.*/, namespace: 'imp-fe-perf-vite' }, (args) => ({
      contents: args.path === 'vite' ? 'export const defineConfig = (c) => c' : "export default () => ({ name: 'react-stub' })",
      loader: 'js',
    }))
  },
}
const configBundle = await build({
  entryPoints: [path.join(ROOT, 'vite.config.ts')],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node',
  plugins: [viteStubs],
  logLevel: 'silent',
})
const viteConfig = await import(toDataUrl(configBundle.outputFiles[0].text))

test('vite.config: vendor chunks for React, the icons, the charts and the CodeMirror stack', () => {
  const { vendorChunk, default: config } = viteConfig
  assert.equal(config.build.rollupOptions.output.manualChunks, vendorChunk)
  const cases = {
    'C:/repo/frontend/node_modules/react/index.js': 'vendor-react',
    '\0C:/repo/frontend/node_modules/react/index.js?commonjs-module': 'vendor-react',
    'C:\\repo\\frontend\\node_modules\\react-dom\\cjs\\react-dom.production.min.js': 'vendor-react',
    '/repo/frontend/node_modules/scheduler/cjs/scheduler.production.min.js': 'vendor-react',
    '/repo/frontend/node_modules/lucide-react/dist/esm/icons/brain.js': 'vendor-icons',
    '/repo/frontend/node_modules/lightweight-charts/dist/lightweight-charts.production.mjs': 'vendor-charts',
    '/repo/frontend/node_modules/fancy-canvas/size.mjs': 'vendor-charts',
    '/repo/frontend/node_modules/@codemirror/view/dist/index.js': 'vendor-codemirror',
    '/repo/frontend/node_modules/@lezer/python/dist/index.js': 'vendor-codemirror',
    '/repo/frontend/node_modules/@uiw/react-codemirror/esm/index.js': 'vendor-codemirror',
    '/repo/frontend/node_modules/@marijn/find-cluster-break/src/index.js': 'vendor-codemirror',
    '/repo/frontend/node_modules/style-mod/src/style-mod.js': 'vendor-codemirror',
    '/repo/frontend/node_modules/w3c-keyname/index.js': 'vendor-codemirror',
    '/repo/frontend/node_modules/crelt/index.js': 'vendor-codemirror',
  }
  for (const [id, chunk] of Object.entries(cases)) assert.equal(vendorChunk(id), chunk, id)
  for (const id of [
    '/repo/frontend/node_modules/zustand/esm/index.mjs',
    '/repo/frontend/node_modules/use-sync-external-store/shim/with-selector.js',
    '/repo/frontend/node_modules/react-router/dist/index.js', // only starts with "react"
    '/repo/frontend/src/pages/StudioPage.tsx',
    '/repo/frontend/src/components/studio/CodeEditor.tsx',
    '\0vite/preload-helper.js',
  ]) {
    assert.equal(vendorChunk(id), undefined, id)
  }
})

test('vite.config: honest chunk size warning limit; output folder and dev proxy unchanged', () => {
  const { build: b, server } = viteConfig.default
  assert.ok(b.chunkSizeWarningLimit > 0 && b.chunkSizeWarningLimit <= 500, `limit ${b.chunkSizeWarningLimit} kB (it was 3000)`)
  assert.equal(b.outDir, 'dist')
  assert.equal(server.port, 5173)
  assert.deepEqual(Object.keys(server.proxy), ['/api', '/ws'])
})

// ----------------------------------------------------------------------------- App.tsx: lazy sections

// The pages and the heavy shell parts (semáforo, symbol search, terminal) are stubs, so these tests only exercise
// App's own code; App.tsx additionally exports its internals to the tests only.
const STUB_NS = 'imp-fe-perf-stub'
const appStubs = {
  name: 'imp-fe-perf-app-stubs',
  setup(b) {
    b.onResolve({ filter: /^\.\/(?:pages\/\w+|components\/macro\/MarketPulse|components\/SymbolSearch|components\/terminal\/Terminal)$/ }, (args) =>
      posix(args.importer).endsWith('/src/App.tsx') ? { path: args.path, namespace: STUB_NS } : undefined,
    )
    b.onLoad({ filter: /.*/, namespace: STUB_NS }, (args) => {
      const name = JSON.stringify(args.path.split('/').pop())
      return {
        resolveDir: SRC,
        loader: 'js',
        contents: `import { createElement as h } from 'react'
${args.path.startsWith('./pages/') ? `globalThis.__impPagesLoaded.push(${name})` : ''}
export default function Stub() { return h('div', { 'data-stub': ${name} }) }`,
      }
    })
    b.onLoad({ filter: /[\\/]src[\\/]App\.tsx$/ }, (args) => ({
      contents: `${readFileSync(args.path, 'utf8')}
export { PAGES as __PAGES, PageBoundary as __PageBoundary, PageLoadError as __PageLoadError, PageLoading as __PageLoading,
  PageView as __PageView, lazyPage as __lazyPage, prefetchPage as __prefetchPage, LOADING_DELAY_MS as __LOADING_DELAY_MS,
  prefetchOnIntent as __prefetchOnIntent }
`,
      loader: 'tsx',
    }))
  },
}
const appBundle = await build({
  stdin: {
    contents: `export * from './App'
export { default as App } from './App'
export { useApp } from './store'
export { Suspense, createElement } from 'react'
export { renderToReadableStream, renderToStaticMarkup } from 'react-dom/server.browser'`,
    resolveDir: SRC,
    loader: 'tsx',
    sourcefile: 'imp-fe-perf-entry.tsx',
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  jsx: 'automatic',
  loader: { '.css': 'empty' },
  define: { 'import.meta.env.VITE_API_BASE': 'undefined', 'process.env.NODE_ENV': '"production"' },
  plugins: [appStubs],
  logLevel: 'silent',
})
const M = await import(toDataUrl(appBundle.outputFiles[0].text))
const h = M.createElement

/** Streams `element` like a server render, waiting for every lazy component; errors inside Suspense are collected.
 *  React's Suspense boundary markers (<!--$-->, <!--/$-->, <!--$!-->) are removed from the HTML. */
async function ssr(element) {
  const errors = []
  const stream = await M.renderToReadableStream(element, { onError: (e) => void errors.push(e) })
  await stream.allReady
  const html = (await new Response(stream).text()).replace(/<!--\/?\$[!?]?-->/g, '')
  return { html, errors }
}

/** Every element of a rendered tree (to click the boundary's buttons without a DOM). */
function elements(node, out = []) {
  if (Array.isArray(node)) node.forEach((n) => elements(n, out))
  else if (node && typeof node === 'object' && 'props' in node) {
    out.push(node)
    elements(node.props.children, out)
  }
  return out
}
const button = (tree, label) => elements(tree).find((el) => el.props.children === label)

const ALL_PAGES = {
  chart: 'ChartPage',
  markets: 'MarketsPage',
  scanner: 'ScannerPage',
  sim: 'SimulationPage',
  paper: 'PaperPage',
  studio: 'StudioPage',
  brain: 'BrainPage',
  settings: 'SettingsPage',
}

test('the section in the URL starts downloading as soon as the app loads, before any render', async () => {
  await flush()
  assert.deepEqual(globalThis.__impPagesLoaded, ['StudioPage'], 'only #/studio, nothing else')
})

test('the shell renders the section in the URL through the page boundary and Suspense', async () => {
  const { html, errors } = await ssr(h(M.App))
  assert.deepEqual(errors, [])
  assert.match(html, /<div class="page-area"><div data-stub="StudioPage"><\/div><\/div>/)
  assert.match(html, /data-stub="Terminal"/, 'the terminal dock is open by default')
  for (const label of ['Gráfico', 'Mercados', 'Escáner', 'Simulación', 'Cuenta demo', 'PyChart', 'Aprendizaje', 'Ajustes']) {
    assert.ok(html.includes(`<span>${label}</span>`), `menu item ${label}`)
  }
})

test('every menu section has its own lazy page; prefetching twice downloads each one once', async () => {
  assert.deepEqual(Object.keys(M.__PAGES).sort(), Object.keys(ALL_PAGES).sort())
  for (const page of Object.keys(ALL_PAGES)) {
    M.__prefetchPage(page)
    M.__prefetchPage(page)
  }
  await flush()
  assert.deepEqual([...globalThis.__impPagesLoaded].sort(), Object.values(ALL_PAGES).sort())
  for (const [page, file] of Object.entries(ALL_PAGES)) {
    const { html, errors } = await ssr(h(M.Suspense, { fallback: null }, h(M.__PageView, { page })))
    assert.deepEqual(errors, [])
    assert.equal(html, `<div data-stub="${file}"></div>`, `${page} renders ${file}`)
  }
})

test('lazyPage: the visit and every prefetch share one download', async () => {
  let calls = 0
  const Loaded = () => h('p', null, 'cargada')
  const page = M.__lazyPage('chart', () => {
    calls++
    return Promise.resolve({ default: Loaded })
  })
  const a = page.preload()
  assert.equal(page.preload(), a, 'same promise while downloading')
  const { html, errors } = await ssr(h(M.Suspense, { fallback: null }, h(page.Component)))
  assert.deepEqual(errors, [])
  assert.equal(html, '<p>cargada</p>')
  assert.equal((await page.preload()).default, Loaded)
  assert.equal(calls, 1)
})

test('lazyPage: a failed download reaches React as a PageLoadError with the browser message', async () => {
  const cause = 'Failed to fetch dynamically imported module: http://127.0.0.1:8420/assets/StudioPage-old.js'
  let calls = 0
  const page = M.__lazyPage('studio', () => {
    calls++
    return Promise.reject(new TypeError(cause))
  })
  // In the browser the page boundary catches it (next tests); a server render reports it and keeps the fallback.
  const { html, errors } = await ssr(h(M.Suspense, { fallback: h('i', null, 'cargando') }, h(page.Component)))
  assert.equal(calls, 1)
  assert.equal(errors.length, 1)
  const [err] = errors
  assert.ok(err instanceof M.__PageLoadError)
  assert.equal(err.name, 'PageLoadError')
  assert.equal(err.page, 'studio')
  assert.equal(err.message, cause)
  assert.match(html, /cargando/)
})

// The app keeps no failure of its own: the next attempt calls import() again. Chromium, however, keeps a failed module
// download for the life of the page (no new request), which is why the menu prefetch waits for a live connection.
test('lazyPage: the app keeps no failure itself, the next attempt calls import() again', async () => {
  let calls = 0
  let fail = true
  const page = M.__lazyPage('paper', () => {
    calls++
    return fail ? Promise.reject(new Error('sin conexión')) : Promise.resolve({ default: () => h('b', null, 'cuenta') })
  })
  await assert.rejects(page.preload(), (e) => e instanceof M.__PageLoadError && e.message === 'sin conexión')
  fail = false
  const { html, errors } = await ssr(h(M.Suspense, { fallback: null }, h(page.Component)))
  assert.deepEqual(errors, [])
  assert.equal(html, '<b>cuenta</b>')
  assert.equal(calls, 2)
})

test('menu prefetch (hover / focus) only while the server is reachable; the click always loads', () => {
  // Verified in Chromium: a prefetch that fails while the server is down leaves that section failing instantly, with no
  // new request, after the server is back (until a reload). So hover / focus wait for the live connection.
  const target = M.__PAGES.markets
  const original = target.preload
  const calls = []
  target.preload = () => {
    calls.push(M.useApp.getState().wsStatus)
    return Promise.resolve({ default: () => null })
  }
  const status = M.useApp.getState().wsStatus
  try {
    for (const ws of ['closed', 'connecting']) {
      M.useApp.setState({ wsStatus: ws })
      M.__prefetchOnIntent('markets')
    }
    assert.deepEqual(calls, [], 'no prefetch without a live connection')
    M.useApp.setState({ wsStatus: 'open' })
    Object.defineProperty(globalThis.navigator, 'onLine', { value: false, configurable: true })
    M.__prefetchOnIntent('markets')
    assert.deepEqual(calls, [], 'no prefetch while the browser is offline')
    Object.defineProperty(globalThis.navigator, 'onLine', { value: true, configurable: true })
    M.__prefetchOnIntent('markets')
    assert.deepEqual(calls, ['open'])
  } finally {
    delete globalThis.navigator.onLine
    target.preload = original
    M.useApp.setState({ wsStatus: status })
  }
  // Wiring: the menu uses the guarded prefetch; the unguarded one only starts the section in the URL at startup.
  const app = readFileSync(path.join(SRC, 'App.tsx'), 'utf8')
  assert.match(app, /onPointerEnter=\{\(\) => prefetchOnIntent\(p\)\}/)
  assert.match(app, /onFocus=\{\(\) => prefetchOnIntent\(p\)\}/)
  assert.doesNotMatch(app, /=\{\(\) => prefetchPage\(/, 'no event handler uses the unguarded prefetch')
  assert.match(app, /^prefetchPage\(useApp\.getState\(\)\.page\)$/m)
})

// ----------------------------------------------------------------------------- measuring tool (no build is run here)

const MEASURE = path.join(ROOT, 'tests', 'imp-fe-perf.measure.mjs')
const runMeasure = (...args) => spawnSync(process.execPath, [MEASURE, ...args], { cwd: ROOT, encoding: 'utf8', timeout: 120000 })

test('measuring tool: refuses an output folder with other files (Vite would empty it) and dist, before building', () => {
  const scratch = mkdtempSync(path.join(tmpdir(), 'imp-fe-perf-'))
  try {
    // A mistyped path such as ../backend or backend/.tmp: real files that Vite's emptyOutDir would delete.
    const busy = path.join(scratch, 'busy')
    mkdirSync(path.join(busy, 'assets'), { recursive: true })
    writeFileSync(path.join(busy, 'keep.txt'), 'datos')
    const refused = runMeasure(busy)
    assert.equal(refused.status, 2, refused.stderr)
    assert.match(refused.stderr, /tiene otros archivos \(keep\.txt\) y Vite la vaciaría/)
    assert.ok(existsSync(path.join(busy, 'keep.txt')) && existsSync(path.join(busy, 'assets')), 'nothing was deleted')
    // The dist folder of the frontend being measured (a copy here, so the real dist is never at risk).
    const copy = path.join(scratch, 'frontend')
    mkdirSync(path.join(copy, 'dist'), { recursive: true })
    const dist = runMeasure(path.join(copy, 'dist'), `--root=${copy}`)
    assert.equal(dist.status, 2, dist.stderr)
    assert.match(dist.stderr, /no puede ser ni contener/)
    if (process.platform === 'win32') {
      // The same folder spelled with another case on a case-insensitive file system.
      const upper = runMeasure(path.join(copy, 'DIST'), `--root=${copy}`)
      assert.equal(upper.status, 2, upper.stderr)
      assert.match(upper.stderr, /no puede ser ni contener/)
    }
  } finally {
    rmSync(scratch, { recursive: true, force: true })
  }
})

test('page boundary: a download error explains itself and offers a reload (an in-page retry cannot work)', () => {
  // Chromium keeps a failed module download for the life of the page and React.lazy keeps the rejection too: the
  // same file is never requested again, so "Reintentar" would be a button that never works.
  const cause = new TypeError('Failed to fetch dynamically imported module: /assets/StudioPage-old.js')
  const boundary = new M.__PageBoundary({ name: 'studio', children: 'sección' })
  boundary.state = M.__PageBoundary.getDerivedStateFromError(new M.__PageLoadError('studio', cause))
  const tree = boundary.render()
  const html = M.renderToStaticMarkup(tree)
  assert.match(html, /<strong>No se pudo descargar esta sección\.<\/strong>/)
  assert.match(
    html,
    /Puede que se haya perdido la conexión con el servidor o que la aplicación se haya actualizado mientras esta pestaña estaba abierta\. Recarga la página para intentarlo de nuevo\./,
  )
  assert.match(html, /Failed to fetch dynamically imported module: \/assets\/StudioPage-old\.js/)
  assert.doesNotMatch(html, /tuvo un error|Reintentar/)
  button(tree, 'Recargar página').props.onClick()
  assert.equal(reloads.length, 1)
})

test('page boundary: a render error keeps its message and Reintentar shows the section again', () => {
  const boundary = new M.__PageBoundary({ name: 'terminal', children: 'contenido' })
  assert.equal(boundary.render(), 'contenido', 'no error: the children')
  const states = []
  boundary.setState = (s) => void states.push(s)
  boundary.state = M.__PageBoundary.getDerivedStateFromError(new Error('x is not a function'))
  const tree = boundary.render()
  const html = M.renderToStaticMarkup(tree)
  assert.match(html, /<strong>Esta sección tuvo un error y no se pudo mostrar\.<\/strong>/)
  assert.match(html, /x is not a function/)
  assert.doesNotMatch(html, /Recargar página|descargar/)
  button(tree, 'Reintentar').props.onClick()
  assert.deepEqual(states, [{ error: null }])
  assert.equal(reloads.length, 1, 'no reload for a render error')
})

test('loading state: nothing for the first 150 ms (no flash on a fast server), then "Cargando sección…"', () => {
  assert.equal(M.__LOADING_DELAY_MS, 150)
  assert.equal(M.renderToStaticMarkup(h(M.__PageLoading)), '<div class="page" aria-busy="true"></div>')
  const app = readFileSync(path.join(SRC, 'App.tsx'), 'utf8')
  assert.match(app, /visible && <Loading label="Cargando sección…" \/>/)
})
