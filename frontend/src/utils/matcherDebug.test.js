import { describe, expect, it } from 'vitest'

import {
  activeFlags,
  defaultSessionLang,
  externalMatcherConfigured,
  filterSetRows,
  isExternalItem,
  matcherArtifacts,
  readSessionLang,
  scaleQuad,
  stateTone,
  supportedSetRows,
  timingSegments,
  validQuad,
  writeSessionLang,
} from './matcherDebug'

const memoryStorage = () => {
  const data = new Map()
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)) }
}

describe('matcher debug helpers', () => {
  it('maps every state to a tone and unknown ones to muted', () => {
    expect(stateTone('IDENTIFIED')).toBe('good')
    expect(stateTone('AMBIGUOUS')).toBe('warn')
    expect(stateTone('NOT_IN_CATALOG')).toBe('bad')
    expect(stateTone('CARD_BACK')).toBe('muted')
    expect(stateTone('WHATEVER')).toBe('muted')
  })

  it('lists only truthy flags and folds twin.ambiguous in', () => {
    expect(activeFlags({ flags: { low_confidence: true, via_refine: false, card_back_score: 0.9 }, twin: { ambiguous: true } }))
      .toEqual(['low_confidence', 'twin_ambiguous'])
    expect(activeFlags({})).toEqual([])
  })

  it('validates and scales the quad', () => {
    expect(validQuad({ quad: [[0, 0], [1, 0], [1, 1]] })).toBeNull()
    expect(validQuad({ quad: [[0, 0], [1, 'x'], [1, 1], [0, 1]] })).toBeNull()
    const quad = validQuad({ quad: [[100, 50], [900, 50], [900, 1250], [100, 1250]] })
    expect(scaleQuad(quad, [1000, 1400], [250, 350])).toEqual([[25, 12.5], [225, 12.5], [225, 312.5], [25, 312.5]])
  })

  it('builds timing segments against the declared total', () => {
    const { segments, total } = timingSegments({ localize: 100, retrieve: 300, total: 500 })
    expect(total).toBe(500)
    expect(segments.map(s => [s.key, s.percent])).toEqual([['localize', 20], ['retrieve', 60], ['__other', 20]])
    expect(timingSegments({ a: 1, b: 3 }).total).toBe(4)
    expect(timingSegments(null).segments).toEqual([])
  })

  it('never draws total and shows the unaccounted remainder as "other"', () => {
    const fixture = { frame_gate: 3, localize: 90, rectify: 4, thumb_head: 20, retrieve: 310, ocr: 60, fuse: 2, twin: 15, total: 504 }
    const exact = timingSegments(fixture)
    expect(exact.segments.map(s => s.key)).toEqual(['frame_gate', 'localize', 'rectify', 'thumb_head', 'retrieve', 'ocr', 'fuse', 'twin'])
    expect(exact.sum).toBe(504)
    expect(exact.segments.reduce((acc, s) => acc + s.percent, 0)).toBeCloseTo(100)

    const withOverhead = timingSegments({ ...fixture, total: 600 })
    const last = withOverhead.segments.at(-1)
    expect(last).toMatchObject({ key: '__other', ms: 96, remainder: true })
    expect(withOverhead.segments.some(s => s.key === 'total')).toBe(false)
    expect(withOverhead.segments.reduce((acc, s) => acc + s.percent, 0)).toBeCloseTo(100)
  })

  it('recognises external items and both artefact spellings', () => {
    expect(isExternalItem({ recognized: { _source: 'external' } })).toBe(true)
    expect(isExternalItem({ _source: 'external' })).toBe(true)
    expect(isExternalItem({ recognized: { name: 'x' } })).toBe(false)
    expect(matcherArtifacts({ artefacts: { plane: '/p' } })).toEqual({ source: null, plane: '/p', overlay: null })
    expect(matcherArtifacts({ artifacts: { overlay: '/o' } }).overlay).toBe('/o')
  })

  it('builds and filters supported-set rows', () => {
    const bundle = { langs: { en: { sv03: { name: 'Obsidian Flames', set_code: 'OBF', prints: 230, gallery_prints: 228 } } } }
    const rows = supportedSetRows(bundle, 'en')
    expect(rows).toEqual([{ set_id: 'sv03', name: 'Obsidian Flames', set_code: 'OBF', era: '', prints: 230, gallery_prints: 228 }])
    expect(filterSetRows(rows, 'obf')).toHaveLength(1)
    expect(filterSetRows(rows, 'paldea')).toHaveLength(0)
    expect(supportedSetRows(bundle, 'de')).toEqual([])
  })

  it('persists the session language and defaults from settings', () => {
    const storage = memoryStorage()
    expect(readSessionLang(storage, 'en')).toBe('en')
    writeSessionLang(storage, 'de')
    expect(readSessionLang(storage, 'en')).toBe('de')
    writeSessionLang(storage, 'ja')
    expect(readSessionLang(storage, 'en')).toBe('de')
    expect(readSessionLang({ getItem: () => { throw new Error('blocked') } }, 'en')).toBe('en')
    expect(defaultSessionLang({ language: 'de' })).toBe('de')
    expect(defaultSessionLang({ language: 'fr' })).toBe('en')
    expect(defaultSessionLang(undefined)).toBe('en')
  })

  it('treats null and configured:false as not configured', () => {
    expect(externalMatcherConfigured(null)).toBe(false)
    expect(externalMatcherConfigured({ configured: false })).toBe(false)
    expect(externalMatcherConfigured({ health: { ok: true } })).toBe(true)
  })
})
