// Runs the alerts regression tests (tests/imp-alerts.*.test.ts): each file is bundled with esbuild (types stripped,
// no type-check: `tsc --noEmit` covers src/) into a temporary folder and run with the Node test runner. Each file is
// its own bundle, so it gets fresh module state. No network, no browser: tests/imp-alerts.env.ts fakes window,
// localStorage, document and the Notification API, and the WebSocket client and the app store imported by
// components/macro/alerts.ts are replaced by tests/imp-alerts.fakes.ts.
// From frontend/:  node tests/imp-alerts.run.mjs
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const fakes = join(here, 'imp-alerts.fakes.ts')

const swapAppModules = {
  name: 'imp-alerts-fakes',
  setup(b) {
    // only the imports of components/macro/alerts.ts: '../../api/ws' and '../../store'
    b.onResolve({ filter: /^\.\.\/\.\.\/(api\/ws|store)$/ }, (args) =>
      /[\\/]components[\\/]macro[\\/]alerts\.ts$/.test(args.importer) ? { path: fakes } : undefined,
    )
  },
}

const files = readdirSync(here)
  .filter((f) => /^imp-alerts\..+\.test\.ts$/.test(f))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'imp-alerts-tests-'))
try {
  await build({
    entryPoints: files.map((f) => join(here, f)),
    outdir: out,
    outExtension: { '.js': '.mjs' },
    bundle: true,
    platform: 'node',
    format: 'esm',
    jsx: 'automatic',
    logLevel: 'warning',
    // the bundles run from a temporary folder: tell them where the repository is (backend defaults are read)
    define: { 'import.meta.env': JSON.stringify({}), __IMP_ALERTS_FRONTEND__: JSON.stringify(dirname(here)) },
    plugins: [swapAppModules],
  })
  const bundles = readdirSync(out)
    .filter((f) => f.endsWith('.mjs'))
    .sort()
    .map((f) => join(out, f))
  const run = spawnSync(process.execPath, ['--test', ...bundles], { stdio: 'inherit' })
  process.exitCode = run.status ?? 1
} finally {
  rmSync(out, { recursive: true, force: true })
}
