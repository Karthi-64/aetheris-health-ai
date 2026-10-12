import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { parseInstant } from '@/lib/format'
import { LOST, type ScriptedAnswer } from '@/test/booking'
import { bodyOf, fail, headerOf, ok, okPage, type Handler, type Outcome, type Routes } from '@/test/fakeApi'
import { doctorRef } from '@/test/fixtures'

/**
 * The patient's own appointment endpoints over a set of appointments,
 * answering the way the binding contract of Task 33 says they do, and keeping
 * every cancellation they were sent:
 *
 * - `GET /appointments?scope=upcoming|past&page=&page_size=` — `scope`
 *   defaults to `upcoming`; anything else, a `page` outside 1..1000 or a
 *   `page_size` outside 1..50 is a 422. `upcoming` is booked / checked in / in
 *   progress AND not yet ended, soonest first; `past` is everything else,
 *   latest first.
 * - `GET /appointments/{ref}` — the appointment, or the one 404.
 * - `POST /appointments/{ref}/cancel` — body `{ reason_code, reason_text? }`
 *   and nothing else (an unknown field, a code that is not one of the four, or
 *   text over 200 characters is a 422). `200` with the appointment, now
 *   cancelled; `200` with the same appointment — and no new history — when it
 *   already was; `409` when it is checked in, in progress, completed or a
 *   no-show; `400` once the cut-off (120 minutes before the start) has passed.
 *
 * Every appointment answered carries `can_cancel` and `cancel_until`, worked
 * out here from its state and from `now` — which is fixed: nothing here, and
 * nothing in the app, reads the clock.
 *
 * A test scripts anything else — a failure, a lost answer, a body that is not
 * an appointment — with `next`, one answer per cancellation, ahead of the
 * rules above. Scripted answers cancel nothing, except `LOST`.
 */

export type WireAppointment = Record<string, unknown>

/** Wednesday 7 October 2026, nine in the morning in India: the same "today" the availability fake has. */
export const NOW = '2026-10-07T09:00:00+05:30'

export const NO_LONGER_CANCELLABLE = 'This appointment can no longer be cancelled.'
export const TOO_LATE = 'It is too late to cancel this appointment in the app. Please contact the hospital.'

/** The contract's refusals of a cancellation, ready to script. */
export const cancelRefusals = {
  noLongerCancellable: () => fail(409, 'RESOURCE_CONFLICT', NO_LONGER_CANCELLABLE),
  tooLate: () => fail(400, 'BUSINESS_RULE_VIOLATION', TOO_LATE),
  notFound: () => fail(404, 'RESOURCE_NOT_FOUND', 'Not Found'),
  invalid: () => fail(422, 'VALIDATION_ERROR', 'Validation failed.'),
}

/** The reference the n-th fixture appointment has: UUID-shaped, and different for every `n`. */
export const myAppointmentRef = (n: number) => `e0000000-0000-4000-8000-${String(n).padStart(12, '0')}`

/**
 * An appointment as the server keeps it: everything the contract's object has
 * except `can_cancel` and `cancel_until`, which the endpoints work out. By
 * default it is booked with Asha Menon at City Care for Monday 12 October
 * 2026, 10:00 to 10:15 in India — five days after {@link NOW}.
 */
export const anAppointment = (overrides: WireAppointment = {}): WireAppointment => ({
  ref: myAppointmentRef(1),
  status: 'booked',
  type: 'new',
  start: '2026-10-12T10:00:00+05:30',
  end: '2026-10-12T10:15:00+05:30',
  timezone: 'Asia/Kolkata',
  hospital: { ref: 'city-care', name: 'City Care' },
  doctor: { ref: doctorRef(1), name: 'Asha Menon', specialization: 'Cardiology' },
  reason: null,
  ...overrides,
})

/** `count` booked appointments on consecutive days from 12 October 2026, with "Doctor 01", "Doctor 02", … */
export const manyAppointments = (count: number, overrides: WireAppointment = {}): WireAppointment[] =>
  Array.from({ length: count }, (_, index) => {
    const number = String(index + 1).padStart(2, '0')
    const day = new Date(Date.UTC(2026, 9, 12 + index)).toISOString().slice(0, 10)
    return anAppointment({
      ref: myAppointmentRef(100 + index),
      start: `${day}T10:00:00+05:30`,
      end: `${day}T10:15:00+05:30`,
      doctor: { ref: doctorRef(100 + index), name: `Doctor ${number}`, specialization: 'General Medicine' },
      ...overrides,
    })
  })

export interface CancelRequest {
  /** The appointment the path named. */
  ref: string
  authorization: string | undefined
  body: unknown
  /** The exact text that left the browser. */
  rawBody: string
}

export interface FakeMyAppointmentsOptions {
  /** The instant it is, for "not yet ended" and for the cut-off. Default {@link NOW}. */
  now?: string
  /** How long before the start the app may still cancel. Default 120 minutes. */
  cutoffMinutes?: number
  /** References to answer for besides those of the appointments given: ones booked later, or never. */
  refs?: string[]
  /** Applied to every appointment answered: what a response must not be trusted with, or is missing. */
  shape?: (appointment: WireAppointment) => unknown
}

export interface FakeMyAppointments {
  /** The routes, to spread into `serve({...})`. */
  routes: Routes
  /** The appointments that exist: the very array given, so one booked later through another fake is seen. */
  appointments: WireAppointment[]
  /** Every cancellation received, in order — refused and scripted ones too. */
  cancelRequests: CancelRequest[]
  /** The history the server wrote: one entry per appointment actually cancelled. */
  cancellations: { ref: string; reason_code: unknown; reason_text: unknown }[]
  /** Answer the next cancellations with these, in order, instead of the contract's own answer. */
  next: (...answers: ScriptedAnswer[]) => void
  /** An appointment as the endpoints answer it now, with `can_cancel` and `cancel_until`. */
  wireOf: (ref: string) => WireAppointment
  /** What staff did since the page loaded: change the appointment behind the app's back. */
  change: (ref: string, changes: WireAppointment) => void
}

export const MY_APPOINTMENTS = 'GET /appointments'
export const myAppointmentRoute = (ref: string) => `GET /appointments/${ref}`
export const cancelRoute = (ref: string) => `POST /appointments/${ref}/cancel`

const REASON_CODES = new Set(['schedule_conflict', 'feeling_better', 'booked_by_mistake', 'other'])
const ALLOWED = new Set(['reason_code', 'reason_text'])
const ACTIVE = new Set(['booked', 'checked_in', 'in_progress'])
const SCOPES = new Set(['upcoming', 'past'])

const msOf = (iso: unknown) => parseInstant(String(iso)) ?? 0

/** `iso` moved back by `minutes`, written with the same offset as `iso`. */
function before(iso: string, minutes: number): string {
  const offset = /(Z|[+-]\d{2}:\d{2})$/.exec(iso)?.[1] ?? 'Z'
  const offsetMinutes = offset === 'Z' ? 0 : (offset[0] === '-' ? -1 : 1) * (Number(offset.slice(1, 3)) * 60 + Number(offset.slice(4, 6)))
  const local = new Date(msOf(iso) + (offsetMinutes - minutes) * 60_000)
  return `${local.toISOString().slice(0, 19)}${offset}`
}

const isWholeIn = (value: unknown, min: number, max: number) =>
  typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max

export function myAppointmentsEndpoints(
  appointments: WireAppointment[] = [],
  options: FakeMyAppointmentsOptions = {},
): FakeMyAppointments {
  const { now = NOW, cutoffMinutes = 120, refs = [], shape = (appointment) => appointment } = options
  const nowMs = msOf(now)
  const cancelRequests: CancelRequest[] = []
  const cancellations: FakeMyAppointments['cancellations'] = []
  const scripted: ScriptedAnswer[] = []

  const find = (ref: string) => appointments.find((each) => each.ref === ref)

  /** The contract's object: the record, and the two fields only the server can work out. */
  const wire = (record: WireAppointment): WireAppointment => {
    const cancelUntil = record.status === 'booked' ? before(String(record.start), cutoffMinutes) : null
    return {
      ...record,
      can_cancel: cancelUntil !== null && nowMs < msOf(cancelUntil),
      cancel_until: cancelUntil,
    }
  }

  const isUpcoming = (record: WireAppointment) => ACTIVE.has(String(record.status)) && msOf(record.end) > nowMs

  const list: Handler = (config) => {
    const { scope = 'upcoming', page = 1, page_size: pageSize = 20 } = (config.params ?? {}) as Record<string, unknown>
    if (!SCOPES.has(String(scope)) || !isWholeIn(page, 1, 1000) || !isWholeIn(pageSize, 1, 50)) return cancelRefusals.invalid()
    const [at, size] = [page as number, pageSize as number]
    const matches =
      scope === 'upcoming'
        ? appointments.filter(isUpcoming).sort((a, b) => msOf(a.start) - msOf(b.start))
        : appointments.filter((each) => !isUpcoming(each)).sort((a, b) => msOf(b.start) - msOf(a.start))
    return okPage(matches.slice((at - 1) * size, at * size).map((each) => shape(wire(each))), at, size, matches.length)
  }

  const detail =
    (ref: string): Handler =>
    () => {
      const record = find(ref)
      return record ? ok(shape(wire(record))) : cancelRefusals.notFound()
    }

  /** The contract's own answer to one cancellation. */
  const answerOf = (ref: string, body: unknown): Outcome => {
    const record = find(ref)
    if (!record) return cancelRefusals.notFound()
    if (typeof body !== 'object' || body === null || Array.isArray(body)) return cancelRefusals.invalid()
    const fields = body as Record<string, unknown>
    if (Object.keys(fields).some((name) => !ALLOWED.has(name))) return cancelRefusals.invalid()
    const { reason_code: code, reason_text: text = null } = fields
    if (typeof code !== 'string' || !REASON_CODES.has(code)) return cancelRefusals.invalid()
    if (text !== null && (typeof text !== 'string' || [...text].length > 200)) return cancelRefusals.invalid()

    // Idempotent: cancelled already is the same answer, and no new history.
    if (record.status === 'cancelled') return ok(shape(wire(record)))
    if (record.status !== 'booked') return cancelRefusals.noLongerCancellable()
    if (nowMs >= msOf(before(String(record.start), cutoffMinutes))) return cancelRefusals.tooLate()

    record.status = 'cancelled'
    cancellations.push({ ref, reason_code: code, reason_text: text })
    return ok(shape(wire(record)))
  }

  const cancel =
    (ref: string): Handler =>
    async (config: InternalAxiosRequestConfig) => {
      const rawBody = typeof config.data === 'string' ? config.data : JSON.stringify(config.data ?? null)
      const body = bodyOf(config)
      cancelRequests.push({ ref, authorization: headerOf(config, 'Authorization'), body, rawBody })

      const answer = scripted.shift()
      if (answer === LOST) {
        // The server did its part; the answer is what went missing.
        answerOf(ref, body)
        throw new AxiosError('Network Error', 'ERR_NETWORK', config)
      }
      if (answer) return typeof answer === 'function' ? answer(config) : answer
      return answerOf(ref, body)
    }

  const known = [...new Set([...appointments.map((each) => String(each.ref)), ...refs])]

  return {
    routes: {
      [MY_APPOINTMENTS]: list,
      ...Object.fromEntries(known.flatMap((ref) => [[myAppointmentRoute(ref), detail(ref)], [cancelRoute(ref), cancel(ref)]])),
    },
    appointments,
    cancelRequests,
    cancellations,
    next: (...answers) => void scripted.push(...answers),
    wireOf: (ref) => {
      const record = find(ref)
      if (!record) throw new Error(`No appointment ${ref}`)
      return wire(record)
    },
    change: (ref, changes) => {
      const record = find(ref)
      if (!record) throw new Error(`No appointment ${ref}`)
      Object.assign(record, changes)
    },
  }
}
