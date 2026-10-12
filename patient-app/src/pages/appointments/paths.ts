import type { AppointmentScope } from '@/api/myAppointments'

/** Where the patient's own appointments live in the app. */
export const APPOINTMENTS_PATH = '/appointments'

/** The list, on one of its two views. Upcoming and the first page are the defaults and are left out. */
export function appointmentsPath({ view = 'upcoming', page = 1 }: { view?: AppointmentScope; page?: number } = {}) {
  const params = new URLSearchParams()
  if (view === 'past') params.set('view', 'past')
  if (page > 1) params.set('page', String(page))
  const query = params.toString()
  return query ? `${APPOINTMENTS_PATH}?${query}` : APPOINTMENTS_PATH
}

/** One appointment's page. The reference comes from the server and is escaped, so it is only ever one path segment. */
export const appointmentPath = (ref: string) => `${APPOINTMENTS_PATH}/${encodeURIComponent(ref)}`

/** What asks the appointment's page to open its cancellation dialog. It only asks: the page decides. */
export const CANCEL_PARAM = 'cancel'

/** One appointment's page, opened on its cancellation dialog. Nothing is cancelled by following it. */
export const appointmentCancelPath = (ref: string) => `${appointmentPath(ref)}?${CANCEL_PARAM}=1`
