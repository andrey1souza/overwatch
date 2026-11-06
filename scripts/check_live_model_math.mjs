/** Exercise the exact unprocessed HTML Zine serves locally, using only its bundled browser assets. */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'

const require = createRequire(import.meta.url)
const { JSDOM, VirtualConsole } = require(process.env.MODEL_DOM_MODULE || 'jsdom')
const errors = []
const console = new VirtualConsole()
console.on('error', error => errors.push(error))
console.on('jsdomError', error => errors.push(error))
const preview = process.env.MODEL_PREVIEW_URL
const html = preview ? await (await fetch(preview)).text() : readFileSync(new URL('../docs/public/model/index.html', import.meta.url), 'utf8')
const dom = new JSDOM(html, { runScripts: 'dangerously', virtualConsole: console })
const { window } = dom
const expressions = [...window.document.querySelectorAll('script[type^="math/tex"]')].map(element => element.textContent.trim())
assert(expressions.length > 0, `Expected raw preview expressions before testing the browser path, found ${expressions.length}`)
for (const asset of ['vendor/katex.min.js', 'math.js']) {
  assert(html.includes(`/${asset}"`), `Zine did not include the browser asset ${asset}`)
  const script = window.document.createElement('script')
  const source = [...window.document.querySelectorAll('script[src]')].find(element => element.src.endsWith(`/${asset}`)).getAttribute('src')
  script.textContent = preview ? await (await fetch(new URL(source, preview))).text() : readFileSync(new URL(`../docs/public/${asset}`, import.meta.url), 'utf8')
  window.document.head.append(script)
}
window.document.dispatchEvent(new window.Event('DOMContentLoaded'))
const rendered = [...window.document.querySelectorAll('math annotation[encoding="application/x-tex"]')].map(element => element.textContent.trim())
assert.deepEqual(rendered, expressions, 'Local preview must render exactly the same TeX expressions as the release build')
assert.equal(window.document.querySelectorAll('script[type^="math/tex"]').length, 0, 'Preview left invisible TeX expressions behind')
assert.deepEqual(errors, [], 'Local preview logged rendering errors')
window.document.dispatchEvent(new window.Event('DOMContentLoaded'))
assert.equal(window.document.querySelectorAll('math').length, expressions.length, 'Repeated page events duplicated equations')
dom.window.close()
process.stdout.write(`${expressions.length} equations rendered from raw Zine HTML using bundled browser assets\n`)
