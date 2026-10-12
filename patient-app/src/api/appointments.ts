import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '@atheris/api-core'
import { availabilityKeys } from '@/api/availability'
import { http } from '@/api/client'
import { isPublicRef } from '@/api/doctors'
import { isSendable as isHospitalRef } from '@/api/hospitals'
import { forgetAppointmentLists } from '@/api/myAppointments'
import { parseInstant } from '@/lib/format'

/**
 * Booking an appointment. The request names a hospital and a doctor by their
 * public references, in the path, and one slot by its two instants. It never
 * names a patient: the server takes the patient from the signed-in account's
 * own link at that hospital. There is no patient, hospital or doctor id in
 * the body, and no status — the server decides all of them.
 *
 * Every request carries an `Idempotency-Key`. The caller makes one per
 * booking and sends the same one on every retry, so a request that was
 * received but whose answer was lost cannot book twice.
 */

/** The longest reason the server takes; anything longer is a 422. */
export const REASON_MAX_LENGTH = 500

/** What the server accepts as an `Idempotency-Key`. */
const IDEMPOTENCY_KEY = /^[A-Za-z0-9_-]{16,64}$/

/** How long one attempt may take. A request that timed out is retried with the same key. */
const BOOKING_TIMEOUT_MS = 30_000

/** A reason's length as the server counts it: in characters, not UTF-16 units. */
export const reasonLength = (reason: string) => [...reason].length

export interface BookingInput {
  hospitalRef: string
  doctorRef: string
  /** The slot's two instants, exactly as the availability endpoint wrote them. */
  start: string
  end: string
  /** Already trimmed; empty or missing is not sent. */
  reason?: string
  /** One per booking, the same on every retry of it. */
  idempotencyKey: string
}

/** A booked appointment, as the Patient App may show it. */
export interface BookedAppointment {
  /** The appointment's public reference. */
  ref: string
  status: 'booked'
  start: string
  end: string
  /** The hospital's IANA zone. */
  timezone: string
  hospital: { name: string }
  doctor: { name: string; specialization: string }
}

/** The same answer the server gives for a reference it does not know. */
const notFound = () => new ApiError('Not found.', 'RESOURCE_NOT_FOUND', 404)

/** The same answer the server gives for a body or a header it cannot read. */
const invalid = () => new ApiError('Validation failed.', 'VALIDATION_ERROR', 422)

const badResponse = () => new ApiError('Response is not an appointment', 'bad_response')

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null

const isText = (value: unknown): value is string => typeof value === 'string' && value.trim() !== ''

/**
 * An appointment from a response, field by field. Only what the contract
 * names and the page shows is copied, so nothing else a response carries can
 * reach the screen. Every part of it is needed to say "booked" truthfully —
 * which appointment, in what state, when, where and with whom — so a response
 * missing any of them is not a confirmation at all.
 */
export function toBookedAppointment(value: unknown): BookedAppointment {
  if (!isRecord(value) || typeof value.ref !== 'string' || !isPublicRef(value.ref)) throw badResponse()
  // The one state a booking answers with. Any other is not something to announce as booked.
  if (value.status !== 'booked') throw badResponse()
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
    status: 'booked',
    start: value.start,
    end: value.end,
    timezone: value.timezone,
    hospital: { name: hospital.name },
    doctor: { name: doctor.name, specialization: typeof doctor.specialization === 'string' ? doctor.specialization : '' },
  }
}

async function bookAppointment(input: BookingInput): Promise<BookedAppointment> {
  const { hospitalRef, doctorRef, start, end, reason, idempotencyKey } = input
  // Nothing that the server would refuse outright is sent.
  if (!isHospitalRef(hospitalRef) || !isPublicRef(doctorRef)) throw notFound()
  const startMs = parseInstant(start)
  const endMs = parseInstant(end)
  if (startMs === null || endMs === null || endMs <= startMs) throw invalid()
  if (!IDEMPOTENCY_KEY.test(idempotencyKey)) throw invalid()
  if (reason !== undefined && reasonLength(reason) > REASON_MAX_LENGTH) throw invalid()

  const appointment = await http.post<unknown>(
    `/hospitals/${encodeURIComponent(hospitalRef)}/doctors/${encodeURIComponent(doctorRef)}/appointments`,
    { start, end, type: 'new', ...(reason ? { reason } : {}) },
    { headers: { 'Idempotency-Key': idempotencyKey }, timeout: BOOKING_TIMEOUT_MS },
  )
  return toBookedAppointment(appointment)
}

/**
 * Book one slot. Nothing is retried here: the page decides, and it retries
 * with the same key. Offline, the request fails at once instead of waiting to
 * be sent later, behind the patient's back.
 *
 * Whatever the answer, the doctor's availability as it was read is no longer
 * to be trusted: the slot is taken now, or was refused. Neither is the list of
 * the patient's own appointments: there may be one more in it.
 */
export function useBookAppointment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: bookAppointment,
    networkMode: 'always',
    retry: false,
    onSettled: (_appointment, _error, input) => {
      void queryClient.invalidateQueries({ queryKey: availabilityKeys.doctor(input.hospitalRef, input.doctorRef) })
      void forgetAppointmentLists(queryClient)
    },
  })
}

/** Every way a booking can fail, as the app tells them apart. */
export type BookingFailure =
  | 'slot_taken'
  | 'not_bookable'
  | 'own_overlap'
  | 'limit_reached'
  | 'link_required'
  | 'policies_pending'
  | 'not_found'
  | 'invalid'
  | 'offline'
  | 'server'
  | 'unconfirmed'

/** The two refusals a 400 tells apart only by its message. */
const OWN_OVERLAP_MESSAGE = 'You already have an appointment at this time.'
const LIMIT_REACHED_MESSAGE = 'You have reached the limit of upcoming appointments at this hospital.'

/**
 * Classify a failed booking by what really happened. The server's text is
 * compared, never shown.
 *
 * - An answer from the server is taken at its word, by status first: 409 is
 *   the slot gone (whichever message it carries); 400 is the slot not
 *   bookable, unless it is one of the two refusals about the patient's own
 *   appointments; 403 names the missing record link or a pending policy; 404
 *   is the doctor or hospital not there; 422 is a request that was not sound.
 * - A 2xx whose body is not an appointment is `unconfirmed`: the booking may
 *   well exist, and only asking again with the same key can tell.
 * - No answer at all — no network, a timeout — is `offline`.
 * - Anything else, a 5xx above all, is `server`.
 */
export function classifyBookingError(error: unknown): BookingFailure {
  if (!(error instanceof ApiError)) return 'server'
  const { status, code, message } = error
  if (status === undefined) {
    if (code === 'bad_response') return 'unconfirmed'
    if (typeof navigator !== 'undefined' && navigator.onLine === false) return 'offline'
    return code === 'network_error' ? 'offline' : 'server'
  }
  if (status === 409) return 'slot_taken'
  if (status === 400) {
    if (code === 'BUSINESS_RULE_VIOLATION' && message === OWN_OVERLAP_MESSAGE) return 'own_overlap'
    if (code === 'BUSINESS_RULE_VIOLATION' && message === LIMIT_REACHED_MESSAGE) return 'limit_reached'
    return 'not_bookable'
  }
  if (status === 403 && code === 'RECORD_LINK_REQUIRED') return 'link_required'
  if (status === 403 && code === 'CONSENT_REQUIRED') return 'policies_pending'
  if (status === 404) return 'not_found'
  if (status === 422) return 'invalid'
  return 'server'
}

/**
 * True when asking again, with the same key, could still end in a booking:
 * nothing was refused, the answer just did not arrive or could not be read.
 */
export const isRetriable = (failure: BookingFailure) =>
  failure === 'offline' || failure === 'server' || failure === 'unconfirmed' || failure === 'policies_pending'
