import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import en from '../i18n/en'
import { MatcherDebugView } from './MatcherDebugPanel'
import { AMBIGUOUS, IDENTIFIED, MATCHES, MISSING_PARTS, NOT_IN_CATALOG } from './MatcherDebugPanel.fixtures'

const t = key => key.split('.').reduce((value, part) => value?.[part], en) ?? key

const render = (matcher, props = {}) => renderToStaticMarkup(
  <MatcherDebugView matcher={matcher} sourceUrl="blob:photo" planeUrl="blob:plane"
    matches={MATCHES} onSelect={() => {}} onCompare={() => {}} t={t} {...props} />,
)

const flagsIn = html => [...html.matchAll(/data-flag="([a-z_]+)"/g)].map(match => match[1])

describe('MatcherDebugView', () => {
  it('draws one timing segment per stage, never total, plus "other" for overhead', () => {
    const timingsIn = html => [...html.matchAll(/data-timing="([a-z_]+)"/g)].map(match => match[1])
    const exact = render(IDENTIFIED)
    expect(timingsIn(exact)).toEqual(['frame_gate', 'localize', 'rectify', 'thumb_head', 'retrieve', 'ocr', 'fuse', 'twin'])
    const overhead = render({ ...IDENTIFIED, timings_ms: { ...IDENTIFIED.timings_ms, total: 600 } })
    expect(timingsIn(overhead).at(-1)).toBe('__other')
    expect(timingsIn(overhead)).not.toContain('total')
    expect(overhead).toContain(`${en.matcher.otherTiming} <span class="font-mono">96</span>`)
  })

  it('renders an IDENTIFIED payload with the quad, plane, candidates and timings', () => {
    const html = render(IDENTIFIED)
    expect(html).toContain('data-testid="matcher-debug-panel"')
    expect(html).toMatch(/<details[^>]* open=""/)
    expect(html).toContain('data-state="IDENTIFIED"')
    expect(html).toContain('text-green')
    // quad drawn in upload pixels against the declared image size
    expect(html).toContain('viewBox="0 0 1000 1333"')
    expect(html).toContain('points="120,80 880,95 870,1140 110,1120"')
    expect(html).toContain('src="blob:plane"')
    expect(html).toContain('043/165')
    expect(html).toContain('agree')
    expect(html).toContain('en 0.80 · de 0.20')
    expect(html).toContain('Oddish')
    expect(html).toContain('Myrapla')
    expect(html).toContain('0.91')
    expect(html).toContain('Δ 0.12')
    expect(html).toContain('504 ms')
    expect(html).toContain('retrieve')
    expect(flagsIn(html)).toEqual([])
    expect(html).toContain(en.matcher.noFlags)
    // add buttons go through the existing resolve flow
    expect(html.match(new RegExp(`aria-label="${en.scanner.addToCollection}"`, 'g'))).toHaveLength(2)
  })

  it('shows twin_ambiguous and only the truthy flags for AMBIGUOUS', () => {
    const html = render(AMBIGUOUS)
    expect(html).toContain('data-state="AMBIGUOUS"')
    expect(html).toContain('text-yellow')
    expect(flagsIn(html)).toEqual(['low_confidence', 'picker_only', 'twin_ambiguous'])
    expect(html).toContain('tie')
    expect(html).not.toContain(en.matcher.noFlags)
  })

  it('shows the hint for NOT_IN_CATALOG', () => {
    const html = render(NOT_IN_CATALOG)
    expect(html).toContain('data-state="NOT_IN_CATALOG"')
    expect(html).toContain('text-brand-red')
    expect(html).toContain('role="note"')
    expect(html).toContain('set may not be supported yet')
    expect(flagsIn(html)).toEqual(['via_fallback', 'unreferenced_guard'])
    expect(html).toContain('disagree')
  })

  it('tolerates missing artefacts and a partial payload', () => {
    const html = render(MISSING_PARTS, { sourceUrl: null, planeUrl: null, planeFailed: true, matches: [] })
    expect(html).toContain('data-state="TOO_BLURRY"')
    expect(html).not.toContain(en.matcher.detectedBoundary)
    expect(html).not.toContain('<polygon')
    expect(html).toContain(en.matcher.noSourceImage)
    expect(html).toContain(en.matcher.artifactUnavailable)
    expect(html).toContain(en.matcher.noCandidates)
    expect(html).toContain(en.matcher.noTimings)
  })

  it('draws the photo without an overlay when geometry is missing', () => {
    const html = render({ ...IDENTIFIED, geometry: null }, { planeUrl: null })
    expect(html).toContain('src="blob:photo"')
    expect(html).not.toContain('<polygon')
    expect(html).toContain(en.matcher.noGeometry)
    expect(html).toContain(en.matcher.noPlane)
  })

  it('starts collapsed when not opened by default and renders nothing without a payload', () => {
    expect(render(IDENTIFIED, { defaultOpen: false })).not.toMatch(/<details[^>]* open=""/)
    expect(render(null)).toBe('')
  })
})
