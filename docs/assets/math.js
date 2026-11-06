/** Render Zine's live-preview TeX using the same local KaTeX renderer as the release build. */
function renderModelMath() {
  const expressions = document.querySelectorAll('.prose script[type^="math/tex"]')
  if (!expressions.length) return
  if (!globalThis.katex) throw new Error('The local KaTeX asset did not load; cannot render the model equations')
  for (const expression of expressions) {
    const tex = expression.textContent
    console.assert(tex.trim(), 'The model contains an empty TeX expression')
    try {
      const container = document.createElement('span')
      container.innerHTML = globalThis.katex.renderToString(tex, {
        displayMode: expression.type.includes('mode=display'), output: 'mathml',
        throwOnError: true, trust: false, strict: 'error',
      })
      console.assert(container.querySelector('math'), `KaTeX produced no mathematics for ${tex}`)
      expression.replaceWith(...container.childNodes)
    } catch (error) {
      const fallback = document.createElement('code')
      fallback.textContent = tex
      expression.replaceWith(fallback)
      console.error('Could not render model equation', error)
    }
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderModelMath, { once: true })
else renderModelMath()
