import { useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Loader2 } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { useCancelAppointment, type AppointmentSummary } from '@/api/appointments'
import { ApiError } from '@/api/types'
import { formatTime } from '@/lib/format'
import { lifecycleErrorMessage } from './lifecycle'

// Module spec §11: a cancellation reason is required, non-blank, ≤ 500 chars.
const schema = z.object({
  reason: z
    .string()
    .trim()
    .min(1, 'Give a reason for cancelling')
    .max(500, 'Keep the reason under 500 characters'),
})

type FormValues = z.infer<typeof schema>

const VERB = { infinitive: 'cancel', participle: 'cancelled' }

/** Confirms a cancellation and collects the reason the API requires. */
export function CancelAppointmentDialog({
  appointment,
  disabled,
}: {
  appointment: AppointmentSummary
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const submitting = useRef(false)
  const cancel = useCancelAppointment()
  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: { reason: '' } })

  function onOpenChange(next: boolean) {
    setOpen(next)
    if (!next) reset()
  }

  async function onSubmit(values: FormValues) {
    if (submitting.current) return
    submitting.current = true
    try {
      await cancel.mutateAsync({ id: appointment.id, reason: values.reason })
      toast.success('Appointment cancelled')
      onOpenChange(false)
    } catch (err) {
      if (err instanceof ApiError && err.status === 422 && Array.isArray(err.details)) {
        const onReason = (err.details as Array<{ field?: string; message?: string }>).find(
          (fe) => fe.field === 'reason',
        )
        if (onReason) {
          setError('reason', { message: onReason.message ?? 'Invalid reason' })
          return
        }
      }
      toast.error(lifecycleErrorMessage(err, VERB))
      // Anything but a rejected reason means this appointment can't be
      // cancelled from here, so there is nothing left to correct in the form.
      if (err instanceof ApiError && err.status !== 422) onOpenChange(false)
    } finally {
      submitting.current = false
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="text-error hover:text-error"
          disabled={disabled}
          aria-label={`Cancel appointment for ${appointment.patient_name}`}
        >
          Cancel
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Cancel this appointment?</DialogTitle>
          <DialogDescription>
            {appointment.patient_name} with {appointment.doctor_name} at{' '}
            {formatTime(appointment.scheduled_start)}. A cancelled appointment can't be reopened —
            a new one has to be booked.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-4" noValidate>
          <Field label="Reason" required error={errors.reason?.message}>
            {(p) => (
              <Textarea rows={3} placeholder="e.g. Patient asked to cancel" {...p} {...register('reason')} />
            )}
          </Field>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Keep appointment
            </Button>
            <Button
              type="submit"
              variant="destructive"
              disabled={cancel.isPending}
              aria-busy={cancel.isPending}
            >
              {cancel.isPending && <Loader2 className="size-4 animate-spin" />}
              {cancel.isPending ? 'Cancelling…' : 'Cancel appointment'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
