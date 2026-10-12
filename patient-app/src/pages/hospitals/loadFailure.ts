import { ApiError } from '@atheris/api-core'

/** Every way a hospital screen can fail to load, as the app tells them apart. */
export type LoadFailure = 'offline' | 'not_found' | 'policies_pending' | 'busy' | 'error'

/**
 * Classify a failed read by what really happened:
 *
 * - an answer from the server is taken at its word — 404 `RESOURCE_NOT_FOUND`
 *   (unknown and unavailable hospitals are one answer on purpose), 403
 *   `CONSENT_REQUIRED` (a platform policy is pending), 429;
 * - no answer at all — the request never reached the server, or the browser
 *   says it has no network — is `offline`;
 * - anything else is `error`. The server's own text is never shown.
 */
export function loadFailureOf(err: unknown): LoadFailure {
  const answered = err instanceof ApiError && err.status !== undefined
  if (answered) {
    if (err.code === 'RESOURCE_NOT_FOUND' || err.status === 404) return 'not_found'
    if (err.code === 'CONSENT_REQUIRED') return 'policies_pending'
    if (err.status === 429) return 'busy'
    return 'error'
  }
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return 'offline'
  return err instanceof ApiError && err.code === 'network_error' ? 'offline' : 'error'
}
