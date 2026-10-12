import { type ReactNode, useRef, useState } from 'react'
import { Controller, useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Check, Loader2 } from 'lucide-react'
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
import { Checkbox } from '@/components/ui/checkbox'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { PatientPicker } from '@/components/patients/PatientPicker'
import { useDoctors, type DoctorSlot } from '@/api/doctors'
import { useBookAppointment, type AppointmentType, type BookAppointmentInput } from '@/api/appointments'
import { ApiError } from '@/api/types'
import { apiErrorMessage } from '@/lib/apiErrors'
import { usePermissions } from '@/hooks/usePermissions'
import { SlotPicker } from './SlotPicker'
import { SlotSuggestion } from './SlotSuggestion'

const TYPES: { value: AppointmentType; label: string }[] = [
  { value: 'new', label: 'New' },
  { value: 'follow_up', label: 'Follow-up' },
  { value: 'walk_in', label: 'Walk-in' },
  { value: 'emergency', label: 'Emergency' },
]

const DURATIONS = [15, 30, 45, 60]

const schema = z
  .object({
    patient_id: z.string().min(1, 'Choose a patient'),
    doctor_id: z.string().min(1, 'Choose a doctor'),
    date: z.string().min(1, 'Pick a date'),
    /** True when the time is typed rather than picked from the doctor's slots. */
    manual: z.boolean(),
    /** The chosen slot's `start`, exactly as the API sent it. */
    slot: z.string(),
    time: z.string(),
    duration: z.coerce.number().int().positive(),
    type: z.enum(['new', 'follow_up', 'walk_in', 'emergency']),
    // Module spec §11: the API rejects a reason over 500 characters.
    reason: z.string().max(500, 'Keep the reason under 500 characters').optional(),
  })
  .superRefine((values, ctx) => {
    if (values.manual) {
      if (!values.time) ctx.addIssue({ code: 'custom', path: ['time'], message: 'Pick a time' })
    } else if (values.date && values.doctor_id && !values.slot) {
      ctx.addIssue({ code: 'custom', path: ['slot'], message: 'Pick a time slot' })
    }
  })

type FormValues = z.input<typeof schema>

/** Where a field named in a 422 from the API is shown on this form. */
function serverField(field: string, manual: boolean): keyof FormValues | undefined {
  if (field === 'scheduled_start' || field === 'scheduled_end') return manual ? 'time' : 'slot'
  if (field === 'patient_id' || field === 'doctor_id' || field === 'type' || field === 'reason') {
    return field
  }
  return undefined
}

interface Notice {
  variant: 'warning' | 'error'
  title: string
  body: string
}

/**
 * Book an appointment: patient → doctor → day → one of the doctor's slots.
 *
 * A slot's own `start` and `end` are what is booked, so the appointment lands
 * in the hospital's timezone whatever the viewer's is, and inside the doctor's
 * availability — which the API requires of anyone without
 * `appointment.book_override`. Someone who holds that override may type a time
 * instead; so does anyone who cannot read availability at all.
 *
 * Where it is available, an AI suggestion of one of those slots can be asked
 * for. It only selects a slot in the picker: Book is still what books.
 *
 * Pass `patient` to book from a patient's record.
 */
export function BookAppointmentDialog({
  trigger,
  patient,
}: {
  trigger: ReactNode
  patient?: { id: string; full_name: string; mrn: string }
}) {
  const { can } = usePermissions()
  const canSeeSlots = can('doctor.availability.read')
  const canTypeTime = !canSeeSlots || can('appointment.book_override')

  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  // The server refused an AI suggestion in a way asking again cannot change.
  // Kept here, outside the keyed control, for as long as the dialog is open.
  const [suggestionRefused, setSuggestionRefused] = useState(false)
  // `isPending` only disables the button after a re-render; this also stops a
  // second submit (double click, Enter held down) fired before that happens.
  const submitting = useRef(false)
  // The slot's end, kept beside the form: `slot` holds only its start.
  const slotEnd = useRef('')
  const book = useBookAppointment()
  const { data: doctorsPage } = useDoctors({ page: 1, page_size: 100 })
  const doctors = doctorsPage?.items ?? []

  const defaults: FormValues = {
    patient_id: patient?.id ?? '',
    doctor_id: '',
    date: '',
    manual: !canSeeSlots,
    slot: '',
    time: '',
    duration: 15,
    type: 'new',
    reason: '',
  }
  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    setValue,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: defaults })
  const [doctorId, date, manual, patientId, slot] = useWatch({
    control,
    name: ['doctor_id', 'date', 'manual', 'patient_id', 'slot'],
  })

  function closeAndReset(next: boolean) {
    setOpen(next)
    if (!next) {
      reset(defaults)
      slotEnd.current = ''
      setNotice(null)
      setSuggestionRefused(false)
    }
  }

  /** A slot belongs to one doctor on one day, so changing either drops it. */
  function clearSlot() {
    setValue('slot', '')
    slotEnd.current = ''
  }

  /** Select a slot the AI suggested, exactly as a click in the picker would. */
  function applySuggestedSlot(suggested: DoctorSlot) {
    slotEnd.current = suggested.end
    setValue('slot', suggested.start, { shouldValidate: true, shouldDirty: true })
  }

  async function onSubmit(values: FormValues) {
    if (submitting.current) return
    setNotice(null)

    let scheduledStart: string
    let scheduledEnd: string
    if (values.manual) {
      const start = new Date(`${values.date}T${values.time}`)
      if (Number.isNaN(start.getTime())) {
        setError('date', { message: 'Enter a valid date and time' })
        return
      }
      scheduledStart = start.toISOString()
      scheduledEnd = new Date(start.getTime() + Number(values.duration) * 60_000).toISOString()
    } else {
      scheduledStart = values.slot
      scheduledEnd = slotEnd.current
    }

    const payload: BookAppointmentInput = {
      patient_id: values.patient_id,
      doctor_id: values.doctor_id,
      scheduled_start: scheduledStart,
      scheduled_end: scheduledEnd,
      type: values.type,
      ...(values.reason ? { reason: values.reason } : {}),
    }

    submitting.current = true
    try {
      await book.mutateAsync(payload)
      toast.success('Appointment booked')
      closeAndReset(false)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // The slots are refetched by the hook; the taken one must be re-picked.
        clearSlot()
        setNotice({
          variant: 'warning',
          title: 'Time unavailable',
          body: 'That doctor already has an appointment in this window. Pick another time.',
        })
        return
      }
      if (err instanceof ApiError && Array.isArray(err.details)) {
        const fieldErrors = err.details as Array<{ field?: string; message?: string }>
        const unmapped: string[] = []
        for (const fe of fieldErrors) {
          const target = fe.field ? serverField(fe.field, values.manual) : undefined
          if (target) setError(target, { message: fe.message ?? 'Invalid value' })
          else if (fe.message) unmapped.push(fe.message)
        }
        // Errors about the whole request (the booking window, a header) have no
        // field to sit under, so they are shown above the form instead.
        if (unmapped.length > 0) {
          setNotice({ variant: 'error', title: "Couldn't book", body: unmapped.join(' ') })
        }
        return
      }
      // 400 is a business rule the user can act on, e.g. outside availability.
      if (err instanceof ApiError && (err.status === 400 || err.status === 422)) {
        setNotice({ variant: 'error', title: "Couldn't book", body: err.message })
        return
      }
      toast.error(apiErrorMessage(err, 'Could not book the appointment. Please try again.'))
    } finally {
      submitting.current = false
    }
  }

  return (
    <Dialog open={open} onOpenChange={closeAndReset}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Book appointment</DialogTitle>
          <DialogDescription>Schedule a patient with a doctor.</DialogDescription>
        </DialogHeader>

        <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-4" noValidate>
          {notice && (
            <Alert variant={notice.variant} title={notice.title}>
              {notice.body}
            </Alert>
          )}

          {patient ? (
            <Field label="Patient">
              {(p) => (
                <p
                  id={p.id}
                  className="neo-pressed bg-surface font-body text-body-sm text-on-surface rounded-xl px-4 py-2.5"
                >
                  {patient.full_name} <span className="font-mono text-outline">· {patient.mrn}</span>
                </p>
              )}
            </Field>
          ) : (
            <Controller
              control={control}
              name="patient_id"
              render={({ field }) => (
                <Field label="Patient" required error={errors.patient_id?.message}>
                  {(p) => (
                    <PatientPicker
                      id={p.id}
                      value={field.value}
                      onChange={field.onChange}
                      invalid={p['aria-invalid']}
                    />
                  )}
                </Field>
              )}
            />
          )}

          <div className="grid grid-cols-2 gap-4">
            <Controller
              control={control}
              name="doctor_id"
              render={({ field }) => (
                <Field label="Doctor" required error={errors.doctor_id?.message}>
                  {(p) => (
                    <Select
                      value={field.value}
                      onValueChange={(v) => {
                        field.onChange(v)
                        clearSlot()
                      }}
                    >
                      <SelectTrigger id={p.id} aria-invalid={p['aria-invalid']}>
                        <SelectValue placeholder="Select a doctor" />
                      </SelectTrigger>
                      <SelectContent>
                        {doctors.map((d) => (
                          <SelectItem key={d.id} value={d.id}>
                            {d.full_name} · {d.specialization}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </Field>
              )}
            />
            <Field label="Date" required error={errors.date?.message}>
              {(p) => <Input type="date" {...p} {...register('date', { onChange: clearSlot })} />}
            </Field>
          </div>

          {canSeeSlots && canTypeTime && (
            <Controller
              control={control}
              name="manual"
              render={({ field }) => (
                <label className="font-body text-body-sm text-on-surface flex items-center gap-2">
                  <Checkbox
                    checked={field.value}
                    onCheckedChange={(v) => {
                      field.onChange(v === true)
                      clearSlot()
                    }}
                  />
                  Enter a time outside the doctor's published slots
                </label>
              )}
            />
          )}

          {manual ? (
            <div className="grid grid-cols-2 gap-4">
              <Field label="Time" required error={errors.time?.message}>
                {(p) => <Input type="time" {...p} {...register('time')} />}
              </Field>
              <Controller
                control={control}
                name="duration"
                render={({ field }) => (
                  <Field label="Duration" required error={errors.duration?.message}>
                    {(p) => (
                      <Select value={String(field.value)} onValueChange={(v) => field.onChange(Number(v))}>
                        <SelectTrigger id={p.id} aria-invalid={p['aria-invalid']}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {DURATIONS.map((d) => (
                            <SelectItem key={d} value={String(d)}>
                              {d} min
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  </Field>
                )}
              />
            </div>
          ) : doctorId && date ? (
            <>
              {/* Before the picker, so a keyboard user reaches it ahead of the
                  slot buttons. Remounted — dropping any suggestion and any
                  answer still in flight — when patient, doctor or date changes. */}
              <SlotSuggestion
                key={`${patientId}|${doctorId}|${date}`}
                patientId={patientId}
                doctorId={doctorId}
                date={date}
                selectedStart={slot}
                onUse={applySuggestedSlot}
                refused={suggestionRefused}
                onRefused={() => setSuggestionRefused(true)}
              />
              <Controller
                control={control}
                name="slot"
                render={({ field }) => (
                  <SlotPicker
                    doctorId={doctorId}
                    date={date}
                    value={field.value}
                    error={errors.slot?.message}
                    onChange={(slot) => {
                      slotEnd.current = slot.end
                      field.onChange(slot.start)
                    }}
                  />
                )}
              />
            </>
          ) : (
            <p className="font-body text-body-sm text-on-surface-variant">
              Choose a doctor and a date to see the available time slots.
            </p>
          )}

          <Controller
            control={control}
            name="type"
            render={({ field }) => (
              <Field label="Type" required error={errors.type?.message}>
                {(p) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id={p.id} aria-invalid={p['aria-invalid']}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {TYPES.map((t) => (
                        <SelectItem key={t.value} value={t.value}>
                          {t.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </Field>
            )}
          />

          <Field label="Reason" hint="Optional" error={errors.reason?.message}>
            {(p) => <Textarea rows={2} placeholder="e.g. Persistent cough" {...p} {...register('reason')} />}
          </Field>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => closeAndReset(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={book.isPending} aria-busy={book.isPending}>
              {book.isPending ? <Loader2 className="size-4 animate-spin" /> : <Check className="size-4" />}
              {book.isPending ? 'Booking…' : 'Book'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
