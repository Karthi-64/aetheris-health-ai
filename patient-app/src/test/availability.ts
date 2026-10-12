import type { AvailabilitySlot, DoctorAvailability } from '@/api/availability'
import { addDays, daysBetween, isIsoDate } from '@/lib/format'
import { fail, ok, type Handler, type Outcome, type Routes } from '@/test/fakeApi'

/**
 * The availability endpoint of one doctor, answering the way the backend
 * contract says it does, so a test can turn the weeks and see what a patient
 * would:
 *
 * - `GET /hospitals/{ref}/doctors/{ref}/availability` — `start_date` and
 *   `end_date` optional (`YYYY-MM-DD`); the default is today to a week ahead,
 *   cut at the horizon; a malformed date, an end before the start or a span
 *   over 14 days is a 422; a start before today or anything after the horizon
 *   is a 400 `BUSINESS_RULE_VIOLATION`.
 * - every date of the range is answered, in order, with its free slots as
 *   ISO-8601 instants carrying the hospital's own UTC offset.
 *
 * "Today" is fixed: nothing here, and nothing in the app, reads the clock.
 */
export interface FakeSchedule {
  /** The hospital's IANA zone. */
  timezone: string
  /** The hospital's date today. */
  today: string
  /** How far ahead can be booked, in days after today. */
  horizonDays: number
  minLeadMinutes: number
  /** The free slots of a day, as `HH:MM` starts on the hospital's clock. `weekday` is 0 for Sunday. */
  slotsOn: (date: string, weekday: number) => string[]
  slotMinutes: number
}

/**
 * Today is Wednesday 7 October 2026, with nothing left on it. Each weekday
 * then has its own count, so a test can tell the days apart by it:
 * Monday 4, Tuesday 1, Wednesday 0, Thursday 3, Friday 2, Saturday 1, Sunday 0.
 */
const BY_WEEKDAY: Record<number, string[]> = {
  0: [],
  1: ['09:00', '09:15', '09:30', '09:45'],
  2: ['09:00'],
  3: [],
  4: ['09:00', '09:15', '09:30'],
  5: ['10:00', '10:15'],
  6: ['11:00'],
}

export const defaultSchedule: FakeSchedule = {
  timezone: 'Asia/Kolkata',
  today: '2026-10-07',
  horizonDays: 30,
  minLeadMinutes: 60,
  slotsOn: (_date, weekday) => BY_WEEKDAY[weekday] ?? [],
  slotMinutes: 15,
}

/** `horizon_end` of a schedule. */
export const horizonOf = (schedule: Partial<FakeSchedule> = {}) => {
  const { today, horizonDays } = { ...defaultSchedule, ...schedule }
  return addDays(today, horizonDays)
}

/** The day of the week of a calendar date, 0 for Sunday. */
const weekdayOf = (date: string) => new Date(`${date}T12:00:00Z`).getUTCDay()

const pad = (n: number) => String(n).padStart(2, '0')

/** The zone's offset from UTC at an instant, in minutes. */
function offsetMinutes(timeZone: string, utcMs: number): number {
  const name =
    new Intl.DateTimeFormat('en-GB', { timeZone, timeZoneName: 'longOffset' })
      .formatToParts(utcMs)
      .find((part) => part.type === 'timeZoneName')?.value ?? 'GMT'
  const match = /GMT([+-])(\d{2}):(\d{2})/.exec(name)
  if (!match) return 0
  return (match[1] === '-' ? -1 : 1) * (Number(match[2]) * 60 + Number(match[3]))
}

/** An instant as ISO-8601 on the clock of `offset` minutes from UTC, e.g. `2026-10-07T09:30:00+05:30`. */
function isoAt(utcMs: number, offset: number): string {
  const local = new Date(utcMs + offset * 60_000)
  const sign = offset < 0 ? '-' : '+'
  const size = Math.abs(offset)
  return (
    `${local.getUTCFullYear()}-${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}` +
    `T${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}:${pad(local.getUTCSeconds())}` +
    `${sign}${pad(Math.floor(size / 60))}:${pad(size % 60)}`
  )
}

/** The instant of a wall-clock time on a date in a zone, written with that zone's offset. */
export function instantOf(date: string, time: string, timeZone: string): string {
  const [hours, minutes] = time.split(':').map(Number)
  const naive = Date.UTC(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10)), hours, minutes)
  // The offset at roughly that instant, then once more at the instant it gives: right across a DST change too.
  let offset = offsetMinutes(timeZone, naive)
  offset = offsetMinutes(timeZone, naive - offset * 60_000)
  return isoAt(naive - offset * 60_000, offset)
}

const minutesLater = (iso: string, minutes: number) => {
  const offset = iso.endsWith('Z') ? 0 : (iso.at(-6) === '-' ? -1 : 1) * (Number(iso.slice(-5, -3)) * 60 + Number(iso.slice(-2)))
  return isoAt(Date.parse(iso) + minutes * 60_000, offset)
}

/** The free slots of one day, as the endpoint writes them. */
export function slotsOf(date: string, schedule: Partial<FakeSchedule> = {}): AvailabilitySlot[] {
  const full = { ...defaultSchedule, ...schedule }
  return full.slotsOn(date, weekdayOf(date)).map((time) => {
    const start = instantOf(date, time, full.timezone)
    return { start, end: minutesLater(start, full.slotMinutes) }
  })
}

/** The whole answer for a range, exactly as the endpoint shapes it. */
export function availabilityOf(start: string, end: string, schedule: Partial<FakeSchedule> = {}): DoctorAvailability {
  const full = { ...defaultSchedule, ...schedule }
  const days = []
  for (let date = start; date <= end; date = addDays(date, 1)) days.push({ date, slots: slotsOf(date, full) })
  return {
    timezone: full.timezone,
    today: full.today,
    horizon_end: horizonOf(full),
    min_lead_minutes: full.minLeadMinutes,
    start_date: start,
    end_date: end,
    days,
  }
}

const earliest = (a: string, b: string) => (a < b ? a : b)

/** The endpoint over `schedule`. */
export function availabilityHandler(schedule: Partial<FakeSchedule> = {}): Handler {
  const full = { ...defaultSchedule, ...schedule }
  const horizon = horizonOf(full)
  return (config): Outcome => {
    const { start_date: startDate, end_date: endDate } = (config.params ?? {}) as { start_date?: unknown; end_date?: unknown }
    const invalid = (value: unknown) => value !== undefined && (typeof value !== 'string' || !isIsoDate(value))
    if (invalid(startDate) || invalid(endDate)) return fail(422, 'VALIDATION_ERROR', 'Validation failed.')
    const start = (startDate as string | undefined) ?? full.today
    const end = (endDate as string | undefined) ?? earliest(addDays(start, 6), horizon)
    if (end < start || daysBetween(start, end) >= 14) return fail(422, 'VALIDATION_ERROR', 'Validation failed.')
    if (start < full.today || start > horizon || end > horizon) {
      return fail(400, 'BUSINESS_RULE_VIOLATION', 'Outside the bookable window.')
    }
    return ok(availabilityOf(start, end, full))
  }
}

/** The route of one doctor's availability at one hospital. */
export function availabilityRoute(hospitalRef: string, doctorRef: string, schedule: Partial<FakeSchedule> = {}): Routes {
  return { [`GET /hospitals/${hospitalRef}/doctors/${doctorRef}/availability`]: availabilityHandler(schedule) }
}
