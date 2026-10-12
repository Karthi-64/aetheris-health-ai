import { useAuthStore } from '@/store/auth-store'

/**
 * Sign a test user in with exactly these permission codes. Tests use the real
 * `usePermissions` hook, so what a screen shows is decided the same way it is
 * in the app.
 */
export function signIn(permissions: string[]) {
  useAuthStore.setState({
    user: { id: 'u1', name: 'Test User', email: 'user@example.com', permissions },
    isAuthenticated: true,
  })
}

export function signOut() {
  useAuthStore.setState({ user: null, isAuthenticated: false })
}
