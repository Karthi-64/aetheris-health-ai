import { useEffect, type ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import { toAuthUser, useAuthStore } from '@/store/auth-store'
import { api } from '@/lib/api'
import { tokenStore } from '@/services/tokenStore'

/**
 * Wraps the app and attempts to restore a valid session on initial load.
 *
 * On mount, if there is a stored refresh token (from a previous login in the
 * same browser session), it calls POST /auth/refresh to obtain a fresh access
 * token. If successful, the user is re-authenticated transparently. If not,
 * the user sees the login screen.
 *
 * During the restoration attempt, a full-screen loading spinner is shown so
 * protected pages are never briefly rendered as "logged out" (task 2).
 *
 * If there is no stored refresh token (e.g. after a hard reload), the
 * restoration completes immediately and the user is sent to /login by the
 * RequireAuth route guard.
 */
export function SessionRestorer({ children }: { children: ReactNode }) {
  const setAuth = useAuthStore((s) => s.setAuth)
  const setRestoring = useAuthStore((s) => s.setRestoring)
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const isRestoring = useAuthStore((s) => s.isRestoring)

  useEffect(() => {
    let cancelled = false

    async function restore() {
      // If already authenticated (e.g. from a prior call), skip.
      if (useAuthStore.getState().isAuthenticated) {
        setRestoring(false)
        return
      }

      // No refresh token in memory → session cannot be restored.
      if (!tokenStore.getRefreshToken()) {
        if (!cancelled) setRestoring(false)
        return
      }

      try {
        const refreshToken = tokenStore.getRefreshToken()
        const { data } = await api.post('/auth/refresh', {
          refresh_token: refreshToken,
        })
        if (!cancelled && data?.data) {
          // POST /auth/refresh returns tokens but no user — fetch the real profile.
          const newAccessToken = data.data.access_token as string
          const newRefreshToken = data.data.refresh_token as string | null

          // Temporarily store tokens so the interceptor can attach the access
          // token on the /users/me request.
          tokenStore.setTokens(newAccessToken, newRefreshToken)

          try {
            const { data: meData } = await api.get('/users/me')
            if (!cancelled && meData?.data) {
              setAuth(toAuthUser(meData.data), newAccessToken, newRefreshToken)
            } else {
              // Fallback: we have tokens but no user profile. Clear and let
              // the route guard redirect to login.
              if (!cancelled) {
                tokenStore.clear()
                setRestoring(false)
              }
            }
          } catch {
            // /users/me failed — clear tokens so the user lands on login.
            if (!cancelled) {
              tokenStore.clear()
              setRestoring(false)
            }
          }
        }
      } catch {
        // Refresh failed — session is invalid. User will be redirected to
        // /login by the RequireAuth route guard.
      } finally {
        if (!cancelled) setRestoring(false)
      }
    }

    restore()
    return () => { cancelled = true }
  }, [setAuth, setRestoring])

  // Show a loading spinner while the restoration attempt is in flight.
  if (isRestoring && !isAuthenticated) {
    return (
      <div className="flex min-h-[100dvh] items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="text-secondary size-8 animate-spin" />
          <p className="font-body text-on-surface-variant text-sm">Restoring session…</p>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
