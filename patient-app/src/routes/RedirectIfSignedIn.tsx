import { Navigate, Outlet } from 'react-router-dom'
import { useIsSignedIn } from '@/store/patient-auth-store'

/** The sign-in pages are for visitors; a signed-in patient goes home. */
export function RedirectIfSignedIn() {
  return useIsSignedIn() ? <Navigate to="/" replace /> : <Outlet />
}
