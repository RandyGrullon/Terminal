// Runs the data-rt tests (tests/imp-data-rt.*.test.ts): price freshness of the chart toolbar (pure rules + the badge)
// and the "Tiempo real" settings group. Each file is bundled with esbuild (types stripped, no type-check: `tsc
// --noEmit` covers src/) into a temporary folder and run with the Node test runner. Files that render the badge
// import tests/fe-shell.env.ts first (fake window, storage, timers: the API modules read them when evaluated).
// No network, no browser. From frontend/:  node tests/imp-data-rt.run.mjs
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const files = readdirSync(here)
  .filter((f) => /^imp-data-rt\..+\.test\.ts$/.test(f))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'imp-data-rt-tests-'))
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
    define: { 'import.meta.env': JSON.stringify({ VITE_API_BASE: '' }) },
    // react-dom/server (CommonJS) requires Node built-ins: give the ESM bundles a real `require`.
    banner: { js: "import { createRequire as __idrCreateRequire } from 'node:module'; const require = __idrCreateRequire(import.meta.url);" },
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
