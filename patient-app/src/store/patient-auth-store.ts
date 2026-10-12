import { create } from 'zustand'
import { queryClient } from '@/lib/query-client'

/**
 * Why the patient was signed out when they did not ask to be. The sign-in page
 * reads it to say what happened; an ordinary sign-out carries none.
 */
export type SignOutReason = 'session_ended'

/**
 * Drop every cached server response. The cache is keyed by resource, not by
 * account, so anything left in it would be shown to whoever signs in next in
 * this tab. In-flight requests are cancelled first so a late answer for the
 * previous patient cannot land in the emptied cache.
 */
function clearServerCache() {
  void queryClient.cancelQueries()
  queryClient.clear()
}

interface PatientAuthState {
  /**
   * The access token, in memory only. Never persisted, so a reload drops it
   * and `SessionRestorer` asks for a new one with the HttpOnly refresh cookie.
   */
  accessToken: string | null
  /** True until the refresh-on-load attempt has an answer. */
  isRestoring: boolean
  /** Set by a forced sign-out, cleared by the next sign-in or sign-out. */
  signOutReason: SignOutReason | null
  /** A patient has just signed in with a code. Whoever was here before is gone. */
  startSession: (accessToken: string) => void
  /** The same session was given a fresh token by a refresh. */
  setAccessToken: (accessToken: string) => void
  /** End the session. Pass a reason only when the patient did not ask to sign out. */
  endSession: (reason?: SignOutReason) => void
  setRestoring: (value: boolean) => void
}

/**
 * Patient auth store. Intentionally NOT persisted: there is no `persist`
 * middleware and nothing here touches web storage. Who the patient is (the
 * account and its masked phone) is server state and is read from `GET /me`;
 * this store only knows whether a session exists.
 */
export const usePatientAuthStore = create<PatientAuthState>((set) => ({
  accessToken: null,
  isRestoring: true,
  signOutReason: null,
  startSession: (accessToken) => {
    // A new identity must never inherit the previous one's cached data.
    clearServerCache()
    set({ accessToken, signOutReason: null })
  },
  setAccessToken: (accessToken) => set({ accessToken }),
  endSession: (reason) => {
    clearServerCache()
    set({ accessToken: null, signOutReason: reason ?? null })
  },
  setRestoring: (value) => set({ isRestoring: value }),
}))

/** True when an access token is held. The server remains the judge of it. */
export const useIsSignedIn = () => usePatientAuthStore((s) => s.accessToken !== null)
