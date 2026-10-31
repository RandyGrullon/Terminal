// Script names as the backend stores them (app/scripting/service.py `_clean_name`: `" ".join(name.split())`).
// Comparing a local draft name with the saved one must use the same normalization, or a name typed with repeated
// spaces never matches what the server returns and the script looks "unsaved" forever.

/** Code points that Python's `str.split()` treats as whitespace (`str.isspace`): ASCII whitespace, the 0x1c-0x1f
 *  separators, NEL, no-break space and the Unicode spaces / line separators. */
const PY_WHITESPACE = new Set<number>([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
])

/** "  Mi   script " -> "Mi script" (same result as the backend). */
export function cleanScriptName(name: string): string {
  const words: string[] = []
  let word = ''
  for (const ch of String(name ?? '')) {
    if (PY_WHITESPACE.has(ch.codePointAt(0) ?? 0)) {
      if (word) words.push(word)
      word = ''
    } else {
      word += ch
    }
  }
  if (word) words.push(word)
  return words.join(' ')
}

/** True when a local (draft) name is stored by the backend as `saved`. */
export function sameScriptName(local: string, saved: string): boolean {
  return cleanScriptName(local) === saved
}
