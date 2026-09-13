import { describe, expect, it } from 'vitest'

import {
  UPLOAD_MAX_EDGE,
  downscaleForUpload,
  isSupportedScannerImage,
  uploadTargetSize,
} from './scannerImages'

describe('sizing an upload', () => {
  it('matches the bound the backend applies anyway', () => {
    // Not smaller. Going below the server's own limit would start deciding how
    // much detail recognition gets, which is a measured question owned there.
    expect(UPLOAD_MAX_EDGE).toBe(2048)
    expect(uploadTargetSize(3000, 4000)).toEqual({ width: 1536, height: 2048 })
    expect(uploadTargetSize(4000, 3000)).toEqual({ width: 2048, height: 1536 })
  })

  it('leaves an image the backend would not resize alone', () => {
    expect(uploadTargetSize(1536, 2048)).toBeNull()
    expect(uploadTargetSize(800, 600)).toBeNull()
  })

  it('keeps the aspect ratio, because the detector reads it', () => {
    // find_card_box rejects a box whose aspect is outside 0.35-1.35, so a
    // resize that squashed the frame would change what it finds.
    const target = uploadTargetSize(4032, 3024)
    expect(target.width / target.height).toBeCloseTo(4032 / 3024, 4)
  })

  it('survives a nonsense size rather than dividing by zero', () => {
    expect(uploadTargetSize(0, 0)).toBeNull()
  })
})

describe('downscaling is best effort', () => {
  const file = { name: 'x.jpg', size: 4_000_000, type: 'image/jpeg', lastModified: 1 }

  it('returns the original when there is no browser to do it in', async () => {
    // Sending the big version is exactly what happened before this existed, so
    // no failure here may cost the user a scan.
    expect(await downscaleForUpload(file)).toBe(file)
  })

  it('passes a missing file straight through', async () => {
    expect(await downscaleForUpload(null)).toBe(null)
  })
})

describe('accepted formats', () => {
  it('still accepts what the camera produces', () => {
    expect(isSupportedScannerImage({ type: 'image/jpeg' })).toBe(true)
    expect(isSupportedScannerImage({ type: 'image/heic' })).toBe(true)
    expect(isSupportedScannerImage({ type: 'application/pdf' })).toBe(false)
  })
})
