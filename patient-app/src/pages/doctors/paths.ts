import { hospitalDoctorsPath } from '@/pages/hospitals/paths'

/**
 * Where a doctor's pages live in the app, under their hospital. Both
 * references come from the server and are escaped, so each can only ever be
 * one path segment.
 */
export const doctorPath = (hospitalRef: string, doctorRef: string) =>
  `${hospitalDoctorsPath(hospitalRef)}/${encodeURIComponent(doctorRef)}`

/** What the availability page keeps in its query string: the day in view and the slot chosen on it. */
export interface AvailabilitySelection {
  date?: string
  /** The slot's start, as the server wrote it. */
  slot?: string
}

/** A query string from the parts that are given; none given, no `?` at all. */
const query = (parts: Record<string, string | undefined>) => {
  const params = new URLSearchParams()
  for (const [name, value] of Object.entries(parts)) if (value) params.set(name, value)
  const text = params.toString()
  return text ? `?${text}` : ''
}

export const doctorAvailabilityPath = (hospitalRef: string, doctorRef: string, selection: AvailabilitySelection = {}) =>
  `${doctorPath(hospitalRef, doctorRef)}/availability${query({ date: selection.date, slot: selection.slot })}`

/** One chosen slot, handed on to booking: the hospital's day it is on and its two instants. */
export interface BookingSelection {
  date: string
  start: string
  end: string
}

export const doctorBookingPath = (hospitalRef: string, doctorRef: string, { date, start, end }: BookingSelection) =>
  `${doctorPath(hospitalRef, doctorRef)}/book${query({ date, start, end })}`
