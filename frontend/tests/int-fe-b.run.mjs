// Runs the int-fe-b regression tests (tests/int-fe-b.*.test.ts) the way tests/fe-shell.run.mjs does: each file is
// bundled with esbuild (types stripped, no type-check: `tsc --noEmit` covers src/) into a temporary folder and run with
// the Node test runner against the fake browser of tests/int-fe-b.env.ts (tests/fe-shell.env.ts: window, storage,
// fetch, WebSocket, manual timers; plus navigator). No network, no browser.
// - VITE_API_BASE is the bare Railway domain, as in the fe-shell tests (cloud mode talks to https://<domain>).
// - *.market-data.test.ts runs src/components/chart/useMarketData.ts on tests/int-fe-b.react-shim.ts (the few hooks
//   it uses, no DOM) instead of React; the other files use the real React.
// - components/chart/IndicatorsDialog.tsx additionally exports its internal NumberParam to the tests only.
// From frontend/:  node tests/int-fe-b.run.mjs            (one file: node tests/int-fe-b.run.mjs ws)
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const API_BASE_AS_TYPED = 'nexus-production.up.railway.app/'

const here = dirname(fileURLToPath(import.meta.url))
const SHIM = join(here, 'int-fe-b.react-shim.ts')

const exposeNumberParam = {
  name: 'int-fe-b-expose-number-param',
  setup(b) {
    b.onLoad({ filter: /[\\/]components[\\/]chart[\\/]IndicatorsDialog\.tsx$/ }, async (args) => ({
      contents: `${await readFile(args.path, 'utf8')}\nexport { NumberParam as __NumberParam }\n`,
      loader: 'tsx',
    }))
  },
}

const reactShim = {
  name: 'int-fe-b-react-shim',
  setup(b) {
    b.onResolve({ filter: /^react$/ }, () => ({ path: SHIM }))
  },
}

const only = process.argv[2] // optional filter, e.g. "ws"
const files = readdirSync(here)
  .filter((f) => /^int-fe-b\..+\.test\.ts$/.test(f) && (!only || f.includes(only)))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'int-fe-b-tests-'))
try {
  for (const f of files) {
    await build({
      entryPoints: [join(here, f)],
      outfile: join(out, f.replace(/\.ts$/, '.mjs')),
      bundle: true,
      platform: 'node',
      format: 'esm',
      jsx: 'automatic',
      logLevel: 'warning',
      define: { 'import.meta.env': JSON.stringify({ VITE_API_BASE: API_BASE_AS_TYPED }) },
      // react-dom/server (CommonJS) requires Node built-ins: give the ESM bundles a real `require`.
      banner: { js: "import { createRequire as __ifbCreateRequire } from 'node:module'; const require = __ifbCreateRequire(import.meta.url);" },
      plugins: f.includes('market-data') ? [reactShim] : [exposeNumberParam],
    })
  }
  const bundles = readdirSync(out)
    .filter((f) => f.endsWith('.mjs'))
    .sort()
    .map((f) => join(out, f))
  const run = spawnSync(process.execPath, ['--test', ...bundles], { stdio: 'inherit' })
  process.exitCode = run.status ?? 1
} finally {
  rmSync(out, { recursive: true, force: true })
}
