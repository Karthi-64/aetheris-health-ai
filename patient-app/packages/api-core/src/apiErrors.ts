import { ApiError } from './types'

/** One field-level error from a 422, as the API names it. */
export interface FieldError {
  /** Dot path into the request body, e.g. `address.country` or `allergies.0.name`. Empty for a whole-body rule. */
  field: string
  message: string
}

/** Pydantic prefixes the message of every custom rule with this; it is noise to a user. */
const cleanMessage = (message: string) => message.replace(/^Value error, /, '')

/**
 * Field-level errors from a 422. Request validation sends a list at `errors`;
 * a rule a service enforces sends `{ errors: [...] }` — both name the field
 * the same way. Any other status, and any other shape of `errors` (a 404
 * carries a context object there), yields none.
 */
export function fieldErrorsOf(err: unknown): FieldError[] {
  if (!(err instanceof ApiError) || err.status !== 422) return []
  const details = err.details as unknown
  const list = Array.isArray(details)
    ? details
    : Array.isArray((details as { errors?: unknown } | null)?.errors)
      ? (details as { errors: unknown[] }).errors
      : []
  return list
    .filter(
      (e): e is FieldError =>
        typeof (e as FieldError)?.field === 'string' && typeof (e as FieldError)?.message === 'string',
    )
    .map((e) => ({ field: e.field, message: cleanMessage(e.message) }))
}

/**
 * Split a 422's field errors into those a form can show under a field and the
 * rest, which belong above the form. `isFormField` decides which is which.
 */
export function splitFieldErrors(
  err: unknown,
  isFormField: (field: string) => boolean,
): { onFields: FieldError[]; other: string[] } {
  const all = fieldErrorsOf(err)
  return {
    onFields: all.filter((e) => isFormField(e.field)),
    other: all.filter((e) => !isFormField(e.field)).map((e) => e.message),
  }
}

/**
 * The message to show for a failed API call. The API's own wording is used for
 * the outcomes it writes for users — a rule broken (400), not allowed (403),
 * gone (404), a conflict (409), invalid input (422). Anything else (a 5xx, the
 * network) gets the caller's fallback rather than internal text.
 */
export function apiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError && [400, 403, 404, 409, 422].includes(err.status ?? 0)) {
    return err.message
  }
  return fallback
}

/** True when `path` (dot-separated, numeric segments index arrays) exists in `values`. */
export function hasPath(values: unknown, path: string): boolean {
  if (!path) return false
  let current: unknown = values
  for (const key of path.split('.')) {
    if (current === null || typeof current !== 'object' || !(key in current)) return false
    current = (current as Record<string, unknown>)[key]
  }
  return true
}
