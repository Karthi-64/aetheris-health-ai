import { create } from 'zustand'
import { ROLE_KEY_BY_NAME, type Permission, type Role } from '@/lib/rbac'
import { tokenStore } from '@/services/tokenStore'

/** User profile as delivered by the backend (login / MFA / refresh + /users/me). */
export interface AuthUser {
  id: string
  name: string
  email: string
  /** First role name when the payload carries one — display only. */
  role?: Role
  /** Permission codes issued by the server (spec / defect F5). */
  permissions: string[]
}

/**
 * Accept the several user payloads the backend emits (login, /users/me) and
 * normalize them into the store's {@link AuthUser}. `permissions` always comes
 * from the server; `role` is resolved from the first role *display name*
 * ("Hospital Admin") into the local key (`hospital_admin`) — display only.
 */
export function toAuthUser(raw: Record<string, unknown>): AuthUser {
  const first = String(raw.first_name ?? '').trim()
  const last = String(raw.last_name ?? '').trim()
  const name = typeof raw.name === 'string' && raw.name.trim() ? raw.name : `${first} ${last}`.trim()

  const roles = Array.isArray(raw.roles) ? raw.roles : []
  const firstRole = typeof roles[0] === 'string' ? roles[0] : undefined

  return {
    id: String(raw.id ?? ''),
    name: name || 'User',
    email: String(raw.email ?? ''),
    role: firstRole ? ROLE_KEY_BY_NAME[firstRole] : undefined,
    permissions: Array.isArray(raw.permissions) ? (raw.permissions as string[]) : [],
  }
}

interface AuthState {
  user: AuthUser | null
  /** Derived, in-memory only. Never persisted, so it cannot be forged from
   *  devtools (defect F2). A reload drops it; the app re-auths via /auth/refresh. */
  isAuthenticated: boolean
  /** True while the app is attempting to restore a session on load. */
  isRestoring: boolean
  setAuth: (user: AuthUser, accessToken: string, refreshToken?: string | null) => void
  /**
   * Replace the cached profile without touching the session tokens — used when
   * the user edits their own name on the profile page and the sidebar has to
   * catch up. Permissions are never widened here: they stay whatever the
   * server issued for this session.
   */
  setUser: (update: Partial<Omit<AuthUser, 'permissions'>>) => void
  logout: () => void
  setRestoring: (v: boolean) => void
}

/**
 * Auth store. Intentionally NOT persisted: the access token lives in
 * `tokenStore` (memory), the refresh token is stored alongside it (returned
 * in the backend response body), and `isAuthenticated` is derived state — not
 * a flag a visitor can write to localStorage to become an admin (defects F2, F3).
 */
export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: false,
  // If there's no refresh token in memory there's nothing to restore, so
  // skip the spinner entirely (avoids a full-screen flash on public pages).
  isRestoring: tokenStore.getRefreshToken() !== null,
  setAuth: (user, accessToken, refreshToken?: string | null) => {
    tokenStore.setAccessToken(accessToken)
    if (refreshToken !== undefined) {
      tokenStore.setRefreshToken(refreshToken)
    }
    set({ user, isAuthenticated: true })
  },
  setUser: (update) =>
    set((state) => (state.user ? { user: { ...state.user, ...update } } : state)),
  logout: () => {
    tokenStore.clear()
    set({ user: null, isAuthenticated: false })
  },
  setRestoring: (v) => set({ isRestoring: v }),
}))

export type { Permission }
