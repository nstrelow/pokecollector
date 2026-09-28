import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bug, Plus, Search } from 'lucide-react'

import { fetchApiImageUrl, getScanItemMatcher } from '../api/client'
import {
  TIMING_COLORS,
  TIMING_REMAINDER_COLOR,
  TONE_CLASSES,
  activeFlags,
  formatScore,
  isExternalItem,
  matcherArtifacts,
  quadPoints,
  scorePercent,
  stateTone,
  timingSegments,
  validImageSize,
  validQuad,
} from '../utils/matcherDebug'

// Debug view of what the external matcher (pokescan) saw and decided for one
// scan: detected card boundary, rectified plane, decision state + flags,
// language/number evidence, ranked candidates and per-stage timings.
// Payload contract: docs/POKESCANNER-PLAN.md §4.2.

// ─── Authenticated artefact → object URL ────────────────────────────────────
function useApiImage(path) {
  const [state, setState] = useState({ url: null, failed: false })
  useEffect(() => {
    setState({ url: null, failed: false })
    if (!path) return undefined
    let disposed = false
    let objectUrl = null
    fetchApiImageUrl(path)
      .then(url => {
        if (disposed) return URL.revokeObjectURL(url)
        objectUrl = url
        setState({ url, failed: false })
      })
      .catch(() => { if (!disposed) setState({ url: null, failed: true }) })
    return () => {
      disposed = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [path])
  return state
}

const SECTION_LABEL = 'text-[10px] font-black uppercase tracking-[0.16em] text-text-muted'
const CHIP = 'inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold'

function Placeholder({ children }) {
  return (
    <div className="grid aspect-[5/7] w-full place-items-center rounded-xl border border-dashed border-white/10 bg-white/[0.02] p-2 text-center text-[10px] text-text-muted">
      {children}
    </div>
  )
}

// ─── (a) Source photo with the detected quad ────────────────────────────────
// The SVG uses the reference image size as its viewBox and is laid over an
// object-contain image in the same box, so both letterbox identically and
// the browser scales the quad from upload pixels to the rendered size.
export function QuadOverlayImage({ src, geometry, t }) {
  const quad = validQuad(geometry)
  const declaredSize = validImageSize(geometry)
  const [naturalSize, setNaturalSize] = useState(null)
  const size = declaredSize || naturalSize

  if (!src) return <Placeholder>{t('matcher.noSourceImage')}</Placeholder>
  return (
    <div className="relative aspect-[5/7] w-full overflow-hidden rounded-xl bg-black/40" data-testid="matcher-source">
      <img
        src={src}
        alt={t('scanner.yourPhoto')}
        className="absolute inset-0 h-full w-full object-contain"
        onLoad={event => {
          const { naturalWidth, naturalHeight } = event.currentTarget
          if (naturalWidth && naturalHeight) setNaturalSize([naturalWidth, naturalHeight])
        }}
      />
      {quad && size && (
        <svg
          className="pointer-events-none absolute inset-0 h-full w-full"
          viewBox={`0 0 ${size[0]} ${size[1]}`}
          preserveAspectRatio="xMidYMid meet"
          aria-label={t('matcher.detectedBoundary')}
          role="img"
        >
          <polygon
            points={quadPoints(quad)}
            fill="rgba(34,197,94,0.12)"
            stroke="#22c55e"
            strokeWidth={Math.max(size[0], size[1]) / 200}
            strokeLinejoin="round"
          />
          <circle cx={quad[0][0]} cy={quad[0][1]} r={Math.max(size[0], size[1]) / 120} fill="#e3000b" />
        </svg>
      )}
      {!quad && (
        <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1.5 py-0.5 text-[9px] text-text-secondary">
          {t('matcher.noGeometry')}
        </span>
      )}
    </div>
  )
}

function StateBadge({ state, confident, t }) {
  return (
    <span className={`${CHIP} text-[11px] font-black tracking-wide ${TONE_CLASSES[stateTone(state)]}`}
      data-state={state || 'UNKNOWN'}>
      {state || t('matcher.unknownState')}
      {confident === true && <span className="ml-1 opacity-80">· {t('matcher.confident')}</span>}
    </span>
  )
}

function KeyValue({ label, children }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[11px]">
      <span className="text-text-muted">{label}</span>
      <span className="min-w-0 truncate text-right font-mono text-text-primary">{children}</span>
    </div>
  )
}

function ScoreBar({ value }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/10">
      <div className="h-full rounded-full bg-green" style={{ width: `${scorePercent(value)}%` }} />
    </div>
  )
}

// ─── (e) Candidates ─────────────────────────────────────────────────────────
// Informational ranking plus the two existing actions: compare (the linked
// zoom review) and add (the same resolve-and-add flow as the grid below).
function CandidateList({ matches, onSelect, onCompare, t }) {
  if (!matches?.length) return <p className="text-[11px] text-text-muted">{t('matcher.noCandidates')}</p>
  return (
    <ol className="space-y-1.5">
      {matches.map((match, index) => (
        <li key={`${match.id || match.tcg_card_id}-${index}`}
          className="flex items-center gap-2 rounded-lg border border-white/[0.06] bg-white/[0.02] p-1.5">
          <span className="w-4 flex-shrink-0 text-center text-[10px] font-bold text-text-muted">{index + 1}</span>
          <button type="button" onClick={() => onCompare?.(match, index)} disabled={!onCompare}
            className="h-12 w-9 flex-shrink-0 overflow-hidden rounded bg-bg-surface" aria-label={t('scanner.compareCandidate')}>
            {match.image
              ? <img src={match.image} alt="" loading="lazy" className="h-full w-full object-cover" />
              : null}
          </button>
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex items-baseline gap-2">
              <span className="truncate text-xs font-bold text-white">{match.name || match.tcg_card_id}</span>
              <span className="flex-shrink-0 font-mono text-[10px] text-brand-red/80">
                {[match.set_id, match.number].filter(Boolean).join(' · ')}
              </span>
              <span className="flex-shrink-0 rounded bg-white/10 px-1 text-[9px] font-bold uppercase text-text-secondary">
                {match.lang || match._lang || '?'}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <ScoreBar value={match._score} />
              <span className="w-9 flex-shrink-0 text-right font-mono text-[10px] text-text-secondary">{formatScore(match._score)}</span>
              <span className="w-14 flex-shrink-0 text-right font-mono text-[10px] text-text-muted" title={t('matcher.margin')}>
                Δ {formatScore(match._margin)}
              </span>
            </div>
          </div>
          {onSelect && (
            <button type="button" onClick={() => onSelect(match)}
              title={t('scanner.addToCollection')} aria-label={t('scanner.addToCollection')}
              className="grid h-7 w-7 flex-shrink-0 place-items-center rounded-full bg-brand-red text-white hover:scale-110">
              <Plus size={13} />
            </button>
          )}
        </li>
      ))}
    </ol>
  )
}

// ─── (f) Timings ────────────────────────────────────────────────────────────
function TimingsBar({ timings, t }) {
  const { segments, total } = timingSegments(timings)
  if (!segments.length) return <p className="text-[11px] text-text-muted">{t('matcher.noTimings')}</p>
  const colorOf = (segment, index) => (
    segment.remainder ? TIMING_REMAINDER_COLOR : TIMING_COLORS[index % TIMING_COLORS.length]
  )
  const labelOf = segment => (segment.remainder ? t('matcher.otherTiming') : segment.key)
  return (
    <div className="space-y-1.5">
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-white/10" role="img"
        aria-label={`${t('matcher.timings')}: ${Math.round(total)} ms`}>
        {segments.map((segment, index) => (
          <div key={segment.key} title={`${labelOf(segment)}: ${Math.round(segment.ms)} ms`} data-timing={segment.key}
            style={{ width: `${segment.percent}%`, background: colorOf(segment, index) }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-text-secondary">
        {segments.map((segment, index) => (
          <span key={segment.key} className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm" style={{ background: colorOf(segment, index) }} />
            {labelOf(segment)} <span className="font-mono">{Math.round(segment.ms)}</span>
          </span>
        ))}
        <span className="font-bold text-text-primary">{t('matcher.total')} <span className="font-mono">{Math.round(total)} ms</span></span>
      </div>
    </div>
  )
}

// ─── Presentational panel (tested directly) ─────────────────────────────────
export function MatcherDebugView({
  matcher, sourceUrl, planeUrl, planeFailed = false, matches, onSelect, onCompare, defaultOpen = true, className = '', t,
}) {
  if (!matcher) return null
  const flags = matcher.flags || {}
  const chips = activeFlags(matcher)
  const language = matcher.language || {}
  const scores = language.scores && typeof language.scores === 'object' ? Object.entries(language.scores) : []
  const number = matcher.number || {}
  const engine = matcher.matcher || {}

  return (
    <details open={defaultOpen} className={`group rounded-xl border border-white/10 bg-black/20 ${className}`} data-testid="matcher-debug-panel">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2">
        <Bug size={13} className="text-text-muted" />
        <span className="text-[11px] font-bold text-text-primary">{t('matcher.debugTitle')}</span>
        <StateBadge state={matcher.state} confident={matcher.confident} t={t} />
        {engine.name && (
          <span className="ml-auto truncate font-mono text-[10px] text-text-muted">
            {[engine.name, engine.commit, engine.bundle].filter(Boolean).join(' · ')}
          </span>
        )}
      </summary>

      <div className="space-y-4 border-t border-white/[0.06] p-3">
        {matcher.hint && (
          <p role="note" className="rounded-lg border border-yellow/30 bg-yellow/10 px-2.5 py-1.5 text-[11px] text-yellow">
            {matcher.hint}
          </p>
        )}

        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.4fr)]">
          <figure className="space-y-1">
            <figcaption className={SECTION_LABEL}>{t('matcher.sourceWithBoundary')}</figcaption>
            <QuadOverlayImage src={sourceUrl} geometry={matcher.geometry} t={t} />
            {matcher.geometry?.source && (
              <p className="text-[10px] text-text-muted">{t('matcher.geometrySource')}: <span className="font-mono">{matcher.geometry.source}</span></p>
            )}
          </figure>

          <figure className="space-y-1">
            <figcaption className={SECTION_LABEL}>{t('matcher.rectified')}</figcaption>
            {planeUrl
              ? <img src={planeUrl} alt={t('matcher.rectified')} className="w-full rounded-xl bg-black/40 object-contain" data-testid="matcher-plane" />
              : <Placeholder>{planeFailed ? t('matcher.artifactUnavailable') : t('matcher.noPlane')}</Placeholder>}
          </figure>

          <div className="space-y-3">
            <div className="space-y-1.5">
              <p className={SECTION_LABEL}>{t('matcher.flags')}</p>
              {chips.length
                ? (
                  <div className="flex flex-wrap gap-1">
                    {chips.map(flag => (
                      <span key={flag} data-flag={flag} className={`${CHIP} ${TONE_CLASSES.warn}`}>{flag}</span>
                    ))}
                  </div>
                )
                : <p className="text-[11px] text-text-muted">{t('matcher.noFlags')}</p>}
              <KeyValue label={t('matcher.script')}>{flags.script ?? '–'}</KeyValue>
              <KeyValue label={t('matcher.orientation')}>{flags.orientation != null ? `${flags.orientation}°` : '–'}</KeyValue>
              <KeyValue label={t('matcher.cardBackScore')}>{formatScore(flags.card_back_score)}</KeyValue>
            </div>

            <div className="space-y-1.5">
              <p className={SECTION_LABEL}>{t('matcher.language')}</p>
              <KeyValue label={t('matcher.languageSource')}>{language.source ?? '–'}</KeyValue>
              {scores.length > 0 && (
                <KeyValue label={t('matcher.languageScores')}>
                  {scores.map(([lang, score]) => `${lang} ${formatScore(score)}`).join(' · ')}
                </KeyValue>
              )}
              {Array.isArray(language.options) && language.options.length > 0 && (
                <KeyValue label={t('matcher.languageOptions')}>{language.options.join(', ')}</KeyValue>
              )}
              {language.unsupported && (
                <KeyValue label={t('matcher.languageUnsupported')}>{String(language.unsupported)}</KeyValue>
              )}
            </div>

            <div className="space-y-1.5">
              <p className={SECTION_LABEL}>{t('matcher.number')}</p>
              <KeyValue label={t('matcher.numberReading')}>{number.reading ?? '–'}</KeyValue>
              <KeyValue label={t('matcher.numberVerdict')}>{number.verdict ?? '–'}</KeyValue>
              {matcher.twin?.verdict && <KeyValue label={t('matcher.twinVerdict')}>{matcher.twin.verdict}</KeyValue>}
            </div>
          </div>
        </div>

        <div className="space-y-1.5">
          <p className={SECTION_LABEL}>
            <Search size={10} className="mr-1 inline" />{t('matcher.candidates')} ({matches?.length || 0})
          </p>
          <CandidateList matches={matches} onSelect={onSelect} onCompare={onCompare} t={t} />
        </div>

        <div className="space-y-1.5">
          <p className={SECTION_LABEL}>{t('matcher.timings')}</p>
          <TimingsBar timings={matcher.timings_ms} t={t} />
        </div>

        {(matcher.print_id || matcher.trace_id) && (
          <div className="space-y-0.5">
            {matcher.print_id && <KeyValue label="print_id">{matcher.print_id}</KeyValue>}
            {matcher.identity_key && <KeyValue label="identity_key">{matcher.identity_key}</KeyValue>}
            {matcher.trace_id && <KeyValue label="trace_id">{matcher.trace_id}</KeyValue>}
          </div>
        )}
      </div>
    </details>
  )
}

// ─── Container: fetches the stored blob for one scan item ───────────────────
// Only asked for external-matcher items. A 404 (not external, or the blob was
// dropped) hides the panel silently: this is diagnostics, not a failure.
export default function MatcherDebugPanel({ jobId, item, photoUrl, onSelect, onCompare, className, t }) {
  const external = isExternalItem(item)
  const enabled = item?.status === 'done' && (external || Boolean(item?.has_matcher))
  const { data } = useQuery({
    queryKey: ['scan-item-matcher', jobId, item?.id],
    queryFn: () => getScanItemMatcher(jobId, item.id),
    enabled,
    retry: false,
    staleTime: Infinity,
  })
  const artifacts = matcherArtifacts(data)
  const plane = useApiImage(enabled && data?.matcher ? artifacts.plane : null)

  if (!enabled || !data?.matcher) return null
  return (
    <MatcherDebugView
      matcher={data.matcher}
      sourceUrl={photoUrl}
      planeUrl={plane.url}
      planeFailed={plane.failed}
      matches={item.matches}
      onSelect={onSelect}
      onCompare={onCompare}
      defaultOpen={external}
      className={className}
      t={t}
    />
  )
}
