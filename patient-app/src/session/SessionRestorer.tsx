import { useEffect, type ReactNode } from 'react'
import { refreshSession } from '@/api/client'
import { FullScreenLoader } from '@/components/FullScreenLoader'
import { usePatientAuthStore } from '@/store/patient-auth-store'

/**
 * Wraps the app and tries to restore a session on load.
 *
 * The access token lives in memory, so a reload starts with none. The refresh
 * token is an HttpOnly cookie the app cannot see, so the only way to learn
 * whether a session exists is to ask: `POST /auth/refresh`. If the server
 * answers with a token the patient is signed in again; any other answer leaves
 * them signed out and the route guard sends them to `/login`.
 *
 * Nothing is rendered until the answer is in, so a protected page is never
 * shown as "signed out" for a moment, and never shown at all without a session.
 */
export function SessionRestorer({ children }: { children: ReactNode }) {
  const isRestoring = usePatientAuthStore((s) => s.isRestoring)

  useEffect(() => {
    const { accessToken, setRestoring } = usePatientAuthStore.getState()
    if (accessToken !== null) {
      setRestoring(false)
      return
    }
    // `refreshSession` is single-flight, so React's double-invoked effect in
    // development still sends one request, and it stores the token itself. It
    // also waits its turn behind any other tab restoring at the same moment.
    void refreshSession().finally(() => setRestoring(false))
  }, [])

  if (isRestoring) return <FullScreenLoader label="Getting things ready…" />

  return <>{children}</>
}
