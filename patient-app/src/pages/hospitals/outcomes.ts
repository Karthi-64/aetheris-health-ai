import { ApiError } from '@atheris/api-core'

/** Every way a link or registration can be turned down, as the app tells them apart. */
export type LinkFailure =
  | 'mrn_required'
  | 'not_found'
  | 'unavailable'
  | 'conflict'
  | 'stale'
  | 'policies_pending'
  | 'invalid'
  | 'busy'
  | 'error'

/**
 * The server's own wording for the three conflicts that are about records
 * (`backend/app/services/patient_app/record_link_service.py`). They share the
 * code `RESOURCE_CONFLICT` with a fourth 409 that is not about records at all:
 * a consent version this build no longer matches. The server gives that one no
 * code of its own, so a conflict is treated as "about a record" only when it
 * says so in these exact words. Anything else — the stale version, or wording
 * this build does not know — gets the neutral "reload" answer, never a claim
 * that a record is already linked. These strings are compared, never shown.
 */
const RECORD_CONFLICTS: ReadonlySet<string> = new Set([
  'You are already linked to a record at this hospital.',
  'A record may already exist for you at this hospital. Please link to it instead.',
  'You have already registered at this hospital.',
])

/**
 * Classify a failed link or registration by what the server can really send:
 *
 * - 404 `RESOURCE_NOT_FOUND` — "no record", "wrong date of birth", "wrong MRN"
 *   and "unknown hospital" all arrive as this one answer on purpose; the app
 *   cannot tell them apart and does not try to.
 * - 409 `LINK_MRN_REQUIRED` — more than one record; the MRN is needed.
 * - 409 `RESOURCE_CONFLICT` — already linked / registered / a record exists
 *   (`conflict`), or the consent version is not current (`stale`).
 * - 403 `LINK_UNAVAILABLE` — deactivated record, or too many attempts.
 * - 403 `CONSENT_REQUIRED` — a platform policy has not been accepted.
 * - 422 `VALIDATION_ERROR` — the details fail the server's rules.
 * - 429 — the API's general rate limit.
 */
export function linkFailureOf(err: unknown): LinkFailure {
  if (!(err instanceof ApiError)) return 'error'
  if (err.code === 'LINK_MRN_REQUIRED') return 'mrn_required'
  if (err.code === 'LINK_UNAVAILABLE') return 'unavailable'
  if (err.code === 'CONSENT_REQUIRED') return 'policies_pending'
  if (err.code === 'RESOURCE_NOT_FOUND' || err.status === 404) return 'not_found'
  if (err.code === 'RESOURCE_CONFLICT' || err.status === 409) {
    return RECORD_CONFLICTS.has(err.message) ? 'conflict' : 'stale'
  }
  if (err.status === 422) return 'invalid'
  if (err.status === 429) return 'busy'
  return 'error'
}

/** How a finished link or registration ended. */
export type LinkSuccess = 'linked' | 'already_linked' | 'registered'

/** The details a patient tried to link with, kept for the registration step. */
export interface LinkAttempt {
  hospitalRef: string
  dateOfBirth: string
}
