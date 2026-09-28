// Representative external-matcher payloads (docs/POKESCANNER-PLAN.md §4.2,
// candidates stripped as the backend stores them) for component tests.

const base = {
  matcher: { name: 'pokescan', commit: '8f0ff3b', bundle: 'v12' },
  confident: false,
  hint: null,
  print_id: null,
  identity_key: null,
  language: { options: [], source: 'detected', scores: { en: 0.5, de: 0.5 }, unsupported: null },
  number: { reading: null, verdict: null },
  twin: { ambiguous: false, verdict: null, scores: null },
  flags: {
    low_confidence: false, picker_only: false, via_fallback: false, via_refine: false,
    slab_trim: false, classic_reprint: false, unreferenced_guard: false,
    card_back_score: 0.03, script: 'latin', orientation: 0,
  },
  geometry: { quad: [[120, 80], [880, 95], [870, 1140], [110, 1120]], source: 'primary', image_size: [1000, 1333] },
  timings_ms: { frame_gate: 3, localize: 90, rectify: 4, thumb_head: 20, retrieve: 310, ocr: 60, fuse: 2, twin: 15, total: 504 },
  trace_id: '2026-09-28T21-04-11Z_ab12cd',
}

export const IDENTIFIED = {
  ...base,
  state: 'IDENTIFIED',
  confident: true,
  print_id: 'en:sv03.5-043',
  identity_key: 'sv|sv03.5|043',
  language: { options: ['en:sv03.5-043', 'de:sv03.5-043'], source: 'detected', scores: { en: 0.8, de: 0.2 }, unsupported: null },
  number: { reading: '043/165', verdict: 'agree' },
}

export const AMBIGUOUS = {
  ...base,
  state: 'AMBIGUOUS',
  print_id: 'en:sv04-101',
  number: { reading: '101/182', verdict: 'unreadable' },
  twin: { ambiguous: true, verdict: 'tie', scores: { a: 0.51, b: 0.49 } },
  flags: { ...base.flags, low_confidence: true, picker_only: true },
}

export const NOT_IN_CATALOG = {
  ...base,
  state: 'NOT_IN_CATALOG',
  hint: 'set may not be supported yet',
  number: { reading: '012/064', verdict: 'disagree' },
  flags: { ...base.flags, via_fallback: true, unreferenced_guard: true },
}

// Older/partial payload: no geometry, no timings, no language block.
export const MISSING_PARTS = {
  matcher: { name: 'pokescan' },
  state: 'TOO_BLURRY',
  confident: false,
  hint: null,
  flags: null,
}

export const MATCHES = [
  {
    id: 'sv03.5-043_en', tcg_card_id: 'sv03.5-043', lang: 'en', set_id: 'sv03.5', number: '043',
    name: 'Oddish', rarity: 'Common', image: '/api/cards/recognize/matcher/ref/en:sv03.5-043',
    _score: 0.91, _margin: 0.12, _match_percent: 91,
  },
  {
    id: 'sv03.5-043_de', tcg_card_id: 'sv03.5-043', lang: 'de', set_id: 'sv03.5', number: '043',
    name: 'Myrapla', rarity: 'Common', image: null, _score: 0.79, _margin: 0.05, _match_percent: 79,
  },
]

export const EXTERNAL_STATUS = {
  configured: true,
  label: 'pokescan',
  health: { ok: true, bundle_version: 'v12', gallery_version: 'g7', commit: '8f0ff3b', uptime: 7200 },
  bundle: {
    langs: {
      en: {
        sv03: { name: 'Obsidian Flames', set_code: 'OBF', era: 'sv', prints: 230, gallery_prints: 228 },
        'sv03.5': { name: '151', set_code: 'MEW', era: 'sv', prints: 207, gallery_prints: 207 },
      },
      de: {
        sv03: { name: 'Obsidianflammen', set_code: 'OBF', era: 'sv', prints: 230, gallery_prints: 200 },
      },
      ja: {
        sv2a: { name: 'ポケモンカード151', set_code: 'SV2a', era: 'sv', prints: 210, gallery_prints: 210 },
      },
    },
    totals: { en: { sets: 2 }, de: { sets: 1 }, ja: { sets: 1 } },
    uncatalogued_scripts: ['thai'],
  },
  error: null,
}
