import { describe, expect, it } from 'vitest'

import { localScannerToggleState } from './ScannerSettingsCard'

const READY = { ready: true, coverage: 0.972 }
const NOT_READY = { ready: false, coverage: 0.11 }

describe('Scanner v2 toggle availability', () => {
  it('can be turned on once the offline index is ready', () => {
    const state = localScannerToggleState({ enabled: false, status: READY })
    expect(state.ready).toBe(true)
    expect(state.disabled).toBe(false)
  })

  it('cannot be turned on while the catalogue is still being fingerprinted', () => {
    const state = localScannerToggleState({ enabled: false, status: NOT_READY })
    expect(state.disabled).toBe(true)
    expect(state.coveragePercent).toBe(11)
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

  it('reports coverage as a whole percentage, and zero when unknown', () => {
    expect(localScannerToggleState({ status: READY }).coveragePercent).toBe(97)
    expect(localScannerToggleState({ status: {} }).coveragePercent).toBe(0)
    expect(localScannerToggleState({}).coveragePercent).toBe(0)
  })
})
