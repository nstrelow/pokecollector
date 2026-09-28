import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import en from '../i18n/en'
import { EXTERNAL_STATUS } from './MatcherDebugPanel.fixtures'
import { ExternalMatcherView } from './ScannerSettingsCard'

const t = key => key.split('.').reduce((value, part) => value?.[part], en) ?? key
const render = (status, props = {}) => renderToStaticMarkup(<ExternalMatcherView status={status} t={t} {...props} />)

describe('ExternalMatcherView (Scanner settings)', () => {
  it('shows health, versions and the supported sets of the first language', () => {
    const html = render(EXTERNAL_STATUS)
    expect(html).toContain('data-testid="external-matcher-block"')
    expect(html).toContain('data-health="ok"')
    expect(html).toContain('8f0ff3b')
    expect(html).toContain('v12')
    expect(html).toContain('g7')
    expect(html).toContain('2.0 h')
    expect(html).toContain('Obsidian Flames')
    expect(html).toContain('MEW')
    expect(html).toContain('228/230')
    expect(html).not.toContain('Obsidianflammen')
    expect(html).toContain('2/2 ' + en.matcher.setsShown)
    expect(html).toMatch(/aria-selected="true"[^>]*>en · 2</)
    expect(html).toContain(en.matcher.unsyncedNote + ' (ja)')
    expect(html).toContain('thai')
  })

  it('filters sets by the search query and switches language', () => {
    const html = render(EXTERNAL_STATUS, { initialQuery: 'mew' })
    expect(html).toContain('151')
    expect(html).not.toContain('Obsidian Flames')
    expect(html).toContain('1/2 ' + en.matcher.setsShown)
    const ja = render(EXTERNAL_STATUS, { initialLang: 'ja' })
    expect(ja).toContain('ポケモンカード151')
  })

  it('shows a down matcher without a bundle', () => {
    const html = render({ configured: true, health: { ok: false }, bundle: null, error: 'connection refused' })
    expect(html).toContain('data-health="down"')
    expect(html).toContain('connection refused')
    expect(html).not.toContain('<table')
  })

  it('is hidden when the matcher is not configured or the endpoint 404s', () => {
    expect(render(null)).toBe('')
    expect(render({ configured: false, health: null, bundle: null })).toBe('')
  })
})
