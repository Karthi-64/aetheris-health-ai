import { Link, useRouteError } from 'react-router-dom'
import { Button } from '@/components/ui/button'

// What each browser says when a lazily-loaded page's file can no longer be
// fetched — typically because a newer build replaced it while the tab was open.
const CHUNK_LOAD_FAILURE =
  /dynamically imported module|importing a module script failed|loading chunk|chunkloaderror/i

function isChunkLoadFailure(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  return CHUNK_LOAD_FAILURE.test(`${error.name} ${error.message}`)
}

/**
 * Shown by the router when a page throws while rendering or its code fails to
 * load. It deliberately shows nothing from the error itself: a stack trace is
 * no use to the person looking at it and should not be on a clinical screen.
 */
export default function RouteErrorPage() {
  const error = useRouteError()
  const stale = isChunkLoadFailure(error)

  return (
    <div
      role="alert"
      className="flex min-h-[100dvh] flex-col items-center justify-center gap-4 px-4 py-24 text-center"
    >
      <h1 className="font-display text-headline-lg text-primary">
        {stale ? 'This page could not be loaded' : 'Something went wrong'}
      </h1>
      <p className="font-body text-body-md text-on-surface-variant max-w-md">
        {stale
          ? 'The app may have been updated since you opened it. Reload to get the latest version.'
          : 'Sorry — this page ran into a problem. Your saved work is not affected. Go back to the dashboard, or reload the app.'}
      </p>
      <p className="font-body text-outline max-w-md text-xs">
        Reloading will ask you to sign in again.
      </p>
      <div className="flex flex-wrap items-center justify-center gap-3">
        <Button type="button" className="rounded-full" onClick={() => window.location.reload()}>
          Reload
        </Button>
        <Button asChild variant="outline" className="rounded-full">
          <Link to="/dashboard">Back to dashboard</Link>
        </Button>
      </div>
    </div>
  )
}
