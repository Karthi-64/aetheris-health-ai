import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { queryClient } from '@/lib/query-client'
import type { AppRouter } from '@/router'
import { SessionRestorer } from '@/session/SessionRestorer'

/**
 * The app's providers, in the order they depend on each other: the query
 * cache, then session restore (which holds every route back until it knows
 * whether a session exists), then the router.
 *
 * There is no theme provider: the one the Hospital app uses remembers its
 * choice in `localStorage`, and the Patient App keeps nothing in web storage.
 */
export function AppProviders({ router }: { router: AppRouter }) {
  return (
    <QueryClientProvider client={queryClient}>
      <SessionRestorer>
        <RouterProvider router={router} />
      </SessionRestorer>
    </QueryClientProvider>
  )
}
