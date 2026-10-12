import { APPOINTMENTS_MAX_PAGE, type AppointmentScope, type AppointmentsQuery } from '@/api/myAppointments'

/**
 * What the appointments screen is showing, kept in the query string
 * (`?view=past&page=2`) so back, forward and reload all return to it.
 */
export interface AppointmentsView {
  view: AppointmentScope
  page: number
}

function pageOf(value: string | null): number {
  if (value === null || !/^[1-9]\d{0,3}$/.test(value)) return 1
  const page = Number(value)
  return page <= APPOINTMENTS_MAX_PAGE ? page : 1
}

/**
 * Read the view from the query string. The address bar is user input: any
 * view but `past` is the upcoming one, and a page that is not a whole number
 * in range is page 1 — so a hand-edited URL shows a list, never a validation
 * error, and nothing but the two scopes the server offers is ever sent.
 */
export function readView(params: URLSearchParams): AppointmentsView {
  return { view: params.get('view') === 'past' ? 'past' : 'upcoming', page: pageOf(params.get('page')) }
}

/** The query string with `changes` applied. A default is left out of the URL; other parameters are kept. */
export function withView(current: URLSearchParams, changes: Partial<AppointmentsView>): URLSearchParams {
  const { view, page } = { ...readView(current), ...changes }
  const next = new URLSearchParams(current)
  if (view === 'past') next.set('view', 'past')
  else next.delete('view')
  if (page > 1) next.set('page', String(page))
  else next.delete('page')
  return next
}

/** What the server is asked for, for a view. */
export const queryOf = ({ view, page }: AppointmentsView): AppointmentsQuery => ({ scope: view, page })
