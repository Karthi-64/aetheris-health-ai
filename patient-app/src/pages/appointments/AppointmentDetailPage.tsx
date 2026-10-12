import { useEffect, useId, useRef, useState, type RefObject } from 'react'
import { CalendarX } from 'lucide-react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Alert, Button, cn, Skeleton } from '@atheris/ui'
import { useMyAppointment, type MyAppointment } from '@/api/myAppointments'
import { BackLink } from '@/components/BackLink'
import { PageHeading } from '@/components/PageHeading'
import { usePageTitle } from '@/lib/usePageTitle'
import { CancelDialog, type CancelOutcome } from '@/pages/appointments/CancelDialog'
import { APPOINTMENTS_PATH, CANCEL_PARAM } from '@/pages/appointments/paths'
import { StatusBadge } from '@/pages/appointments/StatusBadge'
import { appointmentStrings as S } from '@/pages/appointments/strings'
import { appointmentDay, appointmentTime, cancelUntilText } from '@/pages/appointments/when'
import { loadFailureOf } from '@/pages/hospitals/loadFailure'
import { LoadProblem } from '@/pages/hospitals/LoadProblem'
import { hospitalPath } from '@/pages/hospitals/paths'

/**
 * One of the patient's appointments, and the only place it can be cancelled
 * from. Whether it can be is the server's answer (`can_cancel`), never worked
 * out here from the status or the time; and it is shown as cancelled only
 * when the server has answered that it is.
 */
export function AppointmentDetailPage() {
  const { appointmentRef = '' } = useParams()
  // Keyed by the appointment, so moving to another one starts a fresh page.
  return <AppointmentDetail key={appointmentRef} appointmentRef={appointmentRef} />
}

const focusClass = 'focus-visible:ring-ring/50 outline-none focus-visible:ring-[3px]'

/** What a refusal says, once the dialog has closed on it. */
const REFUSAL_MESSAGE: Partial<Record<CancelOutcome, string>> = {
  no_longer_cancellable: S.noLongerCancellable,
  too_late: S.tooLate,
}

function AppointmentDetail({ appointmentRef }: { appointmentRef: string }) {
  usePageTitle(S.detailTitle)
  const appointment = useMyAppointment(appointmentRef)
  const [params, setParams] = useSearchParams()
  // How the last attempt to cancel ended. Held here, above everything the answer redraws.
  const [outcome, setOutcome] = useState<CancelOutcome | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const status = useRef<HTMLSpanElement>(null)
  const refusal = useRef<HTMLDivElement>(null)
  const cancelButtonId = useId()
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // The outcome takes focus: the new status when it is cancelled, the refusal when it is not.
  useEffect(() => {
    if (outcome === 'cancelled') status.current?.focus()
    else if (outcome !== null) refusal.current?.focus()
  }, [outcome])

  // The address only asks for the dialog; it is opened when the server says the appointment can be cancelled.
  const asksToCancel = params.get(CANCEL_PARAM) === '1'
  const askToCancel = (asks: boolean) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current)
        if (asks) next.set(CANCEL_PARAM, '1')
        else next.delete(CANCEL_PARAM)
        return next
      },
      { replace: true },
    )

  const openDialog = () => {
    setOutcome(null)
    setAnnouncement('')
    askToCancel(true)
  }

  const keep = () => {
    setAnnouncement('')
    askToCancel(false)
  }

  const onOutcome = (kind: CancelOutcome) => {
    // Whatever page the patient has moved on to is not this one's to change.
    if (!mounted.current) return
    setOutcome(kind)
    setAnnouncement(kind === 'cancelled' ? S.cancelled : '')
    askToCancel(false)
  }

  if (outcome === 'not_found') return <NotFound focusOnMount />

  // A request held back because the browser is offline never fails: it waits.
  const isHeldOffline = appointment.isPending && appointment.fetchStatus === 'paused'
  const isLoading = appointment.isPending && !isHeldOffline
  const failure = isHeldOffline ? 'offline' : appointment.isError ? loadFailureOf(appointment.error) : null
  if (failure === 'not_found') return <NotFound />

  const loaded = !appointment.isPending && !appointment.isError ? appointment.data : null
  const refusalMessage = outcome ? REFUSAL_MESSAGE[outcome] : undefined

  return (
    <div className="space-y-6">
      <BackLink to={APPOINTMENTS_PATH}>{S.backToList}</BackLink>
      <PageHeading>{S.detailHeading}</PageHeading>

      {refusalMessage && <Alert ref={refusal} tabIndex={-1} variant="error" title={refusalMessage} className={focusClass} />}

      {isLoading && (
        <div aria-hidden className="space-y-3">
          <Skeleton className="h-72 w-full rounded-2xl" />
          <Skeleton className="h-11 w-full rounded-xl" />
        </div>
      )}

      {failure && (
        <LoadProblem
          failure={failure}
          failedMessage={S.appointmentFailed}
          onRetry={() => void appointment.refetch()}
          isRetrying={appointment.isFetching}
        />
      )}

      {loaded && (
        <Loaded
          appointment={loaded}
          wasCancelledHere={outcome === 'cancelled'}
          wasRefused={refusalMessage !== undefined}
          statusRef={status}
          cancelButtonId={cancelButtonId}
          onCancel={openDialog}
        />
      )}

      {/* Opened only on the server's word, and closed for good by its answer. */}
      {loaded && loaded.can_cancel === true && asksToCancel && outcome === null && (
        <CancelDialog
          appointment={loaded}
          onKeep={keep}
          onOutcome={onOutcome}
          onAnnounce={setAnnouncement}
          fallbackFocusId={cancelButtonId}
        />
      )}

      {/* Always here, so what happens to the appointment is announced, not a new element. */}
      <p role="status" className="sr-only">
        {announcement || (isLoading ? S.loadingAppointment : '')}
      </p>
    </div>
  )
}

interface LoadedProps {
  appointment: MyAppointment
  /** The server has just answered that it is cancelled, on this page. */
  wasCancelledHere: boolean
  /** The server has just refused to cancel it; the page already says so. */
  wasRefused: boolean
  statusRef: RefObject<HTMLSpanElement | null>
  cancelButtonId: string
  onCancel: () => void
}

const labelClass = 'text-body-sm text-on-surface-variant'
const valueClass = 'text-body-lg text-on-surface'

/** The appointment as the server last described it. Every line is the server's; nothing is assumed. */
function Loaded({ appointment, wasCancelledHere, wasRefused, statusRef, cancelButtonId, onCancel }: LoadedProps) {
  const detailsId = useId()
  const { doctor, hospital, timezone } = appointment
  const until = cancelUntilText(appointment)

  return (
    <>
      {wasCancelledHere && (
        // A note, not an alert: the live region below has already said it once.
        <Alert role="note" variant="success" title={S.cancelled}>
          {S.cancelledBody}
        </Alert>
      )}

      <section aria-labelledby={detailsId} className="bg-card space-y-3 rounded-2xl border p-5">
        <h2 id={detailsId} className="font-display text-title-lg text-primary">
          {S.detailsHeading}
        </h2>
        <dl className="space-y-2">
          <div>
            <dt className={labelClass}>{S.statusLabel}</dt>
            <dd className={valueClass}>
              <span ref={statusRef} tabIndex={-1} className={cn('inline-block rounded-full', focusClass)}>
                <StatusBadge status={appointment.status} />
              </span>
            </dd>
          </div>
          <div>
            <dt className={labelClass}>{S.doctorLabel}</dt>
            <dd className={cn(valueClass, 'break-words')}>
              {doctor.name}
              {doctor.specialization && <span className="text-body-sm text-on-surface-variant block">{doctor.specialization}</span>}
            </dd>
          </div>
          <div>
            <dt className={labelClass}>{S.hospitalLabel}</dt>
            <dd className={cn(valueClass, 'break-words')}>
              {hospital.ref ? (
                <Link to={hospitalPath(hospital.ref)} className="text-secondary inline-flex min-h-11 items-center font-semibold hover:underline">
                  {hospital.name}
                </Link>
              ) : (
                hospital.name
              )}
            </dd>
          </div>
          <div>
            <dt className={labelClass}>{S.dayLabel}</dt>
            <dd className={valueClass}>{appointmentDay(appointment)}</dd>
          </div>
          <div>
            <dt className={labelClass}>{S.timeLabel}</dt>
            <dd className={cn(valueClass, 'tabular-nums')}>{appointmentTime(appointment)}</dd>
          </div>
          {appointment.reason && (
            <div>
              <dt className={labelClass}>{S.reasonLabel}</dt>
              <dd className={cn(valueClass, 'break-words whitespace-pre-line')}>{appointment.reason}</dd>
            </div>
          )}
          <div>
            <dt className={labelClass}>{S.referenceLabel}</dt>
            <dd className={cn(valueClass, 'font-mono break-all select-all')}>{appointment.ref}</dd>
          </div>
        </dl>
        <p className="text-body-sm text-on-surface-variant break-words">
          {S.timezoneNoteBefore}
          {timezone}
          {S.timezoneNoteAfter}
        </p>
      </section>

      {appointment.can_cancel === true ? (
        <div className="space-y-3">
          {until && <p className="text-body-sm text-on-surface-variant break-words">{until}</p>}
          <Button
            id={cancelButtonId}
            type="button"
            variant="outline"
            size="touch"
            className="border-error text-error hover:bg-error/5 hover:text-error w-full"
            onClick={onCancel}
          >
            {S.cancelAppointment}
          </Button>
        </div>
      ) : (
        // Still booked, and the server will not let the app cancel it: say where to turn.
        appointment.status === 'booked' &&
        !wasRefused && (
          <Alert role="note" variant="warning">
            {S.notCancellableInApp}
          </Alert>
        )
      )}
    </>
  )
}

/** Unknown, someone else's, not a reference at all: the server says one thing and so does the app. */
function NotFound({ focusOnMount }: { focusOnMount?: boolean }) {
  usePageTitle(S.detailTitle)
  return (
    <div className="bg-card flex flex-col items-center gap-4 rounded-2xl border px-6 py-10 text-center">
      <span className="bg-surface-container text-outline flex size-14 items-center justify-center rounded-2xl">
        <CalendarX className="size-7" aria-hidden />
      </span>
      <div className="space-y-1">
        <PageHeading className="text-title-lg" focusOnMount={focusOnMount}>
          {S.notFoundTitle}
        </PageHeading>
        <p className="text-body-sm text-on-surface-variant mx-auto max-w-sm">{S.notFoundBody}</p>
      </div>
      <Button asChild size="touch">
        <Link to={APPOINTMENTS_PATH}>{S.goToList}</Link>
      </Button>
    </div>
  )
}
