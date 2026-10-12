import type { MyAppointment } from '@/api/myAppointments'
import { appointmentStrings as S } from '@/pages/appointments/strings'
import { formatDay, formatSlotTime, localDateOf } from '@/pages/doctors/availability'

/**
 * When an appointment is, in words. Every part is on the HOSPITAL's clock —
 * the zone the server named — and never the browser's. Where the browser does
 * not know that zone, the day and the time are read off the instant's own
 * offset, which the server writes as the hospital's.
 */

/** The hospital's day of an instant, e.g. "Monday 12 October 2026". */
export const dayOf = (iso: string, timezone: string) => formatDay(localDateOf(iso, timezone) ?? iso.slice(0, 10))

/** The appointment's day. */
export const appointmentDay = ({ start, timezone }: Pick<MyAppointment, 'start' | 'timezone'>) => dayOf(start, timezone)

/** The appointment's start and end, e.g. "10:00 – 10:15". */
export const appointmentTime = ({ start, end, timezone }: Pick<MyAppointment, 'start' | 'end' | 'timezone'>) =>
  S.timeRange(formatSlotTime(start, timezone), formatSlotTime(end, timezone))

/** "You can cancel until 08:00, Monday 12 October 2026 (Asia/Kolkata)", or nothing when the server gave no instant. */
export function cancelUntilText({ cancel_until: until, timezone }: Pick<MyAppointment, 'cancel_until' | 'timezone'>): string | null {
  if (until === null) return null
  const time = formatSlotTime(until, timezone)
  const day = dayOf(until, timezone)
  return time && day ? S.cancelUntil(time, day, timezone) : null
}
