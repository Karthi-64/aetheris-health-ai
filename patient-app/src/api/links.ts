import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toApiError, type ApiResponse } from '@atheris/api-core'
import { api, http } from '@/api/client'
import { hospitalKeys } from '@/api/hospitals'
import { meKeys } from '@/api/me'
import { CONSENT_POLICY_VERSION } from '@/lib/consent'

/**
 * Linking and self-registration. The phone is never sent: the server takes it
 * from the authenticated account. Nor is a patient id — the match happens on
 * the server, inside the one hospital named in the path.
 */

export interface LinkInput {
  /** The code the hospital gave the patient (its slug or id). */
  hospitalRef: string
  dateOfBirth: string
  /** Only sent once the server has asked for it. */
  mrn?: string
}

export interface RegisterInput {
  hospitalRef: string
  dateOfBirth: string
  firstName: string
  lastName: string
  gender: Gender
}

export const GENDERS = ['female', 'male', 'other', 'unspecified'] as const
export type Gender = (typeof GENDERS)[number]

/** `created` for a new link (201), `existing` when it was already there (200). */
export type LinkOutcome = 'created' | 'existing'

const hospitalPath = (hospitalRef: string, action: 'link' | 'register') =>
  `/hospitals/${encodeURIComponent(hospitalRef)}/${action}`

async function linkPatient(input: LinkInput): Promise<LinkOutcome> {
  try {
    // The status code is the answer here, so the envelope wrapper is bypassed.
    const res = await api.post<ApiResponse<unknown>>(hospitalPath(input.hospitalRef, 'link'), {
      date_of_birth: input.dateOfBirth,
      ...(input.mrn ? { mrn: input.mrn } : {}),
      consent_policy_version: CONSENT_POLICY_VERSION,
    })
    return res.status === 201 ? 'created' : 'existing'
  } catch (err) {
    throw toApiError(err)
  }
}

/**
 * A new link changes two things the app has already read: the account's links,
 * and the `linked` mark on that hospital wherever it is listed.
 */
function useRefreshLinks() {
  const queryClient = useQueryClient()
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: meKeys.me }),
      queryClient.invalidateQueries({ queryKey: hospitalKeys.all }),
    ])
}

/** Link this account to the patient's existing record at one hospital. */
export function useLinkPatient() {
  const refreshLinks = useRefreshLinks()
  return useMutation({ mutationFn: linkPatient, onSuccess: refreshLinks })
}

/** Register as a new patient at one hospital, when no record matched. */
export function useRegisterPatient() {
  const refreshLinks = useRefreshLinks()
  return useMutation({
    mutationFn: (input: RegisterInput) =>
      http.post<unknown>(hospitalPath(input.hospitalRef, 'register'), {
        first_name: input.firstName,
        last_name: input.lastName,
        date_of_birth: input.dateOfBirth,
        gender: input.gender,
        consent_policy_version: CONSENT_POLICY_VERSION,
      }),
    onSuccess: refreshLinks,
  })
}
