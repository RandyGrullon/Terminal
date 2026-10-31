// Runs every frontend test suite (`npm test`): each tests/*.run.mjs runner (it bundles its own *.test.ts files with
// esbuild) and every tests/*.test.mjs file with node --test. New suites are picked up by their file name.
import { spawnSync } from 'node:child_process'
import { readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = path.dirname(fileURLToPath(import.meta.url))
const files = readdirSync(dir).sort()
const failed = []

for (const file of files.filter((f) => f.endsWith('.run.mjs'))) {
  const result = spawnSync(process.execPath, [path.join(dir, file)], { stdio: 'inherit' })
  if (result.status !== 0) failed.push(file)
}
const direct = files.filter((f) => f.endsWith('.test.mjs')).map((f) => path.join(dir, f))
if (direct.length) {
  const result = spawnSync(process.execPath, ['--test', ...direct], { stdio: 'inherit' })
  if (result.status !== 0) failed.push('node --test *.test.mjs')
}

if (failed.length) {
  console.error(`\nSuites con fallos: ${failed.join(', ')}`)
  process.exit(1)
}
console.log('\nTodas las suites de la interfaz pasaron.')
