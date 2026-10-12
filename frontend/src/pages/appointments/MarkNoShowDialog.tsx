import { useState } from 'react'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { UserX } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { FormDialog } from '@/components/forms/FormDialog'
import { FormRefusal } from '@/components/forms/formRefusal'
import { useMarkNoShow, type AppointmentSummary } from '@/api/appointments'
import { ApiError } from '@/api/types'
import { formatTime } from '@/lib/format'
import { lifecycleErrorMessage } from './lifecycle'

// The endpoint takes no body — there is nothing to fill in, only to confirm.
const confirmSchema = z.object({})
type ConfirmValues = z.infer<typeof confirmSchema>

const VERB = { infinitive: 'mark as a no-show', participle: 'marked as a no-show' }
const FAILED = "Couldn't mark the appointment as a no-show. Please try again."

/** The shared lifecycle wording, except where "mark as a no-show" does not fit its sentence. */
function noShowError(err: unknown): string {
  const status = err instanceof ApiError ? err.status : undefined
  if (status === 403) return "You don't have permission to mark appointments as no-shows."
  if (status === 400 || status === 404 || status === 409 || status === 422) {
    return lifecycleErrorMessage(err, VERB)
  }
  return FAILED
}

/** Shown inside the open dialog, so "now" is read when the user is deciding. */
function NotYetDue({ scheduledStart }: { scheduledStart: string }) {
  const [now] = useState(() => Date.now())
  if (new Date(scheduledStart).getTime() <= now) return null
  return (
    <Alert variant="warning" title="This appointment hasn't started yet">
      The patient may still arrive. Use Cancel if they have asked not to come.
    </Alert>
  )
}

/**
 * Confirm that a patient did not turn up (`POST /appointments/{id}/no-show`).
 *
 * This is reception's manual path; the server's sweeper does the same once the
 * hospital's grace period has passed. It is confirmed because it is final: a
 * no-show is never reopened, and the slot goes back to the doctor's day.
 */
export function MarkNoShowDialog({
  appointment,
  disabled,
}: {
  appointment: AppointmentSummary
  disabled?: boolean
}) {
  const markNoShow = useMarkNoShow()
  return (
    <FormDialog<ConfirmValues>
      trigger={
        <Button
          variant="ghost"
          size="sm"
          disabled={disabled}
          aria-label={`Mark ${appointment.patient_name} as a no-show`}
        >
          <UserX className="size-4" /> No-show
        </Button>
      }
      title="Mark as a no-show?"
      description={
        <>
          {appointment.patient_name} with {appointment.doctor_name} at{' '}
          {formatTime(appointment.scheduled_start)}. This records that the patient did not attend.
          It can't be undone — a new appointment has to be booked.
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Mark no-show"
      pendingLabel="Marking…"
      destructive
      dismissLabel="Keep appointment"
      fallbackError={FAILED}
      onSubmit={async () => {
        try {
          await markNoShow.mutateAsync(appointment.id)
        } catch (err) {
          // The server's wording for these names internal statuses.
          throw new FormRefusal(noShowError(err))
        }
        return `${appointment.patient_name} marked as a no-show`
      }}
    >
      {() => <NotYetDue scheduledStart={appointment.scheduled_start} />}
    </FormDialog>
  )
}
