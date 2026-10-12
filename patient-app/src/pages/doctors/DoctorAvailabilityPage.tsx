import { useEffect, useId, useRef, useState } from 'react'
import { CalendarX2, ChevronLeft, ChevronRight, Globe } from 'lucide-react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { ApiError } from '@atheris/api-core'
import { Alert, Button, cn, EmptyState, Skeleton } from '@atheris/ui'
import { availabilityKeys, useDoctorAvailability, type AvailabilityDay, type DoctorAvailability } from '@/api/availability'
import type { PatientDoctor } from '@/api/doctors'
import type { PatientHospital } from '@/api/hospitals'
import { BackLink } from '@/components/BackLink'
import { PageHeading } from '@/components/PageHeading'
import {
  addDays,
  dayParts,
  formatDay,
  formatSlotTime,
  isSelectableDate,
  parseInstant,
  WINDOW_DAYS,
  windowContaining,
  type DateWindow,
} from '@/pages/doctors/availability'
import { DoctorLoader } from '@/pages/doctors/DoctorLoader'
import { doctorBookingPath, doctorPath } from '@/pages/doctors/paths'
import { doctorStrings as S } from '@/pages/doctors/strings'
import { loadFailureOf, type LoadFailure } from '@/pages/hospitals/loadFailure'
import { LoadProblem } from '@/pages/hospitals/LoadProblem'

/**
 * Where "View Availability" leads: the doctor's free slots, as the server
 * returns them and on the hospital's clock. The doctor is read first, like on
 * the profile, so one who is not listed has no such page and nothing is asked
 * about their availability.
 */
export function DoctorAvailabilityPage() {
  const { hospitalRef = '', doctorRef = '' } = useParams()
  return (
    // Keyed by the doctor, so moving to another one starts a fresh page.
    <DoctorLoader
      key={`${hospitalRef}/${doctorRef}`}
      hospitalRef={hospitalRef}
      doctorRef={doctorRef}
      title={() => S.availabilityTitle}
    >
      {(doctor, hospital) => <Availability doctor={doctor} hospital={hospital} />}
    </DoctorLoader>
  )
}

/** What the page keeps in its query string. `null` takes a part out of it; `undefined` leaves it alone. */
interface Selection {
  date?: string | null
  slot?: string | null
}

/** The query string with `changes` applied; parameters that are not the page's are kept. */
function withSelection(previous: URLSearchParams, changes: Selection): URLSearchParams {
  const next = new URLSearchParams(previous)
  for (const [name, value] of Object.entries(changes)) {
    if (value === undefined) continue
    if (value) next.set(name, value)
    else next.delete(name)
  }
  return next
}

/** A failed window read is told apart from the rest by the one answer that has its own screen. */
const isRefusedDates = (err: unknown) => err instanceof ApiError && err.status === 400

/** A request held back because the browser is offline never fails: it waits. */
const isHeldOffline = (query: UseQueryResult<DoctorAvailability>) => query.isPending && query.fetchStatus === 'paused'

const latest = (a: string, b: string) => (a > b ? a : b)
const earliest = (a: string, b: string) => (a < b ? a : b)

/**
 * The query string is the state: the day in view (`date`) and the slot chosen
 * on it (`slot`), so back, forward and reload all come back to the same place.
 * Everything else — today, the horizon, the days and their slots — is the
 * server's answer, and nothing is made up around it.
 */
function Availability({ doctor, hospital }: { doctor: PatientDoctor; hospital: PatientHospital }) {
  const [params, setParams] = useSearchParams()
  const queryClient = useQueryClient()
  const dateParam = params.get('date') ?? ''
  const slotParam = params.get('slot') ?? ''

  // The first read asks for nothing in particular: the server answers with
  // today, the horizon and the first week. That answer anchors everything
  // after it — it is the only "today" the page knows.
  const anchor = useDoctorAvailability({ hospitalRef: hospital.ref, doctorRef: doctor.ref, startDate: '', endDate: '' })
  const base = anchor.data

  // A day in the address that the first week does not cover: the week that
  // holds it is read instead, by explicit dates. One week, one request.
  const window: DateWindow | null =
    base !== undefined &&
    isSelectableDate(dateParam, base.today, base.horizon_end) &&
    (dateParam < base.start_date || dateParam > base.end_date)
      ? windowContaining(dateParam, base.today, base.horizon_end)
      : null
  const windowed = useDoctorAvailability(
    { hospitalRef: hospital.ref, doctorRef: doctor.ref, startDate: window?.start ?? '', endDate: window?.end ?? '' },
    { enabled: window !== null },
  )

  const current = window === null ? anchor : windowed
  const data: DoctorAvailability | undefined = window === null ? base : windowed.data
  const days = data?.days ?? []
  const today = base?.today ?? ''
  const horizonEnd = base?.horizon_end ?? ''

  // The day in view: the one in the address when the answer has it; otherwise
  // the first day with a free slot, and failing that the first day there is.
  const selectedDate = data
    ? days.some((day) => day.date === dateParam)
      ? dateParam
      : (days.find((day) => day.slots.length > 0)?.date ?? days[0]?.date ?? today)
    : ''
  const selectedDay: AvailabilityDay | undefined = days.find((day) => day.date === selectedDate)
  const selectedSlot = selectedDay?.slots.find((slot) => slot.start === slotParam) ?? null

  const daysId = useId()
  const timesId = useId()
  const selectionId = useId()
  const daysHeading = useRef<HTMLHeadingElement>(null)
  const timesHeading = useRef<HTMLHeadingElement>(null)

  // A slot in the address that the day in view does not offer is remembered
  // here, with its day, as it leaves the address, so the page can say it is
  // gone — on that day, and not on another reached by back or forward. A time
  // that was taken gets the note; anything that was never a time is dropped quietly.
  const [goneSlot, setGoneSlot] = useState<{ slot: string; date: string } | null>(null)
  if (selectedDay !== undefined && slotParam !== '' && selectedSlot === null && goneSlot?.slot !== slotParam) {
    setGoneSlot({ slot: slotParam, date: selectedDay.date })
  }

  // A day the address asks for that cannot be booked — before today, after the
  // horizon, or not a date at all — falls back to today and leaves the address.
  useEffect(() => {
    if (base === undefined || dateParam === '' || isSelectableDate(dateParam, base.today, base.horizon_end)) return
    setParams((previous) => withSelection(previous, { date: null, slot: null }), { replace: true })
  }, [base, dateParam, setParams])

  useEffect(() => {
    if (selectedDay === undefined || slotParam === '' || selectedSlot !== null) return
    setParams((previous) => withSelection(previous, { slot: null }), { replace: true })
  }, [selectedDay, slotParam, selectedSlot, setParams])

  const show = (changes: Selection, how: 'replace' | 'push' = 'replace') =>
    setParams((previous) => withSelection(previous, changes), { replace: how === 'replace' })

  const chooseDay = (date: string) => {
    if (date === selectedDate) return
    setGoneSlot(null)
    show({ date, slot: null })
  }

  const chooseSlot = (start: string) => {
    setGoneSlot(null)
    show({ date: selectedDate, slot: selectedSlot?.start === start ? null : start })
  }

  // The button that was pressed is about to go: focus moves to the top of the times.
  const clearSelection = () => {
    show({ slot: null })
    timesHeading.current?.focus()
  }

  // Each week is its own history entry, so "back" returns to the week before.
  // The pressed control may become disabled at an edge: focus moves to the heading.
  const goToWindow = (date: string) => {
    setGoneSlot(null)
    show({ date, slot: null }, 'push')
    daysHeading.current?.focus()
  }

  // The server said the dates are behind it now: forget every week that was
  // read and ask again for nothing in particular, which is always today.
  const goToToday = () => {
    setGoneSlot(null)
    queryClient.removeQueries({ queryKey: availabilityKeys.doctor(hospital.ref, doctor.ref) })
    show({ date: null, slot: null })
    daysHeading.current?.focus()
  }

  const retry = () => {
    daysHeading.current?.focus()
    void current.refetch()
  }

  const shownWindow: DateWindow | null = data ? { start: data.start_date, end: data.end_date } : window
  const isLoading = current.isPending && !isHeldOffline(current)
  const refusedDates = window !== null && windowed.isError && isRefusedDates(windowed.error)
  const failure: LoadFailure | null = isHeldOffline(current)
    ? 'offline'
    : current.isError && !refusedDates
      ? loadFailureOf(current.error)
      : null
  const windowIsEmpty = data !== undefined && days.every((day) => day.slots.length === 0)
  const canGoEarlier = shownWindow !== null && today !== '' && shownWindow.start > today
  const canGoLater = shownWindow !== null && horizonEnd !== '' && shownWindow.end < horizonEnd
  const slotGone = goneSlot !== null && goneSlot.date === selectedDate && parseInstant(goneSlot.slot) !== null

  const status = isLoading
    ? S.loadingAvailability
    : data === undefined
      ? ''
      : windowIsEmpty
        ? S.windowStatus(formatDay(data.start_date, 'short'), formatDay(data.end_date, 'short'))
        : selectedDay
          ? S.dayStatus(selectedDay.slots.length, formatDay(selectedDay.date))
          : ''

  const timeZone = data?.timezone ?? base?.timezone ?? ''
  const timeOf = (iso: string) => formatSlotTime(iso, timeZone)

  return (
    <div className="space-y-6">
      <BackLink to={doctorPath(hospital.ref, doctor.ref)}>{S.backToDoctor(doctor.name)}</BackLink>

      <header className="space-y-1">
        <PageHeading>{S.availabilityHeading}</PageHeading>
        <p className="text-body-lg text-on-surface break-words">{doctor.name}</p>
        {doctor.specialization && <p className="text-body-sm text-on-surface-variant break-words">{doctor.specialization}</p>}
        {hospital.name && <p className="text-body-sm text-on-surface-variant break-words">{hospital.name}</p>}
      </header>

      {timeZone !== '' && (
        <p className="text-body-sm text-on-surface-variant flex items-start gap-2">
          <Globe className="text-secondary mt-0.5 size-4 shrink-0" aria-hidden />
          <span className="break-words">
            {S.timezoneNoteBefore}
            {timeZone}
            {S.timezoneNoteAfter}
          </span>
        </p>
      )}

      <section aria-labelledby={daysId} className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h2 id={daysId} ref={daysHeading} tabIndex={-1} className="font-display text-title-lg text-primary scroll-mt-24">
            {S.daysHeading}
          </h2>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="icon-touch"
              aria-label={S.earlierDays}
              disabled={!canGoEarlier || isLoading}
              onClick={() => shownWindow && goToWindow(latest(today, addDays(shownWindow.start, -WINDOW_DAYS)))}
            >
              <ChevronLeft aria-hidden />
            </Button>
            <Button
              variant="outline"
              size="icon-touch"
              aria-label={S.laterDays}
              disabled={!canGoLater || isLoading}
              onClick={() => shownWindow && goToWindow(earliest(horizonEnd, addDays(shownWindow.end, 1)))}
            >
              <ChevronRight aria-hidden />
            </Button>
          </div>
        </div>

        {shownWindow && !refusedDates && (
          <p className="text-body-sm text-on-surface-variant">
            {S.windowRange(formatDay(shownWindow.start, 'short'), formatDay(shownWindow.end, 'short'))}
          </p>
        )}

        {isLoading && (
          <div aria-hidden className="-mx-4 flex gap-2 overflow-hidden px-4">
            {Array.from({ length: WINDOW_DAYS }, (_, index) => (
              <Skeleton key={index} className="h-24 w-20 shrink-0 rounded-2xl" />
            ))}
          </div>
        )}

        {failure && (
          <LoadProblem
            // Here the doctor is known: whatever went wrong, it is the availability that did not load.
            failure={failure === 'not_found' ? 'error' : failure}
            failedMessage={S.availabilityFailed}
            onRetry={retry}
            isRetrying={current.isFetching}
          />
        )}

        {refusedDates && (
          <div className="space-y-3">
            <Alert variant="warning" title={S.dateGoneTitle}>
              {S.dateGoneBody}
            </Alert>
            <Button variant="outline" size="touch" onClick={goToToday}>
              {S.goToToday}
            </Button>
          </div>
        )}

        {data && days.length > 0 && (
          <ul aria-label={S.daysLabel} className="-mx-4 flex snap-x gap-2 overflow-x-auto px-4 pb-2">
            {days.map((day) => (
              <li key={day.date} className="shrink-0 snap-start">
                <DayChip day={day} isSelected={day.date === selectedDate} isToday={day.date === today} onChoose={chooseDay} />
              </li>
            ))}
          </ul>
        )}

        {data && windowIsEmpty && (
          <EmptyState
            icon={CalendarX2}
            title={S.emptyWindowTitle}
            description={S.emptyWindowBody}
            className="bg-card border py-10"
          />
        )}
      </section>

      <section aria-labelledby={timesId} className="space-y-3">
        <div className="space-y-1">
          <h2 id={timesId} ref={timesHeading} tabIndex={-1} className="font-display text-title-lg text-primary scroll-mt-24">
            {S.timesHeading}
          </h2>
          {/* Always here, so a change of count is announced, not a new element. */}
          <p role="status" className="text-body-sm text-on-surface-variant min-h-5">
            {status}
          </p>
          {slotGone && <p className="text-body-sm text-error font-semibold">{S.slotGone}</p>}
        </div>

        {isLoading && (
          <div aria-hidden className="grid grid-cols-3 gap-2 sm:grid-cols-4">
            {Array.from({ length: 6 }, (_, index) => (
              <Skeleton key={index} className="h-11 w-full rounded-xl" />
            ))}
          </div>
        )}

        {selectedDay && !windowIsEmpty && selectedDay.slots.length === 0 && (
          <EmptyState title={S.emptyDayTitle} description={S.emptyDayBody} className="bg-card border py-10" />
        )}

        {selectedDay && selectedDay.slots.length > 0 && (
          <ul aria-label={S.timesLabel(formatDay(selectedDay.date))} className="grid grid-cols-3 gap-2 sm:grid-cols-4">
            {selectedDay.slots.map((slot) => {
              const isSelected = selectedSlot?.start === slot.start
              return (
                <li key={slot.start}>
                  <button
                    type="button"
                    aria-pressed={isSelected}
                    aria-label={S.slotLabel(timeOf(slot.start), timeOf(slot.end), formatDay(selectedDay.date))}
                    onClick={() => chooseSlot(slot.start)}
                    className={cn(
                      'text-body-lg flex min-h-11 w-full items-center justify-center rounded-xl border font-semibold tabular-nums transition-colors',
                      'focus-visible:ring-ring/50 outline-none focus-visible:ring-[3px]',
                      isSelected
                        ? 'border-primary bg-primary text-primary-foreground'
                        : 'bg-card text-on-surface hover:bg-accent hover:text-accent-foreground',
                    )}
                  >
                    {timeOf(slot.start)}
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </section>

      <section aria-labelledby={selectionId} className="bg-card space-y-3 rounded-2xl border p-5">
        <h2 id={selectionId} className="font-display text-title-lg text-primary">
          {S.selectionHeading}
        </h2>
        <p className="text-body-lg text-on-surface break-words">
          {selectedSlot && selectedDay
            ? S.selectionSummary(formatDay(selectedDay.date), timeOf(selectedSlot.start), timeOf(selectedSlot.end))
            : S.selectionHint}
        </p>
        <p className="text-body-sm text-on-surface-variant">{S.notReserved}</p>
        <div className="flex flex-col gap-3">
          {selectedSlot && selectedDay ? (
            <Button asChild size="touch" className="w-full">
              <Link
                to={doctorBookingPath(hospital.ref, doctor.ref, {
                  date: selectedDay.date,
                  start: selectedSlot.start,
                  end: selectedSlot.end,
                })}
              >
                {S.continueToBooking}
              </Link>
            </Button>
          ) : (
            <Button size="touch" className="w-full" disabled>
              {S.continueToBooking}
            </Button>
          )}
          {selectedSlot && (
            <Button variant="outline" size="touch" className="w-full" onClick={clearSelection}>
              {S.clearSelection}
            </Button>
          )}
        </div>
      </section>
    </div>
  )
}

interface DayChipProps {
  day: AvailabilityDay
  isSelected: boolean
  isToday: boolean
  onChoose: (date: string) => void
}

/** One day of the strip: the weekday and date, and how many free slots the server returned for it. */
function DayChip({ day, isSelected, isToday, onChoose }: DayChipProps) {
  const { weekday, day: dayOfMonth, month } = dayParts(day.date)
  const count = day.slots.length
  return (
    <button
      type="button"
      aria-pressed={isSelected}
      aria-label={S.dayLabel(formatDay(day.date), count)}
      onClick={() => onChoose(day.date)}
      className={cn(
        'flex min-h-11 w-20 flex-col items-center gap-0.5 rounded-2xl border px-2 py-2 text-center transition-colors',
        'focus-visible:ring-ring/50 outline-none focus-visible:ring-[3px]',
        isSelected
          ? 'border-primary bg-primary text-primary-foreground'
          : 'bg-card text-on-surface hover:bg-accent hover:text-accent-foreground',
      )}
    >
      <span className="text-body-sm font-semibold">{isToday ? S.today : weekday.slice(0, 3)}</span>
      <span className="font-display text-title-lg leading-none">{dayOfMonth}</span>
      <span className="text-body-sm">{month.slice(0, 3)}</span>
      <span className={cn('text-body-sm', isSelected ? '' : count === 0 ? 'text-outline' : 'text-secondary font-semibold')}>
        {count === 0 ? S.noSlotsOnChip : S.slotCount(count)}
      </span>
    </button>
  )
}
