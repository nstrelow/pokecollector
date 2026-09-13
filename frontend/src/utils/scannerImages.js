export const SCANNER_IMAGE_ACCEPT = 'image/jpeg,image/png,image/webp,image/heic,image/heif'

const SUPPORTED_SCANNER_IMAGE_TYPES = new Set(SCANNER_IMAGE_ACCEPT.split(','))

export function isSupportedScannerImage(file) {
  return Boolean(file && SUPPORTED_SCANNER_IMAGE_TYPES.has(String(file.type || '').toLowerCase()))
}

// The backend re-encodes every upload down to a 2048px longest edge before it
// looks at it, so anything larger than that is bytes the user pays to send and
// the server pays to throw away. A 12MP phone photo is ~2.1MB; the same photo
// resized here is ~0.47MB, measured across 37 real ones — a 77% reduction.
//
// Matching the server's own bound exactly, rather than going smaller, is the
// point. Resizing further would start deciding how much detail recognition
// gets, which is a question with a measured answer that lives in the backend.
// This only removes detail the backend was going to discard anyway.
export const UPLOAD_MAX_EDGE = 2048
export const UPLOAD_JPEG_QUALITY = 0.9

// Deliberately NOT cropping to the card here. The backend's detector is what
// every accuracy figure was measured against, and a second implementation in
// JS would have to agree with it pixel for pixel or quietly change results.
// Shrinking is safe in a way cropping is not: it costs one extra JPEG
// generation, which moved the resulting hash on 1 of 37 real photos, by 2 bits
// out of 64 — against matches that land at 0 to 10.
export function uploadTargetSize(width, height, maxEdge = UPLOAD_MAX_EDGE) {
  const longest = Math.max(width, height)
  if (!longest || longest <= maxEdge) return null
  const scale = maxEdge / longest
  return { width: Math.round(width * scale), height: Math.round(height * scale) }
}

async function decode(file) {
  // `imageOrientation: 'from-image'` applies the EXIF rotation into the pixels.
  // Without it a canvas re-encode silently drops the EXIF flag and every photo
  // taken sideways would arrive rotated — the backend normalises orientation
  // today and would no longer get the chance.
  if (typeof createImageBitmap === 'function') {
    return createImageBitmap(file, { imageOrientation: 'from-image' })
  }
  throw new Error('no decoder')
}

// Best effort, always. Every failure path returns the ORIGINAL file: an
// unsupported codec (Safari and HEIC), a missing canvas, an out-of-memory on a
// huge image. Sending the big version still works — it is what happens today —
// so nothing here is allowed to cost the user a scan.
export async function downscaleForUpload(file, {
  maxEdge = UPLOAD_MAX_EDGE,
  quality = UPLOAD_JPEG_QUALITY,
} = {}) {
  if (!file || typeof document === 'undefined') return file
  try {
    const bitmap = await decode(file)
    const target = uploadTargetSize(bitmap.width, bitmap.height, maxEdge)
    if (!target) {
      bitmap.close?.()
      return file
    }
    const canvas = document.createElement('canvas')
    canvas.width = target.width
    canvas.height = target.height
    const context = canvas.getContext('2d')
    if (!context) return file
    context.drawImage(bitmap, 0, 0, target.width, target.height)
    bitmap.close?.()

    const blob = await new Promise(resolve =>
      canvas.toBlob(resolve, 'image/jpeg', quality))
    // A resize that came out bigger is not a saving. Real photos never do this,
    // but a small PNG of flat colour can, and sending the larger one would be
    // a pure loss.
    if (!blob || blob.size >= file.size) return file
    return new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg',
      { type: 'image/jpeg', lastModified: file.lastModified })
  } catch {
    return file
  }
}

export async function downscaleAllForUpload(files) {
  return Promise.all(Array.from(files || []).map(file => downscaleForUpload(file)))
}
