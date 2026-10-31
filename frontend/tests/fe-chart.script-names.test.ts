// Regression (studio-permanent-dirty): the backend stores script names with `" ".join(name.split())`, but the Studio
// dropped the draft after saving only when `draft.name.trim() === saved.name`, so a name with repeated spaces stayed
// "Cambios sin guardar" forever. Names are now compared with the backend's normalization.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { cleanScriptName, sameScriptName } from '../src/components/studio/scriptNames'

const ch = (cp: number) => String.fromCodePoint(cp)

test('a name typed with repeated spaces matches what the server saved', () => {
  assert.equal(cleanScriptName('Mi  script'), 'Mi script')
  assert.ok(sameScriptName('Mi  script', 'Mi script'))
  assert.notEqual('Mi  script'.trim(), 'Mi script') // the old comparison
})

test('same whitespace rules as Python str.split()', () => {
  // expected values computed with the backend's " ".join(name.split())
  assert.equal(cleanScriptName(`  Mi\tscript ${'\n'}`), 'Mi script')
  assert.equal(cleanScriptName(`Mi${ch(0xa0)}${ch(0xa0)}script`), 'Mi script')
  assert.equal(cleanScriptName(`A${ch(0x2028)}B${ch(0x3000)}C`), 'A B C')
  assert.equal(cleanScriptName(`A${ch(0x1c)}B${ch(0x85)}C`), 'A B C')
  assert.equal(cleanScriptName(`A${ch(0x200b)}B`), `A${ch(0x200b)}B`) // zero-width space: not whitespace in Python
  assert.equal(cleanScriptName(`A${ch(0xfeff)}B`), `A${ch(0xfeff)}B`) // nor the BOM
  assert.equal(cleanScriptName('   '), '')
  assert.ok(!sameScriptName('Mi script 2', 'Mi script'))
})
