import { create } from 'zustand'
import { queryClient } from '@/lib/query-client'
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

/**
 * Why the user was signed out when they did not ask to be. The sign-in page
 * reads it to say what happened; an ordinary sign-out carries none.
 */
export type SignOutReason = 'session_ended' | 'password_changed'

/**
 * Drop every cached server response. The cache is keyed by resource, not by
 * user, so anything left in it would be shown to whoever signs in next in this
 * tab. In-flight requests are cancelled first so a late answer for the previous
 * user cannot land in the emptied cache.
 */
function clearServerCache() {
  void queryClient.cancelQueries()
  queryClient.clear()
}

interface AuthState {
  user: AuthUser | null
  /** Set by a forced sign-out, cleared by the next sign-in or sign-out. */
  signOutReason: SignOutReason | null
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
  /** End the session. Pass a reason only when the user did not ask to sign out. */
  logout: (reason?: SignOutReason) => void
  setRestoring: (v: boolean) => void
}

/**
 * Auth store. Intentionally NOT persisted: the access token lives in
 * `tokenStore` (memory), the refresh token is stored alongside it (returned
 * in the backend response body), and `isAuthenticated` is derived state — not
 * a flag a visitor can write to localStorage to become an admin (defects F2, F3).
 */
export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  signOutReason: null,
  isAuthenticated: false,
  // If there's no refresh token in memory there's nothing to restore, so
  // skip the spinner entirely (avoids a full-screen flash on public pages).
  isRestoring: tokenStore.getRefreshToken() !== null,
  setAuth: (user, accessToken, refreshToken?: string | null) => {
    // A different identity must never inherit the previous one's cached data.
    if (get().user?.id !== user.id) clearServerCache()
    tokenStore.setAccessToken(accessToken)
    if (refreshToken !== undefined) {
      tokenStore.setRefreshToken(refreshToken)
    }
    set({ user, isAuthenticated: true, signOutReason: null })
  },
  setUser: (update) =>
    set((state) => (state.user ? { user: { ...state.user, ...update } } : state)),
  logout: (reason) => {
    tokenStore.clear()
    clearServerCache()
    set({ user: null, isAuthenticated: false, signOutReason: reason ?? null })
  },
  setRestoring: (v) => set({ isRestoring: v }),
}))

export type { Permission }
