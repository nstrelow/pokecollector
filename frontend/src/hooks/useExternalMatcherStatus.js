import { useQuery } from '@tanstack/react-query'

import { getExternalMatcherStatus } from '../api/client'

export const EXTERNAL_MATCHER_QUERY_KEY = ['scanner-external-matcher']

// Statuses that mean "there is no external matcher for you to see" rather
// than an error worth surfacing: not configured / not supported (404), or not
// an administrator in multi-user mode (403).
const HIDDEN_STATUSES = new Set([401, 403, 404])

export async function loadExternalMatcherStatus() {
  try {
    return await getExternalMatcherStatus()
  } catch (error) {
    const status = error?.response?.status
    if (HIDDEN_STATUSES.has(status)) return null
    // A 503 means configured but down: keep the block visible with "down".
    if (status === 503) {
      return { configured: true, health: { ok: false }, bundle: null, error: error?.response?.data?.detail || null }
    }
    throw error
  }
}

// Shared by Scanner Settings and the scanner modal (session-language toggle),
// cached so opening the scanner does not re-probe the matcher every time.
export function useExternalMatcherStatus({ enabled = true } = {}) {
  return useQuery({
    queryKey: EXTERNAL_MATCHER_QUERY_KEY,
    queryFn: loadExternalMatcherStatus,
    enabled,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
}
