import { RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/api/types'

/** Refusals the API words for the user, and that asking again cannot change. */
const REFUSED = [400, 403]
/** Too many requests: the API says so, and asking again later is the remedy. */
const RATE_LIMITED = 429

/**
 * Why a read failed, with a Retry where one can help.
 *
 * Every pharmacy route can refuse a signed-in user in the API's own words —
 * a 403 for an account that is not active or lacks the permission, a 400 for
 * an account not scoped to a hospital, a 429 when rate limited
 * (docs/18-API_CONTRACTS.md §9.1). Calling those a connection problem names
 * the wrong cause, and a Retry cannot fix the first two. Anything else — a
 * 5xx, the network — gets the caller's wording, never the server's text.
 */
export function PrescriptionLoadError({
  title,
  error,
  fallback,
  onRetry,
  variant = 'error',
}: {
  title: string
  error: unknown
  /** What to say when the cause is not one the API words for users. */
  fallback: string
  onRetry: () => void
  variant?: 'error' | 'warning'
}) {
  const status = error instanceof ApiError ? (error.status ?? 0) : 0
  const refused = REFUSED.includes(status)
  const message = error instanceof ApiError && (refused || status === RATE_LIMITED) ? error.message : fallback
  return (
    <Alert variant={variant} title={title}>
      <div className="flex flex-col items-start gap-3">
        <p>{message}</p>
        {!refused && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RotateCw className="size-4" /> Retry
          </Button>
        )}
      </div>
    </Alert>
  )
}
