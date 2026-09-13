export const SCANNER_REQUEST_TIMEOUT_OPTIONS = [30, 60, 120, 180]
export const DEFAULT_SCANNER_REQUEST_TIMEOUT_SECONDS = SCANNER_REQUEST_TIMEOUT_OPTIONS[0]

const normalizedScannerRequestTimeout = (seconds) => (
  SCANNER_REQUEST_TIMEOUT_OPTIONS.includes(Number(seconds))
    ? Number(seconds)
    : DEFAULT_SCANNER_REQUEST_TIMEOUT_SECONDS
)

export const scannerTestRequestTimeoutMs = (seconds) => {
  const selected = normalizedScannerRequestTimeout(seconds)
  // The capability test can make a three-attempt multi-image request followed
  // by a three-attempt single-image fallback. Cover the backend's complete
  // bounded flow rather than letting Axios abandon a result first.
  return (selected * 6 + 10) * 1000
}

export const scannerRecognitionRequestTimeoutMs = (seconds) => {
  const selected = normalizedScannerRequestTimeout(seconds)
  // A synchronous legacy/API scan can use three extraction attempts and two
  // visual-verification attempts. The extra 90 seconds covers bounded TCGdex
  // searches, reference downloads, retry backoff, and database work.
  return (selected * 5 + 90) * 1000
}

// An upload is not a server-work timeout, and must not share one. How long it
// takes is the payload divided by the user's uplink: the API instance's 30s
// default is ample for a request the server has to think about, and far too
// short for nine phone photos over mobile data, which is exactly how it failed
// -- Axios aborted mid-body, nginx logged the client disconnect as a 400, and
// the backend never saw the request at all.
//
// Generous rather than tuned. It is a ceiling for a stalled connection, not a
// wait anybody sits through: a working upload finishes long before it, and the
// backend caps the payload at 15MB per photo and 200MB per job regardless.
export const SCAN_UPLOAD_BASE_TIMEOUT_SECONDS = 60
export const SCAN_UPLOAD_PER_FILE_SECONDS = 45
export const SCAN_UPLOAD_MAX_TIMEOUT_SECONDS = 900

export const scanUploadTimeoutMs = (fileCount) => {
  const files = Number.isFinite(fileCount) && fileCount > 0 ? Math.floor(fileCount) : 1
  const seconds = Math.min(
    SCAN_UPLOAD_BASE_TIMEOUT_SECONDS + files * SCAN_UPLOAD_PER_FILE_SECONDS,
    SCAN_UPLOAD_MAX_TIMEOUT_SECONDS,
  )
  return seconds * 1000
}

// Axios reports a timeout with no `response` at all, which is why a timed-out
// upload surfaced as the generic "batch could not be submitted" -- the code
// looked for `error.response.data.detail` and found nothing.
export const isUploadTimeout = (error) => (
  Boolean(error) && !error.response && (
    error.code === 'ECONNABORTED'
    || error.code === 'ETIMEDOUT'
    || /timeout/i.test(error.message || '')
  )
)
