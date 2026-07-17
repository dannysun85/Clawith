import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'


describe('application shell dependencies', () => {
  it('does not require cross-origin scripts or stylesheets', () => {
    const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8')

    expect(html).not.toMatch(/<(?:link|script)\b[^>]*(?:href|src)=["']https?:\/\//i)
  })
})
