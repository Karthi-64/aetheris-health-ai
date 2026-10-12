import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { ApiError, type Paginated } from '@atheris/api-core'
import { http } from '@/api/client'
import { isPublicRef } from '@/api/doctors'
import { isSendable as isHospitalRef } from '@/api/hospitals'
import { parseInstant } from '@/lib/format'

/**
 * The signed-in patient's own appointments: the list, one appointment, and
 * cancelling one. No request here names a patient — the server takes the
 * patient from the session — and an appointment is named only by its public
 * reference, in the path.
 *
 * Whether an appointment can be cancelled is the SERVER's decision, carried in
 * `can_cancel`. Nothing here works it out from the status or from the clock.
 */

/** The states an appointment can be in. There are no others. */
export const APPOINTMENT_STATUSES = ['booked', 'checked_in', 'in_progress', 'completed', 'cancelled', 'no_show'] as const
export type AppointmentStatus = (typeof APPOINTMENT_STATUSES)[number]

/** The two halves of the list, as the server divides them. */
export type AppointmentScope = 'upcoming' | 'past'

/** The reasons a patient may give for cancelling. */
export const CANCEL_REASON_CODES = ['schedule_conflict', 'feeling_better', 'booked_by_mistake', 'other'] as const
export type CancelReasonCode = (typeof CANCEL_REASON_CODES)[number]

/** The limits of the endpoints: anything beyond them is a 422. */
export const APPOINTMENTS_PAGE_SIZE = 20
export const APPOINTMENTS_MAX_PAGE = 1000
export const CANCEL_TEXT_MAX_LENGTH = 200

/** How long one attempt to cancel may take. A request that timed out can simply be sent again. */
const CANCEL_TIMEOUT_MS = 30_000

/** The length of the details as the server counts it: in characters, not UTF-16 units. */
export const cancelTextLength = (text: string) => [...text].length

/** One appointment, as the Patient App may show it. */
export interface MyAppointment {
  /** The appointment's public reference. */
  ref: string
  status: AppointmentStatus
  start: string
  end: string
  /** The hospital's IANA zone. Every date and time is shown on this clock. */
  timezone: string
  /** `ref` is the hospital's public reference, or `null` when the response gave none that can be linked to. */
  hospital: { ref: string | null; name: string }
  /** `ref` is used only to refresh that doctor's availability; it is never shown. */
  doctor: { ref: string | null; name: string; specialization: string }
  /** The patient's own reason for the visit. */
  reason: string | null
  /** True only when the server said exactly `true`. */
  can_cancel: boolean
  /** The last instant the app may cancel it, or `null`. */
  cancel_until: string | null
}

export interface AppointmentsQuery {
  scope: AppointmentScope
  page: number
}

export interface CancelInput {
  ref: string
  reasonCode: CancelReasonCode
  /** Already trimmed; empty or missing is not sent. */
  reasonText?: string
}

export const myAppointmentKeys = {
  all: ['patient', 'appointments'] as const,
  /** Every page of both halves of the list. */
  lists: ['patient', 'appointments', 'list'] as const,
  list: ({ scope, page }: AppointmentsQuery) => ['patient', 'appointments', 'list', scope, page] as const,
  detail: (ref: string) => ['patient', 'appointments', 'detail', ref] as const,
}

/** The same answer the server gives for a reference it does not know. */
const notFound = () => new ApiError('Not found.', 'RESOURCE_NOT_FOUND', 404)

/** The same answer the server gives for a body it cannot read. */
const invalid = () => new ApiError('Validation failed.', 'VALIDATION_ERROR', 422)

const badResponse = (what = 'an appointment') => new ApiError(`Response is not ${what}`, 'bad_response')

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null

const isText = (value: unknown): value is string => typeof value === 'string' && value.trim() !== ''

const isStatus = (value: unknown): value is AppointmentStatus =>
  typeof value === 'string' && (APPOINTMENT_STATUSES as readonly string[]).includes(value)

const isReasonCode = (value: unknown): value is CancelReasonCode =>
  typeof value === 'string' && (CANCEL_REASON_CODES as readonly string[]).includes(value)

/**
 * An appointment from a response, field by field. Only what the contract
 * names and a page uses is copied, so nothing else a response carries — a
 * patient id, staff notes, who cancelled and why — can reach the screen.
 *
 * What an appointment cannot be shown truthfully without is required: which
 * one, its state, when, on whose clock, where and with whom. A response
 * missing any of these is not an appointment at all.
 *
 * `can_cancel` is true only for the boolean `true`: `"true"`, `1`, a missing
 * field and anything else all mean the app offers no cancellation.
 */
export function toMyAppointment(value: unknown): MyAppointment {
  if (!isRecord(value) || typeof value.ref !== 'string' || !isPublicRef(value.ref)) throw badResponse()
  if (!isStatus(value.status)) throw badResponse()
  if (typeof value.start !== 'string' || typeof value.end !== 'string') throw badResponse()
  const start = parseInstant(value.start)
  const end = parseInstant(value.end)
  if (start === null || end === null || end <= start) throw badResponse()
  if (!isText(value.timezone)) throw badResponse()
  const { hospital, doctor } = value
  if (!isRecord(hospital) || !isText(hospital.name)) throw badResponse()
  if (!isRecord(doctor) || !isText(doctor.name)) throw badResponse()
  return {
    ref: value.ref,
    status: value.status,
    start: value.start,
    end: value.end,
    timezone: value.timezone,
    hospital: {
      ref: typeof hospital.ref === 'string' && isHospitalRef(hospital.ref) ? hospital.ref : null,
      name: hospital.name,
    },
    doctor: {
      ref: typeof doctor.ref === 'string' && isPublicRef(doctor.ref) ? doctor.ref : null,
      name: doctor.name,
      specialization: typeof doctor.specialization === 'string' ? doctor.specialization : '',
    },
    reason: isText(value.reason) ? value.reason : null,
    can_cancel: value.can_cancel === true,
    cancel_until: typeof value.cancel_until === 'string' && parseInstant(value.cancel_until) !== null ? value.cancel_until : null,
  }
}

const sameRef = (a: string, b: string) => a.toLowerCase() === b.toLowerCase()

const isCount = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 0

async function fetchMyAppointments({ scope, page }: AppointmentsQuery): Promise<Paginated<MyAppointment>> {
  const result = await http.getPaginated<unknown>('/appointments', {
    params: { scope, page, page_size: APPOINTMENTS_PAGE_SIZE },
  })
  // A page that is not a list can be neither shown nor counted: it is a failed read.
  if (!Array.isArray(result.items)) throw badResponse('a list')
  const { pagination } = result
  if (!isCount(pagination.page) || !isCount(pagination.pageSize) || !isCount(pagination.total) || !isCount(pagination.totalPages)) {
    throw badResponse('a list')
  }
  // One entry that is not an appointment fails the whole read: a list that is
  // silently one short would hide an appointment the patient has.
  return { items: result.items.map(toMyAppointment), pagination }
}

/** One page of the patient's upcoming or past appointments, in the server's order. */
export function useMyAppointments({ scope, page }: AppointmentsQuery) {
  return useQuery({
    queryKey: myAppointmentKeys.list({ scope, page }),
    queryFn: () => fetchMyAppointments({ scope, page }),
  })
}

async function fetchMyAppointment(ref: string): Promise<MyAppointment> {
  // A reference that is not one is never sent; it is the server's own 404.
  if (!isPublicRef(ref)) throw notFound()
  const appointment = toMyAppointment(await http.get<unknown>(`/appointments/${encodeURIComponent(ref)}`))
  // An answer about another appointment is not an answer about this one.
  if (!sameRef(appointment.ref, ref)) throw badResponse()
  return appointment
}

/** One of the patient's appointments. Unknown, someone else's and malformed are the same 404. */
export function useMyAppointment(ref: string) {
  return useQuery({
    queryKey: myAppointmentKeys.detail(ref),
    queryFn: () => fetchMyAppointment(ref),
  })
}

async function cancelAppointment({ ref, reasonCode, reasonText }: CancelInput): Promise<MyAppointment> {
  // Nothing that the server would refuse outright is sent.
  if (!isPublicRef(ref)) throw notFound()
  if (!isReasonCode(reasonCode)) throw invalid()
  if (reasonText !== undefined && cancelTextLength(reasonText) > CANCEL_TEXT_MAX_LENGTH) throw invalid()

  const answer = await http.post<unknown>(
    `/appointments/${encodeURIComponent(ref)}/cancel`,
    { reason_code: reasonCode, ...(reasonText ? { reason_text: reasonText } : {}) },
    { timeout: CANCEL_TIMEOUT_MS },
  )
  const appointment = toMyAppointment(answer)
  // "Cancelled" is said only for this appointment, in exactly that state.
  if (!sameRef(appointment.ref, ref) || appointment.status !== 'cancelled') throw badResponse()
  return appointment
}

/**
 * What is known about the patient's appointments is out of date: the lists
 * are emptied rather than merely marked, so the next look at them waits for
 * the server instead of showing — and offering to cancel — what was there
 * before.
 */
export function forgetAppointmentLists(queryClient: QueryClient) {
  return queryClient.resetQueries({ queryKey: myAppointmentKeys.lists })
}

/** Every window of availability read for one doctor (or for all, when the doctor is not known). */
function invalidateAvailability(queryClient: QueryClient, doctorRef: string | null) {
  return queryClient.invalidateQueries({
    predicate: ({ queryKey }) =>
      queryKey[0] === 'patient' && queryKey[1] === 'availability' && (doctorRef === null || queryKey[3] === doctorRef),
  })
}

/**
 * Cancel one appointment. Nothing is retried here: the page decides, and a
 * repeat is safe — the server answers a cancelled appointment with the same
 * cancelled appointment. Offline, the request fails at once instead of
 * waiting to be sent later, behind the patient's back.
 *
 * - `200` with this appointment, cancelled: the answer itself becomes what is
 *   known about it; the lists and the doctor's availability are read again.
 * - `409` or `400`: the appointment is not what the page thought it was. What
 *   was known is dropped and it is read again from the server, so nothing
 *   stale — a cancel button above all — stays on screen.
 * - Anything else changes nothing that is known.
 */
export function useCancelAppointment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: cancelAppointment,
    networkMode: 'always',
    retry: false,
    onSuccess: (appointment, { ref }) => {
      queryClient.setQueryData(myAppointmentKeys.detail(ref), appointment)
      void forgetAppointmentLists(queryClient)
      void invalidateAvailability(queryClient, appointment.doctor.ref)
    },
    onError: (error, { ref }) => {
      const failure = classifyCancelError(error)
      if (failure === 'no_longer_cancellable' || failure === 'too_late') {
        const known = queryClient.getQueryData<MyAppointment>(myAppointmentKeys.detail(ref))
        void queryClient.resetQueries({ queryKey: myAppointmentKeys.detail(ref), exact: true })
        void forgetAppointmentLists(queryClient)
        void invalidateAvailability(queryClient, known?.doctor.ref ?? null)
      } else if (failure === 'not_found') {
        // Not shown again from memory; the next look asks the server.
        void queryClient.invalidateQueries({ queryKey: myAppointmentKeys.detail(ref), exact: true, refetchType: 'none' })
        void forgetAppointmentLists(queryClient)
      }
    },
  })
}

/** Every way a cancellation can fail, as the app tells them apart. */
export type CancelFailure =
  | 'no_longer_cancellable'
  | 'too_late'
  | 'not_found'
  | 'invalid'
  | 'offline'
  | 'server'
  | 'unconfirmed'

/**
 * Classify a failed cancellation by what really happened. The server's text
 * is never read, and never shown.
 *
 * - An answer from the server is taken at its word, by status: 409 is an
 *   appointment that can no longer be cancelled (it is checked in, under way,
 *   done or missed); 400 is the hospital's cut-off having passed; 404 is an
 *   appointment that is not there, or not the patient's; 422 is a request
 *   that was not sound.
 * - A 2xx whose body is not this appointment, cancelled, is `unconfirmed`:
 *   it may well be cancelled, and only asking again can tell.
 * - No answer at all — no network, a timeout — is `offline`.
 * - Anything else, a 5xx above all, is `server`.
 */
export function classifyCancelError(error: unknown): CancelFailure {
  if (!(error instanceof ApiError)) return 'server'
  const { status, code } = error
  if (status === undefined) {
    if (code === 'bad_response') return 'unconfirmed'
    if (typeof navigator !== 'undefined' && navigator.onLine === false) return 'offline'
    return code === 'network_error' ? 'offline' : 'server'
  }
  if (status === 409) return 'no_longer_cancellable'
  if (status === 400) return 'too_late'
  if (status === 404) return 'not_found'
  if (status === 422) return 'invalid'
  return 'server'
}

/**
 * True when asking again could still end in a cancellation: nothing was
 * refused, the answer just did not arrive or could not be read.
 */
export const isCancelRetriable = (failure: CancelFailure) =>
  failure === 'offline' || failure === 'server' || failure === 'unconfirmed'
