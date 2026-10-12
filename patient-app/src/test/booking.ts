import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import type { PatientDoctor } from '@/api/doctors'
import type { PatientHospital } from '@/api/hospitals'
import { parseInstant } from '@/lib/format'
import { bodyOf, fail, headerOf, ok, type Outcome, type Routes } from '@/test/fakeApi'

/**
 * The booking endpoint of one doctor, answering the way the binding contract
 * of Task 32 says it does, and keeping every request it was sent:
 *
 * - `POST /hospitals/{ref}/doctors/{ref}/appointments`, header
 *   `Idempotency-Key` (`^[A-Za-z0-9_-]{16,64}$`), body
 *   `{ start, end, type?, reason? }` and nothing else — an unknown field, a
 *   missing or malformed one, or a bad key is a 422.
 * - `201` with the appointment when it is created; `200` with the SAME
 *   appointment when the same key and the same request arrive again; `409`
 *   "This request was already used for another booking." for the same key
 *   with another request; `409` "This time is no longer available." for a
 *   slot already booked under another key.
 * - `bookable`, when given, is asked about each slot: `false` is the `400`
 *   "This time cannot be booked."
 *
 * A test scripts anything else — a failure, a lost answer, a body that is not
 * an appointment — with `next`, one answer per request, ahead of the rules
 * above. Scripted answers book nothing, except `LOST`.
 */

export interface BookingRequest {
  /** The `Idempotency-Key` header, as sent. */
  key: string | undefined
  authorization: string | undefined
  body: unknown
  /** The exact text that left the browser. */
  rawBody: string
}

/**
 * In place of a scripted answer: the request is received and answered by the
 * contract's own rules — a booking is made — and the answer never arrives.
 */
export const LOST = 'lost' as const

export type ScriptedAnswer = Outcome | ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | typeof LOST

export interface FakeBooking {
  /** The route, to spread into `serve({...})`. */
  routes: Routes
  /** `"POST /hospitals/…/appointments"`. */
  route: string
  /** Every request received, in order — refused and scripted ones too. */
  requests: BookingRequest[]
  /** The appointments that exist. */
  appointments: Record<string, unknown>[]
  /** Answer the next requests with these, in order, instead of the contract's own answer. */
  next: (...answers: ScriptedAnswer[]) => void
}

export interface FakeBookingOptions {
  /** Is this slot one that can be booked? Default: every slot is. */
  bookable?: (start: string, end: string) => boolean
  /** Extra fields put on every appointment answered: what a response must not be trusted with. */
  smuggled?: Record<string, unknown>
}

const KEY = /^[A-Za-z0-9_-]{16,64}$/
const ALLOWED = new Set(['start', 'end', 'type', 'reason'])

export const SLOT_TAKEN = 'This time is no longer available.'
export const KEY_REUSED = 'This request was already used for another booking.'
export const NOT_BOOKABLE = 'This time cannot be booked.'
export const OWN_OVERLAP = 'You already have an appointment at this time.'
export const LIMIT_REACHED = 'You have reached the limit of upcoming appointments at this hospital.'
export const LINK_REQUIRED = 'Link your record at this hospital to continue.'

/** The contract's refusals, ready to script. */
export const refusals = {
  slotTaken: () => fail(409, 'RESOURCE_CONFLICT', SLOT_TAKEN),
  keyReused: () => fail(409, 'RESOURCE_CONFLICT', KEY_REUSED),
  notBookable: () => fail(400, 'BUSINESS_RULE_VIOLATION', NOT_BOOKABLE),
  ownOverlap: () => fail(400, 'BUSINESS_RULE_VIOLATION', OWN_OVERLAP),
  limitReached: () => fail(400, 'BUSINESS_RULE_VIOLATION', LIMIT_REACHED),
  linkRequired: () => fail(403, 'RECORD_LINK_REQUIRED', LINK_REQUIRED),
  consentRequired: () => fail(403, 'CONSENT_REQUIRED', 'Consent required.'),
  notFound: () => fail(404, 'RESOURCE_NOT_FOUND', 'Not Found'),
  invalid: () => fail(422, 'VALIDATION_ERROR', 'Validation failed.'),
}

const invalid = () => refusals.invalid()

/** The reference the n-th appointment gets: UUID-shaped, and different for every `n`. */
export const appointmentRef = (n: number) => `a0000000-0000-4000-8000-${String(n).padStart(12, '0')}`

export function bookingEndpoint(hospital: PatientHospital, doctor: PatientDoctor, options: FakeBookingOptions = {}): FakeBooking {
  const route = `POST /hospitals/${hospital.ref}/doctors/${doctor.ref}/appointments`
  const requests: BookingRequest[] = []
  const appointments: Record<string, unknown>[] = []
  const scripted: ScriptedAnswer[] = []
  /** What each key was used for, and what it was answered with. */
  const byKey = new Map<string, { fingerprint: string; appointment: Record<string, unknown> }>()

  /** The contract's own answer to one request. */
  const answerOf = (key: string | undefined, body: unknown): Outcome => {
    if (key === undefined || !KEY.test(key)) return invalid()
    if (typeof body !== 'object' || body === null || Array.isArray(body)) return invalid()
    const fields = body as Record<string, unknown>
    if (Object.keys(fields).some((name) => !ALLOWED.has(name))) return invalid()
    const { start, end, type = 'new', reason = null } = fields
    if (typeof start !== 'string' || typeof end !== 'string') return invalid()
    const startMs = parseInstant(start)
    const endMs = parseInstant(end)
    if (startMs === null || endMs === null) return invalid()
    if (type !== 'new' && type !== 'follow_up') return invalid()
    if (reason !== null && (typeof reason !== 'string' || [...reason].length > 500)) return invalid()

    const fingerprint = JSON.stringify([startMs, endMs, type, reason])
    const seen = byKey.get(key)
    if (seen) return seen.fingerprint === fingerprint ? ok(seen.appointment, 200) : refusals.keyReused()

    if (endMs <= startMs || options.bookable?.(start, end) === false) return refusals.notBookable()
    const overlaps = (each: Record<string, unknown>) =>
      (parseInstant(String(each.start)) ?? 0) < endMs && startMs < (parseInstant(String(each.end)) ?? 0)
    if (appointments.some(overlaps)) return refusals.slotTaken()

    const appointment = {
      ref: appointmentRef(appointments.length + 1),
      status: 'booked',
      type,
      start,
      end,
      timezone: hospital.timezone,
      hospital: { ref: hospital.ref, name: hospital.name },
      doctor: { ref: doctor.ref, name: doctor.name, specialization: doctor.specialization },
      reason,
      ...options.smuggled,
    }
    appointments.push(appointment)
    byKey.set(key, { fingerprint, appointment })
    return ok(appointment, 201)
  }

  const handler = async (config: InternalAxiosRequestConfig): Promise<Outcome> => {
    const key = headerOf(config, 'Idempotency-Key')
    const rawBody = typeof config.data === 'string' ? config.data : JSON.stringify(config.data ?? null)
    const body = bodyOf(config)
    requests.push({ key, authorization: headerOf(config, 'Authorization'), body, rawBody })

    const answer = scripted.shift()
    if (answer === LOST) {
      // The server did its part; the answer is what went missing.
      answerOf(key, body)
      throw new AxiosError('Network Error', 'ERR_NETWORK', config)
    }
    if (answer) return typeof answer === 'function' ? answer(config) : answer
    return answerOf(key, body)
  }

  return { routes: { [route]: handler }, route, requests, appointments, next: (...answers) => void scripted.push(...answers) }
}

/** An appointment as the endpoint answers it, for a test that scripts the answer itself. */
export const appointmentOf = (
  hospital: PatientHospital,
  doctor: PatientDoctor,
  start: string,
  end: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  ref: appointmentRef(1),
  status: 'booked',
  type: 'new',
  start,
  end,
  timezone: hospital.timezone,
  hospital: { ref: hospital.ref, name: hospital.name },
  doctor: { ref: doctor.ref, name: doctor.name, specialization: doctor.specialization },
  reason: null,
  ...overrides,
})
