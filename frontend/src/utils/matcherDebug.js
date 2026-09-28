// Pure helpers for the external matcher (pokescan) debug panel, the scanner
// settings block and the session-language toggle. Kept free of React so they
// can be unit-tested directly; see docs/POKESCANNER-PLAN.md §4.2 for the
// payload these read.

export const MATCHER_STATES = [
  'IDENTIFIED',
  'CONFIRM_LANGUAGE',
  'AMBIGUOUS',
  'POSSIBLY_UNSUPPORTED_SET',
  'NOT_IN_CATALOG',
  'NO_CARD',
  'CARD_BACK',
  'TOO_BLURRY',
]

// Badge colour per decision state: green = accepted, blue/yellow = needs a
// look, red = not in the catalogue, grey = nothing usable in the frame.
const STATE_TONES = {
  IDENTIFIED: 'good',
  CONFIRM_LANGUAGE: 'info',
  AMBIGUOUS: 'warn',
  POSSIBLY_UNSUPPORTED_SET: 'warn',
  NOT_IN_CATALOG: 'bad',
  NO_CARD: 'muted',
  CARD_BACK: 'muted',
  TOO_BLURRY: 'muted',
}

export const TONE_CLASSES = {
  good: 'border-green/40 bg-green/15 text-green',
  info: 'border-blue/40 bg-blue/15 text-blue',
  warn: 'border-yellow/40 bg-yellow/15 text-yellow',
  bad: 'border-brand-red/40 bg-brand-red/15 text-brand-red',
  muted: 'border-white/15 bg-white/5 text-text-secondary',
}

export function stateTone(state) {
  return STATE_TONES[state] || 'muted'
}

// Boolean flags shown as chips, in display order. `twin_ambiguous` is not in
// `flags` in the contract — it comes from `twin.ambiguous` — but reads as one.
export const FLAG_KEYS = [
  'low_confidence',
  'picker_only',
  'twin_ambiguous',
  'via_fallback',
  'via_refine',
  'slab_trim',
  'classic_reprint',
  'unreferenced_guard',
]

export function activeFlags(matcher) {
  const flags = matcher?.flags || {}
  return FLAG_KEYS.filter(key => (
    key === 'twin_ambiguous'
      ? Boolean(flags.twin_ambiguous || matcher?.twin?.ambiguous)
      : Boolean(flags[key])
  ))
}

// Returns [[x, y] × 4] or null. Anything malformed means "no overlay", never
// an exception — the panel has to survive older or partial payloads.
export function validQuad(geometry) {
  const quad = geometry?.quad
  if (!Array.isArray(quad) || quad.length !== 4) return null
  const points = quad.map(point => (
    Array.isArray(point) && point.length >= 2 ? [Number(point[0]), Number(point[1])] : null
  ))
  return points.every(point => point && Number.isFinite(point[0]) && Number.isFinite(point[1]))
    ? points
    : null
}

export function validImageSize(geometry) {
  const size = geometry?.image_size
  if (!Array.isArray(size) || size.length < 2) return null
  const [width, height] = size.map(Number)
  return width > 0 && height > 0 ? [width, height] : null
}

// The quad is in uploaded-image pixels. The overlay SVG uses the reference
// size as its viewBox, so the browser does the scaling; this is the same
// mapping for anything that needs explicit rendered coordinates.
export function scaleQuad(quad, fromSize, toSize) {
  if (!quad || !fromSize || !toSize) return null
  const sx = toSize[0] / fromSize[0]
  const sy = toSize[1] / fromSize[1]
  return quad.map(([x, y]) => [x * sx, y * sy])
}

export function quadPoints(quad) {
  return quad ? quad.map(([x, y]) => `${round1(x)},${round1(y)}`).join(' ') : ''
}

function round1(value) {
  return Math.round(value * 10) / 10
}

// Stacked timings bar: every numeric stage except the total, in payload order.
// The total is the payload's own when present (it includes overhead the
// stages do not), else the sum.
export function timingSegments(timings) {
  if (!timings || typeof timings !== 'object') return { segments: [], total: 0 }
  const segments = Object.entries(timings)
    .filter(([key, value]) => key !== 'total' && Number.isFinite(Number(value)) && Number(value) >= 0)
    .map(([key, value]) => ({ key, ms: Number(value) }))
  const sum = segments.reduce((acc, segment) => acc + segment.ms, 0)
  const declared = Number(timings.total)
  const total = Number.isFinite(declared) && declared > 0 ? declared : sum
  const scale = Math.max(total, sum) || 1
  return {
    total,
    segments: segments.map(segment => ({ ...segment, percent: (segment.ms / scale) * 100 })),
  }
}

export const TIMING_COLORS = [
  '#3b82f6', '#22c55e', '#eab308', '#f97316', '#e3000b',
  '#a855f7', '#14b8a6', '#ec4899', '#94a3b8', '#84cc16',
]

export function isExternalItem(item) {
  return (item?._source || item?.recognized?._source) === 'external'
}

export function identityDecision(item) {
  return item?._identity_decision ?? item?.recognized?._identity_decision ?? null
}

// The backend spells it "artefacts"; the plan says "artifacts". Accept both.
export function matcherArtifacts(response) {
  const raw = response?.artifacts || response?.artefacts || {}
  return {
    source: raw.source || null,
    plane: raw.plane || null,
    overlay: raw.overlay || null,
  }
}

export function formatScore(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '–'
}

export function scorePercent(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return 0
  return Math.max(0, Math.min(100, number * 100))
}

// ─── Supported sets (settings block) ────────────────────────────────────────

export function supportedSetRows(bundle, lang) {
  const block = bundle?.langs?.[lang]
  if (!block || typeof block !== 'object') return []
  const sets = block.sets && typeof block.sets === 'object' ? block.sets : block
  return Object.entries(sets)
    .filter(([, info]) => info && typeof info === 'object')
    .map(([setId, info]) => ({
      set_id: setId,
      name: info.name || setId,
      set_code: info.set_code || '',
      era: info.era || '',
      prints: Number(info.prints) || 0,
      gallery_prints: Number(info.gallery_prints) || 0,
    }))
}

export function filterSetRows(rows, query) {
  const needle = (query || '').trim().toLowerCase()
  if (!needle) return rows
  return rows.filter(row => [row.name, row.set_code, row.set_id, row.era]
    .some(value => String(value || '').toLowerCase().includes(needle)))
}

export function bundleLanguages(bundle) {
  return Object.keys(bundle?.langs || {})
}

// Languages pokecollector syncs. Anything else the matcher returns (ja,
// zh-tw, …) is still shown but falls back to a live TCGdex lookup on add.
export const SYNCED_MATCHER_LANGUAGES = ['en', 'de']

// ─── Session language (scanner modal) ──────────────────────────────────────

export const SESSION_LANGS = ['en', 'de']
export const SESSION_LANG_STORAGE_KEY = 'scanner_session_lang'

export function defaultSessionLang(settings) {
  const candidates = [settings?.scanner_session_lang, settings?.language]
  return candidates.find(lang => SESSION_LANGS.includes(lang)) || 'en'
}

export function readSessionLang(storage, fallback = 'en') {
  try {
    const stored = storage?.getItem(SESSION_LANG_STORAGE_KEY)
    return SESSION_LANGS.includes(stored) ? stored : fallback
  } catch {
    return fallback
  }
}

export function writeSessionLang(storage, lang) {
  if (!SESSION_LANGS.includes(lang)) return
  try {
    storage?.setItem(SESSION_LANG_STORAGE_KEY, lang)
  } catch {
    // Private mode / blocked storage: the toggle still works for this session.
  }
}

// The matcher settings query answers 200 {configured: false} when no URL is
// set; older/other backends 404. Either way: not configured.
export function externalMatcherConfigured(status) {
  return Boolean(status) && status.configured !== false
}
