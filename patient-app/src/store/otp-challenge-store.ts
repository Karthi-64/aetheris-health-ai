import { create } from 'zustand'

/** The code request the patient is in the middle of answering. */
export interface OtpChallenge {
  /** Opaque id of the challenge; the code itself is never known to the app. */
  id: string
  /** The number the patient typed, as E.164 — needed to ask for another code. */
  phone: string
  /** When another code may be requested (epoch ms). */
  resendAt: number
}

interface OtpChallengeState {
  challenge: OtpChallenge | null
  setChallenge: (challenge: OtpChallenge) => void
  clearChallenge: () => void
}

/**
 * Carries the pending challenge from the phone page to the code page in
 * memory. It is deliberately not put in the URL (it would end up in history
 * and logs) nor in router state (which the browser keeps across reloads): a
 * reload forgets it and the patient simply asks for a new code.
 */
export const useOtpChallengeStore = create<OtpChallengeState>((set) => ({
  challenge: null,
  setChallenge: (challenge) => set({ challenge }),
  clearChallenge: () => set({ challenge: null }),
}))
