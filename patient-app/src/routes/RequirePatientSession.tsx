import { Navigate, Outlet } from 'react-router-dom'
import { FullScreenLoader } from '@/components/FullScreenLoader'
import { usePatientAuthStore } from '@/store/patient-auth-store'

/**
 * Guard for every signed-in route. Without a session the patient goes to
 * `/login`. This only decides what to show — the server authorises every
 * request on its own.
 */
export function RequirePatientSession() {
  const isSignedIn = usePatientAuthStore((s) => s.accessToken !== null)
  const isRestoring = usePatientAuthStore((s) => s.isRestoring)

  if (isRestoring) return <FullScreenLoader label="Getting things ready…" />
  if (!isSignedIn) return <Navigate to="/login" replace />
  return <Outlet />
}
