import { useMutation } from '@tanstack/react-query'
import { ApiError } from '@atheris/api-core'
import { CSRF_HEADER, http } from '@/api/client'
import { useOtpChallengeStore } from '@/store/otp-challenge-store'
import { usePatientAuthStore } from '@/store/patient-auth-store'

/** `POST /auth/otp/request` — identical whether or not the number has an account. */
export interface OtpRequested {
  challenge_id: string
  /** Seconds the code stays valid. */
  expires_in: number
  /** Seconds before another code may be requested. */
  resend_after: number
}

export interface PatientAccount {
  id: string
  /** Masked by the server; the app never holds the full number of an account. */
  phone_masked: string
  status: 'active' | 'suspended' | 'closed'
}

/** A policy the patient still has to accept. None is required today. */
export interface PendingPolicy {
  purpose: string
  version: string
}

/** `POST /auth/otp/verify`. The refresh token is a cookie and never appears here. */
export interface VerifiedSession {
  access_token: string
  expires_in: number
  account: PatientAccount
  pending_policies: PendingPolicy[]
}

/** Ask for a one-time code to be sent to `phone` (E.164). */
export function useRequestOtp() {
  return useMutation({
    mutationFn: (phone: string) => http.post<OtpRequested>('/auth/otp/request', { phone }),
  })
}

/** Check a code. On success the session starts; the token goes to memory only. */
export function useVerifyOtp() {
  return useMutation({
    mutationFn: (input: { challengeId: string; code: string }) =>
      http.post<VerifiedSession>('/auth/otp/verify', {
        challenge_id: input.challengeId,
        code: input.code,
      }),
    onSuccess: (session) => {
      // The challenge is spent; forget it before the page that used it goes away.
      useOtpChallengeStore.getState().clearChallenge()
      usePatientAuthStore.getState().startSession(session.access_token)
    },
  })
}

/**
 * Sign out: the server revokes the refresh token and clears the cookie, then
 * the in-memory session is dropped. If the server cannot be reached the
 * session is NOT dropped locally — the cookie would still be valid and a
 * reload would sign the patient back in, so they are told it did not work
 * instead. A 401 means there was no session left to end.
 */
export function useSignOut() {
  return useMutation({
    mutationFn: async () => {
      try {
        await http.post<void>('/auth/logout', undefined, { headers: CSRF_HEADER })
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 401)) throw err
      }
    },
    onSuccess: () => usePatientAuthStore.getState().endSession(),
  })
}
