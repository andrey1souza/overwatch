/** Render Zine's generated TeX expressions as native, accessible MathML before publication. */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync, writeFileSync } from 'node:fs'

const require = createRequire(import.meta.url)
const katex = require(process.env.KATEX_MODULE || 'katex')
const target = new URL('../docs/public/model/index.html', import.meta.url)
const html = readFileSync(target, 'utf8')
let count = 0
const rendered = html.replace(/<script type="math\/tex(?:; mode=display)?">([\s\S]*?)<\/script>/g, (script, tex) => {
  assert(tex.trim(), 'The model guide contains an empty mathematical expression')
  const math = katex.renderToString(tex, { displayMode: true, output: 'htmlAndMathml', throwOnError: true, trust: false, strict: 'error' })
  assert(math.includes('<math'), `KaTeX produced no mathematical markup for ${tex}`)
  count++
  return math
})
assert(count > 0, `The model guide contains ${count} generated TeX expressions; regenerate and build the docs first`)
assert(!rendered.includes('type="math/tex'), 'Some model expressions were left unrendered')
writeFileSync(target, rendered)
process.stdout.write(`${count} model equations rendered as MathML\n`)
