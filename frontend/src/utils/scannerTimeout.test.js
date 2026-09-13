import { describe, expect, it } from 'vitest'

import {
  SCAN_UPLOAD_MAX_TIMEOUT_SECONDS,
  isUploadTimeout,
  scanUploadTimeoutMs,
} from './scannerTimeout'

describe('scan upload timeout', () => {
  it('is far longer than the API default for a real batch', () => {
    // Nine phone photos over mobile data exceeded the 30s API default, Axios
    // aborted mid-body, and the user saw "scan batch failed".
    expect(scanUploadTimeoutMs(9)).toBeGreaterThan(30_000)
    expect(scanUploadTimeoutMs(9)).toBe((60 + 9 * 45) * 1000)
  })

  it('grows with the number of photos', () => {
    expect(scanUploadTimeoutMs(10)).toBeGreaterThan(scanUploadTimeoutMs(1))
  })

  it('is capped so a dead connection still ends', () => {
    expect(scanUploadTimeoutMs(50)).toBe(SCAN_UPLOAD_MAX_TIMEOUT_SECONDS * 1000)
  })

  it('survives a nonsense count rather than disabling the timeout', () => {
    for (const bad of [0, -3, undefined, null, NaN, 'seven']) {
      expect(scanUploadTimeoutMs(bad)).toBe((60 + 45) * 1000)
    }
  })
})

describe('recognising an aborted upload', () => {
  it('matches an Axios timeout, which carries no response', () => {
    expect(isUploadTimeout({ code: 'ECONNABORTED', message: 'timeout of 30000ms exceeded' })).toBe(true)
    expect(isUploadTimeout({ code: 'ETIMEDOUT' })).toBe(true)
    expect(isUploadTimeout({ message: 'Network timeout' })).toBe(true)
  })

  it('does not swallow a real server rejection', () => {
    expect(isUploadTimeout({ response: { status: 400, data: { detail: 'too many files' } } })).toBe(false)
    expect(isUploadTimeout({ code: 'ERR_NETWORK', message: 'Network Error' })).toBe(false)
    expect(isUploadTimeout(undefined)).toBe(false)
  })
})
