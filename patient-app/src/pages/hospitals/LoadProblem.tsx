import { Alert, Button } from '@atheris/ui'
import type { LoadFailure } from '@/pages/hospitals/loadFailure'
import { hospitalStrings as S } from '@/pages/hospitals/strings'

interface LoadProblemProps {
  failure: LoadFailure
  /** What to say when all that is known is that it did not load. */
  failedMessage: string
  onRetry: () => void
  isRetrying: boolean
}

/**
 * A hospital screen that could not be loaded: what went wrong, in the app's
 * own words, and a way to try again. "No connection" is said plainly and
 * apart from a failure on the server's side.
 */
export function LoadProblem({ failure, failedMessage, onRetry, isRetrying }: LoadProblemProps) {
  return (
    <div className="space-y-3">
      {failure === 'offline' ? (
        <Alert variant="warning" title={S.offlineTitle}>
          {S.offlineBody}
        </Alert>
      ) : (
        <Alert variant="error">
          {failure === 'policies_pending' ? S.policiesPending : failure === 'busy' ? S.busy : failedMessage}
        </Alert>
      )}
      <Button variant="outline" size="touch" onClick={onRetry} disabled={isRetrying}>
        {S.retry}
      </Button>
    </div>
  )
}
