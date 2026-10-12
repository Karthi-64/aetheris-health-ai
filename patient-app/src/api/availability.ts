import { useQuery } from '@tanstack/react-query'
import { ApiError } from '@atheris/api-core'
import { http } from '@/api/client'
import { isPublicRef } from '@/api/doctors'
import { isSendable as isHospitalRef } from '@/api/hospitals'
import { daysBetween, isIsoDate as isCalendarDate, parseInstant } from '@/lib/format'

/**
 * A doctor's free slots at one hospital, over a range of the hospital's days.
 * This is reference data: the same for every signed-in patient. Nothing here
 * sends an account or a patient, and nothing here reserves anything — seeing
 * a slot is not holding it.
 *
 * What a slot has is exactly `start` and `end`. There is no id, no status, no
 * appointment, no fee, no patient and no doctor in the contract, and a
 * response that carries one anyway has it dropped here, before any page sees
 * it. The only slots in the answer are the bookable ones; the server sends
 * nothing about the rest of the day.
 */

/** One free slot, as two instants with the hospital's UTC offset. */
export interface AvailabilitySlot {
  start: string
  end: string
}

/** One of the hospital's days: its calendar date and its free slots, in order. */
export interface AvailabilityDay {
  date: string
  slots: AvailabilitySlot[]
}

export interface DoctorAvailability {
  /** The hospital's IANA zone. Every time is shown on this clock. */
  timezone: string
  /** The hospital's date today: the only "today" the app knows. */
  today: string
  /** The last date that can be booked. */
  horizon_end: string
  min_lead_minutes: number
  /** The range the answer covers, inclusive. */
  start_date: string
  end_date: string
  /** Every date of the range, in order, each with its free slots. */
  days: AvailabilityDay[]
}

/**
 * What `GET /hospitals/{ref}/doctors/{ref}/availability` is asked for. Empty
 * dates mean "the server's own default": today and the days after it.
 */
export interface AvailabilityQuery {
  hospitalRef: string
  doctorRef: string
  startDate: string
  endDate: string
}

/** The widest range one request may cover, in days inclusive: anything wider is a 422. */
export const AVAILABILITY_MAX_SPAN_DAYS = 14

export const availabilityKeys = {
  /** Every window of one doctor's availability: what "Go to today" discards. */
  doctor: (hospitalRef: string, doctorRef: string) => ['patient', 'availability', hospitalRef, doctorRef] as const,
  window: (query: AvailabilityQuery) =>
    [...availabilityKeys.doctor(query.hospitalRef, query.doctorRef), query.startDate, query.endDate] as const,
}

/** The same answer the server gives for a reference it does not know. */
const notFound = () => new ApiError('Not found.', 'RESOURCE_NOT_FOUND', 404)

const badResponse = () => new ApiError('Response is not availability', 'bad_response')

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null

/**
 * A range the server would take: two real dates, in order, no wider than the
 * limit. Anything else is not sent — the page is shown the default range
 * instead of an error the address bar asked for.
 */
export function isSendableRange(startDate: string, endDate: string): boolean {
  return (
    isCalendarDate(startDate) &&
    isCalendarDate(endDate) &&
    endDate >= startDate &&
    daysBetween(startDate, endDate) < AVAILABILITY_MAX_SPAN_DAYS
  )
}

/** A slot from a response: two instants, the second after the first — or nothing. */
function toSlot(value: unknown): AvailabilitySlot | null {
  if (!isRecord(value) || typeof value.start !== 'string' || typeof value.end !== 'string') return null
  const start = parseInstant(value.start)
  const end = parseInstant(value.end)
  if (start === null || end === null || end <= start) return null
  return { start: value.start, end: value.end }
}

/**
 * Availability from a response, field by field. Only what the contract names
 * is copied, so nothing else a response carries can reach the screen. A slot
 * that is not two instants in order is dropped; a day that has no date is
 * dropped; a second entry for the same date is dropped. What the page cannot
 * work without — the zone, today, the horizon, the days — is a failed read.
 */
export function toAvailability(value: unknown): DoctorAvailability {
  if (!isRecord(value) || !Array.isArray(value.days)) throw badResponse()
  if (typeof value.timezone !== 'string' || value.timezone.trim() === '') throw badResponse()
  if (typeof value.today !== 'string' || !isCalendarDate(value.today)) throw badResponse()
  if (typeof value.horizon_end !== 'string' || !isCalendarDate(value.horizon_end)) throw badResponse()

  const seen = new Set<string>()
  const days = value.days.flatMap((entry: unknown): AvailabilityDay[] => {
    if (!isRecord(entry) || typeof entry.date !== 'string' || !isCalendarDate(entry.date) || seen.has(entry.date)) return []
    seen.add(entry.date)
    const slots = (Array.isArray(entry.slots) ? entry.slots : []).flatMap((slot: unknown) => {
      const kept = toSlot(slot)
      return kept ? [kept] : []
    })
    return [{ date: entry.date, slots }]
  })

  const dateOr = (field: unknown, fallback: string) => (typeof field === 'string' && isCalendarDate(field) ? field : fallback)
  return {
    timezone: value.timezone,
    today: value.today,
    horizon_end: value.horizon_end,
    min_lead_minutes:
      typeof value.min_lead_minutes === 'number' && Number.isFinite(value.min_lead_minutes) && value.min_lead_minutes >= 0
        ? value.min_lead_minutes
        : 0,
    start_date: dateOr(value.start_date, days[0]?.date ?? value.today),
    end_date: dateOr(value.end_date, days.at(-1)?.date ?? value.today),
    days,
  }
}

async function fetchAvailability({ hospitalRef, doctorRef, startDate, endDate }: AvailabilityQuery): Promise<DoctorAvailability> {
  if (!isHospitalRef(hospitalRef) || !isPublicRef(doctorRef)) throw notFound()
  const sendDates = isSendableRange(startDate, endDate)
  const availability = await http.get<unknown>(
    `/hospitals/${encodeURIComponent(hospitalRef)}/doctors/${encodeURIComponent(doctorRef)}/availability`,
    sendDates ? { params: { start_date: startDate, end_date: endDate } } : undefined,
  )
  return toAvailability(availability)
}

/**
 * One doctor's free slots over one range of days. With no dates the server
 * chooses: today to a week ahead, inside the booking horizon. A range the
 * server would refuse is not sent — it is the same request as no range.
 * Nothing is asked until the hospital and the doctor are known.
 */
export function useDoctorAvailability(query: AvailabilityQuery, { enabled = true }: { enabled?: boolean } = {}) {
  const { hospitalRef, doctorRef } = query
  const sendDates = isSendableRange(query.startDate, query.endDate)
  const normalized: AvailabilityQuery = {
    hospitalRef,
    doctorRef,
    startDate: sendDates ? query.startDate : '',
    endDate: sendDates ? query.endDate : '',
  }
  return useQuery({
    queryKey: availabilityKeys.window(normalized),
    queryFn: () => fetchAvailability(normalized),
    enabled: enabled && hospitalRef !== '' && doctorRef !== '',
  })
}
