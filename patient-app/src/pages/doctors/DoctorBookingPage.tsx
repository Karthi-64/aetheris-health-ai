import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { CircleCheck, Link2Off, LoaderCircle } from 'lucide-react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { newIdempotencyKey } from '@atheris/api-core'
import { Alert, Button, cn } from '@atheris/ui'
import {
  classifyBookingError,
  isRetriable,
  REASON_MAX_LENGTH,
  reasonLength,
  useBookAppointment,
  type BookedAppointment,
  type BookingFailure,
  type BookingInput,
} from '@/api/appointments'
import type { PatientDoctor } from '@/api/doctors'
import type { PatientHospital } from '@/api/hospitals'
import { BackLink } from '@/components/BackLink'
import { fieldControlClass } from '@/components/fieldStyles'
import { FormField } from '@/components/FormField'
import { PageHeading } from '@/components/PageHeading'
import { usePageTitle } from '@/lib/usePageTitle'
import { formatDay, formatSlotTime, localDateOf, readChosenSlot, type ChosenSlot } from '@/pages/doctors/availability'
import { appointmentPath, APPOINTMENTS_PATH } from '@/pages/appointments/paths'
import { DoctorLoader, DoctorNotAvailable } from '@/pages/doctors/DoctorLoader'
import { doctorAvailabilityPath, doctorPath } from '@/pages/doctors/paths'
import { doctorStrings as S } from '@/pages/doctors/strings'
import { linkStateFor } from '@/pages/hospitals/linkState'

/**
 * Where "Continue to booking" leads: the chosen slot is shown back for review,
 * on the hospital's clock, and is booked only when the patient confirms it.
 * The doctor is read first, like on the profile, so one who is not listed has
 * no such page.
 *
 * Nothing on this page says an appointment exists until the server has
 * answered with it. Seeing the review holds nothing, and one confirmation
 * sends one request — a second click, a retry and a refreshed session all
 * send that same request again, under the same idempotency key.
 */
export function DoctorBookingPage() {
  const { hospitalRef = '', doctorRef = '' } = useParams()
  return (
    // Keyed by the doctor, so moving to another one starts a fresh page.
    <DoctorLoader
      key={`${hospitalRef}/${doctorRef}`}
      hospitalRef={hospitalRef}
      doctorRef={doctorRef}
      title={() => S.bookingTitle}
    >
      {(doctor, hospital) => <Booking doctor={doctor} hospital={hospital} />}
    </DoctorLoader>
  )
}

interface BookingProps {
  doctor: PatientDoctor
  hospital: PatientHospital
}

function Booking({ doctor, hospital }: BookingProps) {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  // Held in memory only: a reload starts again from the review.
  const [booked, setBooked] = useState<BookedAppointment | null>(null)
  const [announcement, setAnnouncement] = useState('')
  // The link is user input. A slot it names is acted on only when every part
  // of it holds together; otherwise nothing of it is shown, and nothing is sent.
  const chosen = readChosenSlot(params, hospital.timezone)

  const onBooked = (appointment: BookedAppointment) => {
    setBooked(appointment)
    setAnnouncement(S.bookedHeading)
    // The slot is spent. It is taken out of this history entry, so coming back
    // to the entry, or reloading it, does not offer the same confirmation again.
    void navigate(`${doctorPath(hospital.ref, doctor.ref)}/book`, { replace: true })
  }

  return (
    <>
      {booked ? (
        <Confirmation appointment={booked} hospital={hospital} doctor={doctor} />
      ) : chosen ? (
        // Keyed by the slot: another slot is another booking, with its own key.
        <Review
          key={`${chosen.start}|${chosen.end}`}
          chosen={chosen}
          doctor={doctor}
          hospital={hospital}
          onBooked={onBooked}
          onAnnounce={setAnnouncement}
        />
      ) : (
        <div className="space-y-6">
          <BackLink to={doctorAvailabilityPath(hospital.ref, doctor.ref)}>{S.backToAvailability}</BackLink>
          <WhoseBooking hospital={hospital} doctor={doctor} />
          <InvalidLink hospital={hospital} doctor={doctor} />
        </div>
      )}
      {/* Always here, so what happens to the booking is announced, not a new element. */}
      <p role="status" className="sr-only">
        {announcement}
      </p>
    </>
  )
}

const panelClass = 'bg-card flex flex-col items-center gap-4 rounded-2xl border px-6 py-10 text-center'
const outcomeFocusClass = 'focus-visible:ring-ring/50 outline-none focus-visible:ring-[3px]'

/** The page's heading where there is no review under it to say whose booking this would be. */
function WhoseBooking({ hospital, doctor }: BookingProps) {
  return (
    <header className="space-y-1">
      <PageHeading>{S.bookingHeading}</PageHeading>
      <p className="text-body-lg text-on-surface break-words">{doctor.name}</p>
      {doctor.specialization && <p className="text-body-sm text-on-surface-variant break-words">{doctor.specialization}</p>}
      {hospital.name && <p className="text-body-sm text-on-surface-variant break-words">{hospital.name}</p>}
    </header>
  )
}

/** A link that names no slot the page can act on, or a request the server could not read. */
function InvalidLink({ hospital, doctor, focusOnMount = false }: BookingProps & { focusOnMount?: boolean }) {
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    if (focusOnMount) heading.current?.focus()
  }, [focusOnMount])

  return (
    <div className={panelClass}>
      <span className="bg-surface-container text-outline flex size-14 items-center justify-center rounded-2xl">
        <Link2Off className="size-7" aria-hidden />
      </span>
      <div className="space-y-1">
        <h2 ref={heading} tabIndex={-1} className={cn('font-display text-title-lg text-primary', outcomeFocusClass)}>
          {S.invalidLinkTitle}
        </h2>
        <p className="text-body-sm text-on-surface-variant mx-auto max-w-sm">{S.invalidLinkBody}</p>
      </div>
      <div className="flex w-full flex-col gap-3">
        <Button asChild size="touch" className="w-full">
          <Link to={doctorAvailabilityPath(hospital.ref, doctor.ref)}>{S.goToAvailability}</Link>
        </Button>
        <Button asChild variant="outline" size="touch" className="w-full">
          <Link to={doctorPath(hospital.ref, doctor.ref)}>{S.backToProfile}</Link>
        </Button>
      </div>
    </div>
  )
}

/** What a failure that can be tried again says. */
const RETRY_MESSAGE: Record<string, { title: string; body?: string; tone: 'warning' | 'error' }> = {
  offline: { title: S.bookingOfflineTitle, body: S.bookingOfflineBody, tone: 'warning' },
  server: { title: S.bookingFailedTitle, body: S.bookingFailedBody, tone: 'error' },
  unconfirmed: { title: S.bookingUnconfirmedTitle, body: S.bookingUnconfirmedBody, tone: 'error' },
  policies_pending: { title: S.bookingFailedTitle, body: S.bookingPoliciesPending, tone: 'error' },
}

/** What a refusal says. After one, this slot is not offered for confirmation again. */
const REFUSAL_MESSAGE: Record<string, string> = {
  slot_taken: S.slotTaken,
  not_bookable: S.notBookable,
  own_overlap: S.ownOverlap,
  limit_reached: S.limitReached,
  link_required: S.linkRequired,
}

/** The refusals after which the slot in the link is no use: the way back to availability leaves it out. */
const SLOT_IS_STALE: BookingFailure[] = ['slot_taken', 'not_bookable', 'own_overlap']

interface ReviewProps extends BookingProps {
  chosen: ChosenSlot
  onBooked: (appointment: BookedAppointment) => void
  onAnnounce: (message: string) => void
}

function Review({ chosen, doctor, hospital, onBooked, onAnnounce }: ReviewProps) {
  const book = useBookAppointment()
  const reviewId = useId()
  const counterId = useId()

  // One key for this review of this slot, made once and kept in memory: never
  // in the address, never in storage. Every attempt at this booking carries it.
  const [idempotencyKey] = useState(newIdempotencyKey)
  // The request as it first went out. A retry sends this, not what is on screen now.
  const request = useRef<BookingInput | null>(null)
  // The guards against a second request: `disabled` on the button is for the
  // eye; these hold even for a second event that arrives before it is drawn.
  const inFlight = useRef(false)
  const settled = useRef(false)
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [attempts, setAttempts] = useState(0)
  const [failure, setFailure] = useState<BookingFailure | null>(null)
  const reasonField = useRef<HTMLTextAreaElement>(null)
  const outcome = useRef<HTMLDivElement>(null)

  // A failure takes focus, so it is read and the patient is at the way on.
  useEffect(() => {
    if (failure) outcome.current?.focus()
  }, [failure, attempts])

  const count = reasonLength(reason.trim())
  const tooLong = count > REASON_MAX_LENGTH

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || settled.current) return
    if (tooLong && request.current === null) {
      setReasonError(true)
      reasonField.current?.focus()
      return
    }
    inFlight.current = true
    const trimmed = reason.trim()
    request.current ??= {
      hospitalRef: hospital.ref,
      doctorRef: doctor.ref,
      start: chosen.start,
      end: chosen.end,
      ...(trimmed ? { reason: trimmed } : {}),
      idempotencyKey,
    }
    setSubmitting(true)
    setFailure(null)
    setAttempts((made) => made + 1)
    onAnnounce(S.bookingInProgress)
    try {
      const appointment = await book.mutateAsync(request.current)
      // Whatever page the patient has moved on to is not this one's to change.
      if (!mounted.current) return
      settled.current = true
      onBooked(appointment)
    } catch (error) {
      if (!mounted.current) return
      const kind = classifyBookingError(error)
      // A refusal is final for this slot; only a lost answer is asked for again.
      if (!isRetriable(kind)) settled.current = true
      onAnnounce('')
      setFailure(kind)
    } finally {
      inFlight.current = false
      if (mounted.current) setSubmitting(false)
    }
  }

  if (failure === 'not_found') return <DoctorNotAvailable hospital={hospital} focusOnMount />

  const slotIsStale = failure !== null && SLOT_IS_STALE.includes(failure)
  const availability = doctorAvailabilityPath(
    hospital.ref,
    doctor.ref,
    failure === 'invalid' ? {} : slotIsStale ? { date: chosen.date } : { date: chosen.date, slot: chosen.start },
  )
  const profile = doctorPath(hospital.ref, doctor.ref)

  if (failure === 'invalid') {
    return (
      <div className="space-y-6">
        <BackLink to={availability}>{S.backToAvailability}</BackLink>
        <WhoseBooking hospital={hospital} doctor={doctor} />
        <InvalidLink hospital={hospital} doctor={doctor} focusOnMount />
      </div>
    )
  }

  const retry = failure ? RETRY_MESSAGE[failure] : undefined
  const refusal = failure ? REFUSAL_MESSAGE[failure] : undefined
  // Once a request has gone out, the reason is part of it.
  const locked = attempts > 0

  return (
    <div className="space-y-6">
      <BackLink to={availability}>{S.backToAvailability}</BackLink>
      <PageHeading>{S.bookingHeading}</PageHeading>

      <section aria-labelledby={reviewId} className="bg-card space-y-3 rounded-2xl border p-5">
        <h2 id={reviewId} className="font-display text-title-lg text-primary">
          {S.reviewHeading}
        </h2>
        <dl className="space-y-2">
          {hospital.name && (
            <div>
              <dt className="text-body-sm text-on-surface-variant">{S.reviewHospitalLabel}</dt>
              <dd className="text-body-lg text-on-surface break-words">{hospital.name}</dd>
            </div>
          )}
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.reviewDoctorLabel}</dt>
            <dd className="text-body-lg text-on-surface break-words">
              {doctor.name}
              {doctor.specialization && (
                <span className="text-body-sm text-on-surface-variant block">{doctor.specialization}</span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.chosenDayLabel}</dt>
            <dd className="text-body-lg text-on-surface">{formatDay(chosen.date)}</dd>
          </div>
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.chosenTimeLabel}</dt>
            <dd className="text-body-lg text-on-surface tabular-nums">
              {formatSlotTime(chosen.start, hospital.timezone)} – {formatSlotTime(chosen.end, hospital.timezone)}
            </dd>
          </div>
        </dl>
        <p className="text-body-sm text-on-surface-variant break-words">
          {S.timezoneNoteBefore}
          {hospital.timezone}
          {S.timezoneNoteAfter}
        </p>
      </section>

      {refusal ? (
        <div className="space-y-3">
          <Alert ref={outcome} tabIndex={-1} variant="error" title={refusal} className={outcomeFocusClass} />
          <div className="flex flex-col gap-3">
            {failure === 'link_required' ? (
              <>
                <Button asChild size="touch" className="w-full">
                  {/* The code travels in router state, as it does from the hospital's own page. */}
                  <Link to="/link-patient" state={linkStateFor(hospital.ref)}>
                    {S.linkRecord}
                  </Link>
                </Button>
                <Button asChild variant="outline" size="touch" className="w-full">
                  <Link to={availability}>{S.backToAvailability}</Link>
                </Button>
              </>
            ) : failure === 'limit_reached' ? (
              <>
                <Button asChild size="touch" className="w-full">
                  <Link to={profile}>{S.backToTheDoctor}</Link>
                </Button>
                <Button asChild variant="outline" size="touch" className="w-full">
                  <Link to="/">{S.backToHome}</Link>
                </Button>
              </>
            ) : (
              <>
                <Button asChild size="touch" className="w-full">
                  <Link to={availability}>{S.chooseAnotherTime}</Link>
                </Button>
                <Button asChild variant="outline" size="touch" className="w-full">
                  <Link to={profile}>{S.backToTheDoctor}</Link>
                </Button>
              </>
            )}
          </div>
        </div>
      ) : (
        <form onSubmit={(event) => void submit(event)} noValidate aria-busy={submitting} className="space-y-4">
          <FormField
            label={S.reasonLabel}
            hint={locked ? S.reasonLocked : S.reasonHint}
            error={reasonError && tooLong ? S.reasonTooLong(REASON_MAX_LENGTH) : undefined}
          >
            {(field) => (
              <textarea
                {...field}
                ref={reasonField}
                aria-describedby={[field['aria-describedby'], counterId].filter(Boolean).join(' ')}
                rows={3}
                value={reason}
                readOnly={locked}
                onChange={(event) => setReason(event.target.value)}
                className={cn(fieldControlClass, 'min-h-24 resize-y read-only:opacity-60')}
              />
            )}
          </FormField>
          <p id={counterId} className={cn('text-body-sm tabular-nums', tooLong ? 'text-error font-semibold' : 'text-on-surface-variant')}>
            {S.reasonCount(count, REASON_MAX_LENGTH)}
          </p>

          <Alert role="note" variant="warning">
            {S.emergencyNotice}
          </Alert>
          <p className="text-body-sm text-on-surface-variant">{S.notHeld}</p>

          {retry && (
            <Alert ref={outcome} tabIndex={-1} variant={retry.tone} title={retry.title} className={outcomeFocusClass}>
              {retry.body}
            </Alert>
          )}

          <div className="flex flex-col gap-3">
            <Button type="submit" size="touch" className="w-full" disabled={submitting} aria-busy={submitting}>
              {submitting && <LoaderCircle className="animate-spin" aria-hidden />}
              {retry || attempts > 1 ? S.tryBookingAgain : S.confirmAppointment}
            </Button>
            {submitting ? (
              <Button type="button" variant="outline" size="touch" className="w-full" disabled>
                {S.chooseAnotherTime}
              </Button>
            ) : (
              <Button asChild variant="outline" size="touch" className="w-full">
                <Link to={availability}>{S.chooseAnotherTime}</Link>
              </Button>
            )}
          </div>
        </form>
      )}
    </div>
  )
}

interface ConfirmationProps extends BookingProps {
  appointment: BookedAppointment
}

/**
 * The server's answer, shown back: every line of it is what the server said
 * was booked, not what the page asked for. There is nothing here to confirm
 * again.
 */
function Confirmation({ appointment, hospital, doctor }: ConfirmationProps) {
  usePageTitle(S.bookedTitle)
  const detailsId = useId()
  const { timezone } = appointment
  // The hospital's day; if the browser does not know the zone, the day the server's own offset gives.
  const day = formatDay(localDateOf(appointment.start, timezone) ?? appointment.start.slice(0, 10))

  return (
    <div className="space-y-6">
      <div className="bg-card flex flex-col items-center gap-3 rounded-2xl border px-6 py-8 text-center">
        <CircleCheck className="text-stable size-12" aria-hidden />
        <PageHeading focusOnMount>{S.bookedHeading}</PageHeading>
        <p className="text-body-lg text-on-surface-variant">{S.bookedBody}</p>
      </div>

      <section aria-labelledby={detailsId} className="bg-card space-y-3 rounded-2xl border p-5">
        <h2 id={detailsId} className="font-display text-title-lg text-primary">
          {S.bookedDetailsHeading}
        </h2>
        <dl className="space-y-2">
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.referenceLabel}</dt>
            <dd className="text-body-lg text-on-surface font-mono break-all select-all">
              {/* The reference is also the way to the appointment's own page. */}
              <Link to={appointmentPath(appointment.ref)} className="text-secondary inline-flex min-h-11 items-center hover:underline">
                {appointment.ref}
              </Link>
            </dd>
          </div>
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.statusLabel}</dt>
            <dd className="text-body-lg text-on-surface">{S.statusBooked}</dd>
          </div>
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.reviewDoctorLabel}</dt>
            <dd className="text-body-lg text-on-surface break-words">
              {appointment.doctor.name}
              {appointment.doctor.specialization && (
                <span className="text-body-sm text-on-surface-variant block">{appointment.doctor.specialization}</span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.reviewHospitalLabel}</dt>
            <dd className="text-body-lg text-on-surface break-words">{appointment.hospital.name}</dd>
          </div>
          {day && (
            <div>
              <dt className="text-body-sm text-on-surface-variant">{S.chosenDayLabel}</dt>
              <dd className="text-body-lg text-on-surface">{day}</dd>
            </div>
          )}
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.chosenTimeLabel}</dt>
            <dd className="text-body-lg text-on-surface tabular-nums">
              {formatSlotTime(appointment.start, timezone)} – {formatSlotTime(appointment.end, timezone)}
            </dd>
          </div>
        </dl>
        <p className="text-body-sm text-on-surface-variant break-words">
          {S.timezoneNoteBefore}
          {timezone}
          {S.timezoneNoteAfter}
        </p>
      </section>

      <div className="flex flex-col gap-3">
        <Button asChild size="touch" className="w-full">
          <Link to={APPOINTMENTS_PATH}>{S.viewMyAppointments}</Link>
        </Button>
        <Button asChild variant="outline" size="touch" className="w-full">
          <Link to="/">{S.backToHome}</Link>
        </Button>
        <Button asChild variant="outline" size="touch" className="w-full">
          <Link to={doctorPath(hospital.ref, doctor.ref)}>{S.backToTheDoctor}</Link>
        </Button>
      </div>
    </div>
  )
}
