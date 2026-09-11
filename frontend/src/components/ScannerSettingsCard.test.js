import { describe, expect, it } from 'vitest'

import { localScannerToggleState } from './ScannerSettingsCard'

// The reference catalogue: 44,466 of 45,737 cards with artwork are hashed,
// but only 44,466 of 58,630 catalogue rows, because 22% have no picture at all.
const READY = { ready: true, coverage: 0.972, catalogue_coverage: 0.758 }
const NOT_READY = { ready: false, coverage: 0.11, catalogue_coverage: 0.086 }

describe('Scanner v2 toggle availability', () => {
  it('can be turned on once the offline index is ready', () => {
    const state = localScannerToggleState({ enabled: false, status: READY })
    expect(state.ready).toBe(true)
    expect(state.disabled).toBe(false)
  })

  it('cannot be turned on while the catalogue is still being fingerprinted', () => {
    const state = localScannerToggleState({ enabled: false, status: NOT_READY })
    expect(state.disabled).toBe(true)
    expect(state.backfillPercent).toBe(11)
  })

  it('can still be turned off after the index stops being ready', () => {
    // Otherwise a resynced catalogue traps the user on a scanner that 503s.
    expect(
      localScannerToggleState({ enabled: true, status: NOT_READY }).disabled,
    ).toBe(false)
    expect(
      localScannerToggleState({ enabled: true, statusFailed: true }).disabled,
    ).toBe(false)
  })

  it('does not offer to turn on when readiness is unknown', () => {
    expect(
      localScannerToggleState({ enabled: false, loading: true }).disabled,
    ).toBe(true)
    expect(
      localScannerToggleState({ enabled: false, statusFailed: true }).disabled,
    ).toBe(true)
    expect(
      localScannerToggleState({ enabled: false, status: undefined }).disabled,
    ).toBe(true)
  })

  it('is held still while a change is being saved', () => {
    expect(
      localScannerToggleState({ enabled: true, status: READY, saving: true }).disabled,
    ).toBe(true)
  })

  it('reports both percentages as whole numbers, and zero when unknown', () => {
    expect(localScannerToggleState({ status: READY }).backfillPercent).toBe(97)
    expect(localScannerToggleState({ status: READY }).cataloguePercent).toBe(76)
    for (const field of ['cataloguePercent', 'backfillPercent']) {
      expect(localScannerToggleState({ status: {} })[field]).toBe(0)
      expect(localScannerToggleState({})[field]).toBe(0)
    }
  })

  it('does not show the backfill fraction where the catalogue one belongs', () => {
    // 97% of cards that HAVE artwork are hashed; 76% of the catalogue is
    // matchable. The card promised the catalogue and printed the backfill,
    // overstating what the scanner can recognise by twenty-one points.
    const state = localScannerToggleState({ status: READY })
    expect(state.cataloguePercent).toBeLessThan(state.backfillPercent)
  })
})
