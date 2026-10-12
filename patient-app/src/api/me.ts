import { useQuery } from '@tanstack/react-query'
import type { PatientAccount, PendingPolicy } from '@/api/auth'
import { http } from '@/api/client'

/** One hospital record this account is linked to. */
export interface HospitalLink {
  hospital_id: string
  /** The hospital's public reference: where its page in the app lives. Null if it has none. */
  hospital_ref: string | null
  hospital_name: string
  linked_at: string
  /**
   * True when the phone on the hospital's record no longer matches the
   * account's verified phone: the link exists but gives no access.
   */
  suspended: boolean
}

/** `GET /me`. */
export interface PatientMe {
  account: PatientAccount
  links: HospitalLink[]
  pending_policies: PendingPolicy[]
}

export const meKeys = {
  me: ['patient', 'me'] as const,
}

/** The signed-in patient: account, linked hospitals, pending policies. */
export function useMe() {
  return useQuery({ queryKey: meKeys.me, queryFn: () => http.get<PatientMe>('/me') })
}
