import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { LoaderCircle } from 'lucide-react'
import { Alert, Button, cn } from '@atheris/ui'
import {
  CANCEL_TEXT_MAX_LENGTH,
  cancelTextLength,
  classifyCancelError,
  isCancelRetriable,
  useCancelAppointment,
  type CancelFailure,
  type CancelInput,
  type CancelReasonCode,
  type MyAppointment,
} from '@/api/myAppointments'
import { checkboxClass, fieldControlClass } from '@/components/fieldStyles'
import { FormField } from '@/components/FormField'
import { ModalDialog } from '@/components/ModalDialog'
import { appointmentStrings as S, cancelReasons } from '@/pages/appointments/strings'
import { appointmentDay, appointmentTime } from '@/pages/appointments/when'

/** How a cancellation ended, when it ended: the server answered, and there is nothing more to ask here. */
export type CancelOutcome = 'cancelled' | 'no_longer_cancellable' | 'too_late' | 'not_found'

interface CancelDialogProps {
  appointment: MyAppointment
  /** The patient chose to keep the appointment. Nothing was sent, or nothing more will be. */
  onKeep: () => void
  /** The server answered. Called even when this dialog has already been taken off the screen by that answer. */
  onOutcome: (outcome: CancelOutcome) => void
  onAnnounce: (message: string) => void
  /** The id of the control focus returns to when the dialog closes and what had it before is gone. */
  fallbackFocusId?: string
}

/** What a failure that can be tried again says. */
const RETRY_MESSAGE: Partial<Record<CancelFailure, { title: string; body: string; tone: 'warning' | 'error' }>> = {
  offline: { title: S.cancelOfflineTitle, body: S.cancelOfflineBody, tone: 'warning' },
  server: { title: S.cancelFailedTitle, body: S.cancelFailedBody, tone: 'error' },
  unconfirmed: { title: S.cancelUnconfirmedTitle, body: S.cancelUnconfirmedBody, tone: 'error' },
  invalid: { title: S.cancelInvalidTitle, body: S.cancelInvalidBody, tone: 'error' },
}

const outcomeFocusClass = 'focus-visible:ring-ring/50 outline-none focus-visible:ring-[3px]'

/**
 * The question asked before an appointment is cancelled: which appointment,
 * why, and "Keep appointment" beside "Cancel appointment". Nothing is sent
 * until a reason is chosen and the patient confirms, and one confirmation
 * sends one request — a second press, a retry and a refreshed session all
 * send that same request again.
 *
 * Nothing here says the appointment is cancelled. Only the server's answer
 * does, and the page shows that.
 */
export function CancelDialog({ appointment, onKeep, onOutcome, onAnnounce, fallbackFocusId }: CancelDialogProps) {
  const cancel = useCancelAppointment()
  const reasonName = useId()
  const legendId = useId()
  const counterId = useId()

  // The request as it first went out. A retry sends this, not what is on screen now.
  const request = useRef<CancelInput | null>(null)
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

  const [code, setCode] = useState<CancelReasonCode | null>(null)
  const [details, setDetails] = useState('')
  const [detailsError, setDetailsError] = useState(false)
  const [locked, setLocked] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [attempts, setAttempts] = useState(0)
  const [failure, setFailure] = useState<CancelFailure | null>(null)
  const detailsField = useRef<HTMLTextAreaElement>(null)
  const problem = useRef<HTMLDivElement>(null)

  // A failure takes focus, so it is read and the patient is at the way on.
  useEffect(() => {
    if (failure) problem.current?.focus()
  }, [failure, attempts])

  const count = cancelTextLength(details.trim())
  const tooLong = count > CANCEL_TEXT_MAX_LENGTH

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || settled.current) return
    if (request.current === null) {
      if (code === null) return
      if (tooLong) {
        setDetailsError(true)
        detailsField.current?.focus()
        return
      }
      const trimmed = details.trim()
      request.current = { ref: appointment.ref, reasonCode: code, ...(trimmed ? { reasonText: trimmed } : {}) }
    }
    inFlight.current = true
    setLocked(true)
    setSubmitting(true)
    setFailure(null)
    setAttempts((made) => made + 1)
    onAnnounce(S.cancelling)
    try {
      await cancel.mutateAsync(request.current)
      settled.current = true
      onOutcome('cancelled')
    } catch (error) {
      const kind = classifyCancelError(error)
      if (kind === 'no_longer_cancellable' || kind === 'too_late' || kind === 'not_found') {
        // A refusal is final; the page says what it was.
        settled.current = true
        onOutcome(kind)
      } else if (mounted.current) {
        // A request the server could not read is not sent again as it was.
        if (!isCancelRetriable(kind)) {
          request.current = null
          setLocked(false)
        }
        onAnnounce('')
        setFailure(kind)
      }
    } finally {
      inFlight.current = false
      if (mounted.current) setSubmitting(false)
    }
  }

  const retry = failure ? RETRY_MESSAGE[failure] : undefined
  const summary = S.dialogSummary(
    appointment.doctor.name,
    appointmentDay(appointment),
    S.timeWithZone(appointmentTime(appointment), appointment.timezone),
  )

  return (
    <ModalDialog title={S.dialogTitle} description={summary} onDismiss={onKeep} busy={submitting} fallbackFocusId={fallbackFocusId}>
      <form onSubmit={(event) => void submit(event)} noValidate aria-busy={submitting} className="space-y-4">
        <fieldset aria-labelledby={legendId} aria-required="true" className="space-y-1">
          <legend id={legendId} className="text-body-sm text-on-surface font-semibold">
            {S.reasonLegend}
          </legend>
          <p className="text-body-sm text-on-surface-variant">{locked ? S.detailsLocked : S.reasonRequiredHint}</p>
          {cancelReasons.map((reason) => (
            <label
              key={reason.code}
              className={cn('text-body-lg text-on-surface flex min-h-11 items-center gap-3', locked ? 'opacity-60' : 'cursor-pointer')}
            >
              <input
                type="radio"
                name={reasonName}
                value={reason.code}
                checked={code === reason.code}
                disabled={locked}
                onChange={() => setCode(reason.code)}
                // Enter on a choice is not a confirmation: only the button cancels.
                onKeyDown={(event) => {
                  if (event.key === 'Enter') event.preventDefault()
                }}
                className={cn(checkboxClass, 'mt-0')}
              />
              <span>{reason.label}</span>
            </label>
          ))}
        </fieldset>

        <FormField
          label={S.detailsFieldLabel}
          hint={S.detailsHint}
          error={detailsError && tooLong ? S.detailsTooLong(CANCEL_TEXT_MAX_LENGTH) : undefined}
        >
          {(field) => (
            <textarea
              {...field}
              ref={detailsField}
              aria-describedby={[field['aria-describedby'], counterId].filter(Boolean).join(' ')}
              rows={2}
              value={details}
              readOnly={locked}
              onChange={(event) => setDetails(event.target.value)}
              className={cn(fieldControlClass, 'min-h-20 resize-y read-only:opacity-60')}
            />
          )}
        </FormField>
        <p id={counterId} className={cn('text-body-sm tabular-nums', tooLong ? 'text-error font-semibold' : 'text-on-surface-variant')}>
          {S.detailsCount(count, CANCEL_TEXT_MAX_LENGTH)}
        </p>

        {retry && (
          <Alert ref={problem} tabIndex={-1} variant={retry.tone} title={retry.title} className={outcomeFocusClass}>
            {retry.body}
          </Alert>
        )}

        <div className="flex flex-col gap-3">
          <Button type="button" variant="outline" size="touch" className="w-full" onClick={onKeep} disabled={submitting}>
            {S.keepAppointment}
          </Button>
          <Button
            type="submit"
            variant="destructive"
            size="touch"
            className="w-full"
            disabled={submitting || code === null}
            aria-busy={submitting}
          >
            {submitting && <LoaderCircle className="animate-spin" aria-hidden />}
            {failure !== null && isCancelRetriable(failure) ? S.tryAgain : S.cancelAppointment}
          </Button>
        </div>
      </form>
    </ModalDialog>
  )
}
