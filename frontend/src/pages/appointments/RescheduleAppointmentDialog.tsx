import { useRef, useState } from 'react'
import { Controller, useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { ArrowLeft, ArrowRight, CalendarClock, Loader2 } from 'lucide-react'
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
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Alert } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { SlotPicker } from '@/components/appointments/SlotPicker'
import { useRescheduleAppointment, type AppointmentSummary } from '@/api/appointments'
import { useHospital } from '@/api/hospitals'
import { ApiError } from '@/api/types'
import { fieldErrorsOf } from '@/lib/apiErrors'
import { formatDayIn, formatTimeIn, isoDateIn } from '@/lib/format'
import { lifecycleErrorMessage } from './lifecycle'

const schema = z.object({
  date: z.string().min(1, 'Pick a date'),
  /** The chosen slot's `start`, exactly as the API sent it. */
  slot: z.string().min(1, 'Pick a new time slot'),
  /** That slot's `end`. */
  slot_end: z.string(),
  // Module spec §11: the API rejects a reason over 500 characters.
  reason: z.string().max(500, 'Keep the reason under 500 characters'),
})

type FormValues = z.infer<typeof schema>

const VERB = { infinitive: 'reschedule', participle: 'rescheduled' }

interface Notice {
  variant: 'warning' | 'error'
  title: string
  body: string
}

/** "Tue, 6 Oct 2026 · 11:00 AM – 11:30 AM" on the hospital's clock. */
function windowLabel(start: string, end: string, timeZone?: string): string {
  return `${formatDayIn(start, timeZone)} · ${formatTimeIn(start, timeZone)} – ${formatTimeIn(end, timeZone)}`
}

function When({ label, value, emphasis }: { label: string; value: string; emphasis?: boolean }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <dt className="font-label text-label-caps text-on-surface-variant w-20 shrink-0">{label}</dt>
      <dd
        className={
          emphasis
            ? 'font-body text-body-sm text-primary font-semibold tabular-nums'
            : 'font-body text-body-sm text-on-surface tabular-nums'
        }
      >
        {value}
      </dd>
    </div>
  )
}

/**
 * The form itself. Mounted once the hospital's timezone is known, so the date
 * it opens on is the appointment's day on the hospital's calendar.
 */
function RescheduleForm({
  appointment,
  timeZone,
  onDone,
}: {
  appointment: AppointmentSummary
  timeZone?: string
  onDone: () => void
}) {
  const [step, setStep] = useState<'pick' | 'confirm'>('pick')
  const [notice, setNotice] = useState<Notice | null>(null)
  // `isPending` only disables the button after a re-render; this also stops a
  // second submit (double click, Enter held down) fired before that happens.
  const submitting = useRef(false)
  const reschedule = useRescheduleAppointment()
  const [today] = useState(() => isoDateIn(new Date().toISOString(), timeZone))

  const {
    control,
    register,
    handleSubmit,
    setError,
    setValue,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { date: isoDateIn(appointment.scheduled_start, timeZone), slot: '', slot_end: '', reason: '' },
  })
  const [date, slot, slotEnd] = useWatch({ control, name: ['date', 'slot', 'slot_end'] })

  const current = windowLabel(appointment.scheduled_start, appointment.scheduled_end, timeZone)

  /** A slot belongs to one day, so changing the day drops it. */
  function clearSlot() {
    setValue('slot', '')
    setValue('slot_end', '')
  }

  /** Back to choosing, with the reason the chosen time was refused. */
  function refuse(next: Notice) {
    clearSlot()
    setStep('pick')
    setNotice(next)
  }

  async function save(values: FormValues) {
    if (submitting.current) return
    submitting.current = true
    setNotice(null)
    try {
      await reschedule.mutateAsync({
        id: appointment.id,
        scheduled_start: values.slot,
        scheduled_end: values.slot_end,
        ...(values.reason.trim() ? { reason: values.reason.trim() } : {}),
      })
      toast.success(`Appointment moved to ${windowLabel(values.slot, values.slot_end, timeZone)}`)
      onDone()
    } catch (err) {
      const status = err instanceof ApiError ? err.status : undefined
      if (status === 409) {
        // The slots are refetched by the hook; the taken one must be re-picked.
        refuse({
          variant: 'warning',
          title: 'Time unavailable',
          body: `${appointment.doctor_name} already has an appointment in that window. Pick another time.`,
        })
        return
      }
      if (status === 422) {
        const fields = fieldErrorsOf(err)
        const onReason = fields.find((fe) => fe.field === 'reason')
        if (onReason) setError('reason', { message: onReason.message })
        const rest = fields.filter((fe) => fe !== onReason).map((fe) => fe.message)
        setStep('pick')
        if (rest.length > 0 || !onReason) {
          setNotice({
            variant: 'error',
            title: "Couldn't reschedule",
            body: rest.join(' ') || (err as ApiError).message,
          })
        }
        return
      }
      if (status === 400) {
        // A rule the user can act on — the time is outside the doctor's hours,
        // already past, or the appointment is no longer `booked`. In the last
        // case the refreshed queue stops offering this action and the dialog
        // goes with it, so the toast is what carries the reason.
        const message = (err as ApiError).message
        refuse({ variant: 'error', title: "Couldn't reschedule", body: message })
        toast.error(message)
        return
      }
      toast.error(lifecycleErrorMessage(err, VERB))
      // Gone or not permitted: nothing in this form can fix that.
      if (status === 403 || status === 404) onDone()
    } finally {
      submitting.current = false
    }
  }

  function onSubmit(values: FormValues) {
    if (step === 'pick') {
      setNotice(null)
      setStep('confirm')
      return
    }
    return save(values)
  }

  return (
    <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-4" noValidate>
      {notice && (
        <Alert variant={notice.variant} title={notice.title}>
          {notice.body}
        </Alert>
      )}

      <dl className="neo-pressed bg-surface space-y-2 rounded-xl px-4 py-3">
        <When label="Current" value={current} />
        {step === 'confirm' && (
          <When label="New" value={windowLabel(slot, slotEnd, timeZone)} emphasis />
        )}
      </dl>

      {step === 'pick' ? (
        <>
          <Field label="New date" required error={errors.date?.message}>
            {(p) => (
              <Input type="date" min={today} {...p} {...register('date', { onChange: clearSlot })} />
            )}
          </Field>

          {date ? (
            <Controller
              control={control}
              name="slot"
              render={({ field }) => (
                <SlotPicker
                  doctorId={appointment.doctor_id}
                  date={date}
                  value={field.value}
                  error={errors.slot?.message}
                  currentAppointmentId={appointment.id}
                  onChange={(picked) => {
                    setValue('slot_end', picked.end)
                    field.onChange(picked.start)
                  }}
                />
              )}
            />
          ) : (
            <p className="font-body text-body-sm text-on-surface-variant">
              Pick a date to see {appointment.doctor_name}'s time slots.
            </p>
          )}

          <Field label="Reason" hint="Optional — kept in the appointment's history" error={errors.reason?.message}>
            {(p) => (
              <Textarea rows={2} placeholder="e.g. Patient asked for a later time" {...p} {...register('reason')} />
            )}
          </Field>
        </>
      ) : (
        <p className="font-body text-body-sm text-on-surface-variant">
          {appointment.patient_name} will be seen by {appointment.doctor_name} at the new time. The
          current time is released for other patients.
        </p>
      )}

      <DialogFooter>
        {step === 'pick' ? (
          <>
            <Button type="button" variant="ghost" onClick={onDone}>
              Keep current time
            </Button>
            <Button type="submit">
              Review change <ArrowRight className="size-4" />
            </Button>
          </>
        ) : (
          <>
            <Button
              type="button"
              variant="ghost"
              disabled={reschedule.isPending}
              onClick={() => setStep('pick')}
            >
              <ArrowLeft className="size-4" /> Back
            </Button>
            <Button type="submit" disabled={reschedule.isPending} aria-busy={reschedule.isPending}>
              {reschedule.isPending && <Loader2 className="size-4 animate-spin" />}
              {reschedule.isPending ? 'Rescheduling…' : 'Confirm reschedule'}
            </Button>
          </>
        )}
      </DialogFooter>
    </form>
  )
}

/**
 * What the open dialog holds. The hospital record is read here rather than by
 * the trigger, so a queue of fifty rows does not subscribe to it fifty times.
 */
function RescheduleBody({
  appointment,
  onDone,
}: {
  appointment: AppointmentSummary
  onDone: () => void
}) {
  const hospital = useHospital()
  if (hospital.isPending) {
    return (
      <div role="status" aria-label="Loading appointment" className="space-y-3">
        <Skeleton className="h-14 w-full rounded-xl" />
        <Skeleton className="h-10 w-full rounded-xl" />
      </div>
    )
  }
  // Without the hospital record the viewer's own clock is used.
  return <RescheduleForm appointment={appointment} timeZone={hospital.data?.timezone} onDone={onDone} />
}

/**
 * Move a booked appointment to another of the same doctor's slots
 * (`PATCH /appointments/{id}`). Two steps: pick the new slot, then confirm the
 * change with the current and new times side by side.
 *
 * Only the time can move — the API accepts nothing else — and it is picked
 * from the doctor's published slots, so it is sent back exactly as the API
 * issued it, on the hospital's clock.
 */
export function RescheduleAppointmentDialog({
  appointment,
  disabled,
}: {
  appointment: AppointmentSummary
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          disabled={disabled}
          aria-label={`Reschedule appointment for ${appointment.patient_name}`}
        >
          <CalendarClock className="size-4" /> Reschedule
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Reschedule appointment</DialogTitle>
          <DialogDescription>
            {appointment.patient_name} with {appointment.doctor_name}.
          </DialogDescription>
        </DialogHeader>
        <RescheduleBody appointment={appointment} onDone={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  )
}
