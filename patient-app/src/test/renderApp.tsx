import { StrictMode } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { AppProviders } from '@/AppProviders'
import { queryClient } from '@/lib/query-client'
import { routes } from '@/router'
import { usePatientAuthStore } from '@/store/patient-auth-store'

/** The app's real route table, on an in-memory history. */
const routerAt = (path: string) => createMemoryRouter(routes, { initialEntries: [path] })

/**
 * Render the real routes at `path`, after session restore — the state every
 * page test starts from. Use {@link signIn} first for a signed-in patient.
 */
export function renderApp(path = '/') {
  const router = routerAt(path)
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { router, user: userEvent.setup() }
}

/**
 * Render the whole app as the browser loads it: nothing in memory (unless an
 * `accessToken` is given), session restore still to run, and React's
 * StrictMode double-invoking effects.
 */
export function loadApp(path = '/', accessToken: string | null = null) {
  usePatientAuthStore.setState({ accessToken, isRestoring: true, signOutReason: null })
  const router = routerAt(path)
  render(
    <StrictMode>
      <AppProviders router={router} />
    </StrictMode>,
  )
  return { router, user: userEvent.setup() }
}

/** Put a session in memory, as a successful code check or restore leaves it. */
export function signIn(accessToken = 'access-1') {
  usePatientAuthStore.setState({ accessToken, isRestoring: false, signOutReason: null })
}

export const isSignedIn = () => usePatientAuthStore.getState().accessToken !== null
