import { useId, useRef, type KeyboardEvent } from 'react'
import { CalendarDays, CalendarX, ChevronLeft, ChevronRight } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import type { Paginated } from '@atheris/api-core'
import { Button, cn, EmptyState, Skeleton } from '@atheris/ui'
import { useMyAppointments, type AppointmentScope, type MyAppointment } from '@/api/myAppointments'
import { PageHeading } from '@/components/PageHeading'
import { usePageTitle } from '@/lib/usePageTitle'
import { AppointmentCard } from '@/pages/appointments/AppointmentCard'
import { appointmentStrings as S } from '@/pages/appointments/strings'
import { queryOf, readView, withView, type AppointmentsView } from '@/pages/appointments/views'
import { loadFailureOf } from '@/pages/hospitals/loadFailure'
import { LoadProblem } from '@/pages/hospitals/LoadProblem'

const VIEWS: { view: AppointmentScope; label: string }[] = [
  { view: 'upcoming', label: S.upcomingTab },
  { view: 'past', label: S.pastTab },
]

/** Where the patient is in the results, in the server's own numbers. */
function positionOf({ items, pagination }: Paginated<MyAppointment>, view: AppointmentScope): string {
  const { page, pageSize, total } = pagination
  if (total === 0) return view === 'upcoming' ? S.noUpcomingStatus : S.noPastStatus
  if (items.length === 0) return S.pastEndStatus
  const from = (page - 1) * pageSize + 1
  const to = from + items.length - 1
  return from === 1 && to === total ? S.count(total) : S.range(from, to, total)
}

/**
 * The patient's own appointments, in the two halves the server divides them
 * into: upcoming, soonest first, and past, latest first. The query string is
 * the state, so back, forward and reload all come back to the same list.
 *
 * Nothing is cancelled from here. A card offers "Cancel" only for an upcoming
 * appointment the server says can be cancelled, and it leads to the
 * appointment's own page, which asks first.
 */
export function AppointmentsPage() {
  usePageTitle(S.listTitle)
  const [params, setParams] = useSearchParams()
  const shown = readView(params)
  const { view } = shown
  const appointments = useMyAppointments(queryOf(shown))

  const tabsId = useId()
  const tabs = useRef<Record<AppointmentScope, HTMLButtonElement | null>>({ upcoming: null, past: null })
  const panelHeading = useRef<HTMLHeadingElement>(null)

  // Another view and another page are both somewhere to come back from.
  const show = (changes: Partial<AppointmentsView>) => setParams((current) => withView(current, changes))

  const select = (next: AppointmentScope) => {
    if (next !== view) show({ view: next, page: 1 })
  }

  // The tabs are one stop for Tab; the arrow keys move between them, and the
  // one that has focus is the one shown.
  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>) => {
    const at = VIEWS.findIndex((each) => each.view === view)
    const to =
      event.key === 'ArrowRight' || event.key === 'ArrowDown'
        ? (at + 1) % VIEWS.length
        : event.key === 'ArrowLeft' || event.key === 'ArrowUp'
          ? (at - 1 + VIEWS.length) % VIEWS.length
          : event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? VIEWS.length - 1
              : -1
    if (to === -1) return
    event.preventDefault()
    const next = VIEWS[to].view
    tabs.current[next]?.focus()
    select(next)
  }

  // The control that was pressed is about to change or go: focus moves to the
  // top of the results, which is also where the new ones start.
  const goToPage = (next: number) => {
    show({ page: next })
    panelHeading.current?.focus()
  }

  const retry = () => {
    panelHeading.current?.focus()
    void appointments.refetch()
  }

  // A request held back because the browser is offline never fails: it waits.
  const isHeldOffline = appointments.isPending && appointments.fetchStatus === 'paused'
  const isLoading = appointments.isPending && !isHeldOffline
  const failure = isHeldOffline ? 'offline' : appointments.isError ? loadFailureOf(appointments.error) : null
  const result = !appointments.isPending && !appointments.isError ? appointments.data : null

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <PageHeading>{S.listHeading}</PageHeading>
        <p className="text-body-lg text-on-surface-variant">{S.listIntro}</p>
      </div>

      <div role="tablist" aria-label={S.viewsLabel} className="bg-surface-container grid grid-cols-2 gap-1 rounded-2xl p-1">
        {VIEWS.map((each) => {
          const selected = each.view === view
          return (
            <button
              key={each.view}
              ref={(element) => {
                tabs.current[each.view] = element
              }}
              type="button"
              role="tab"
              id={`${tabsId}-${each.view}`}
              aria-selected={selected}
              aria-controls={`${tabsId}-panel`}
              tabIndex={selected ? undefined : -1}
              onClick={() => select(each.view)}
              onKeyDown={onTabKey}
              className={cn(
                'min-h-11 rounded-xl px-4 text-base font-semibold transition-colors',
                selected ? 'bg-card text-primary shadow-xs' : 'text-on-surface-variant hover:text-on-surface',
              )}
            >
              {each.label}
            </button>
          )
        })}
      </div>

      <section role="tabpanel" id={`${tabsId}-panel`} aria-labelledby={`${tabsId}-${view}`} className="space-y-4">
        <div className="space-y-1">
          <h2 ref={panelHeading} tabIndex={-1} className="font-display text-title-lg text-primary scroll-mt-24">
            {view === 'upcoming' ? S.upcomingHeading : S.pastHeading}
          </h2>
          {/* Always here, so a change of count is announced, not a new element. */}
          <p role="status" className="text-body-sm text-on-surface-variant min-h-5">
            {isLoading ? S.loading : result ? positionOf(result, view) : ''}
          </p>
        </div>

        {isLoading && (
          <div aria-hidden className="space-y-3">
            <Skeleton className="h-40 w-full rounded-2xl" />
            <Skeleton className="h-40 w-full rounded-2xl" />
            <Skeleton className="h-40 w-full rounded-2xl" />
          </div>
        )}

        {failure && (
          <LoadProblem
            failure={failure}
            failedMessage={S.listFailed}
            onRetry={retry}
            isRetrying={appointments.isFetching}
          />
        )}

        {result && result.pagination.total === 0 && view === 'upcoming' && (
          <EmptyState
            icon={CalendarDays}
            title={S.noUpcomingTitle}
            description={S.noUpcomingBody}
            className="bg-card border py-10"
            action={
              <Button asChild size="touch">
                <Link to="/hospitals">{S.findHospital}</Link>
              </Button>
            }
          />
        )}

        {result && result.pagination.total === 0 && view === 'past' && (
          <EmptyState icon={CalendarX} title={S.noPastTitle} description={S.noPastBody} className="bg-card border py-10" />
        )}

        {/* A page past the end: the appointments exist, just not here. */}
        {result && result.pagination.total > 0 && result.items.length === 0 && (
          <EmptyState
            title={S.pastEndTitle}
            description={S.pastEndBody(result.pagination.total)}
            className="bg-card border py-10"
            action={
              <Button variant="outline" size="touch" onClick={() => goToPage(1)}>
                {S.firstPage}
              </Button>
            }
          />
        )}

        {result && result.items.length > 0 && (
          <ul aria-label={view === 'upcoming' ? S.upcomingHeading : S.pastHeading} className="space-y-3">
            {result.items.map((appointment) => (
              <AppointmentCard key={appointment.ref} appointment={appointment} offerCancel={view === 'upcoming'} />
            ))}
          </ul>
        )}

        {result && result.items.length > 0 && result.pagination.totalPages > 1 && (
          <nav aria-label={S.paginationLabel} className="space-y-3">
            <p className="text-body-sm text-on-surface-variant text-center">
              {S.pageOf(result.pagination.page, result.pagination.totalPages)}
            </p>
            <div className="grid grid-cols-2 gap-3">
              <Button
                variant="outline"
                size="touch"
                onClick={() => goToPage(result.pagination.page - 1)}
                disabled={result.pagination.page <= 1}
              >
                <ChevronLeft aria-hidden />
                {S.previous}
              </Button>
              <Button
                variant="outline"
                size="touch"
                onClick={() => goToPage(result.pagination.page + 1)}
                disabled={result.pagination.page >= result.pagination.totalPages}
              >
                {S.next}
                <ChevronRight aria-hidden />
              </Button>
            </div>
          </nav>
        )}
      </section>
    </div>
  )
}
