// Runs the fe-chart regression tests (tests/fe-chart.*.test.ts): each file is bundled with esbuild (types stripped,
// no type-check: `tsc --noEmit` covers src/) into a temporary folder and run with the Node test runner.
// No network, no browser. From frontend/:  node tests/fe-chart.run.mjs
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const files = readdirSync(here)
  .filter((f) => /^fe-chart\..+\.test\.ts$/.test(f))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'fe-chart-tests-'))
try {
  await build({
    entryPoints: files.map((f) => join(here, f)),
    outdir: out,
    outExtension: { '.js': '.mjs' },
    bundle: true,
    platform: 'node',
    format: 'esm',
    logLevel: 'warning',
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
