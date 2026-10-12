import { Button } from '@atheris/ui'

/**
 * Shown when a page fails to load or throws while rendering. It says nothing
 * about the cause: an error object can carry anything.
 */
export function RouteError() {
  return (
    <main className="bg-background flex min-h-dvh flex-col items-center justify-center gap-4 px-6 text-center">
      <h1 className="font-display text-headline-md text-primary">Something went wrong</h1>
      <p className="text-body-sm text-on-surface-variant max-w-sm">
        This page could not be shown. Please reload and try again.
      </p>
      <Button size="touch" onClick={() => window.location.reload()}>
        Reload
      </Button>
    </main>
  )
}
