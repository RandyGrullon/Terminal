// Runs the economic-calendar tests (tests/imp-calendar.*.test.ts): each file is bundled with esbuild (types stripped,
// no type-check: `tsc --noEmit` covers src/) into a temporary folder and run with the Node test runner. Pure modules
// only (no DOM, no network). From frontend/:  node tests/imp-calendar.run.mjs
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const files = readdirSync(here)
  .filter((f) => /^imp-calendar\..+\.test\.ts$/.test(f))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'imp-calendar-tests-'))
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
