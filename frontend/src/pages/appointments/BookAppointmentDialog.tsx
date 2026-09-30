import { type ReactNode, useEffect, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Check, Loader2, Search, X } from 'lucide-react'
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { usePatients, type PatientSummary } from '@/api/patients'
import { useDoctors } from '@/api/doctors'
import { useBookAppointment, type AppointmentType, type BookAppointmentInput } from '@/api/appointments'
import { ApiError } from '@/api/types'
import { cn } from '@/lib/utils'

const TYPES: { value: AppointmentType; label: string }[] = [
  { value: 'new', label: 'New' },
  { value: 'follow_up', label: 'Follow-up' },
  { value: 'walk_in', label: 'Walk-in' },
  { value: 'emergency', label: 'Emergency' },
]

const DURATIONS = [15, 30, 45, 60]

const schema = z.object({
  patient_id: z.string().min(1, 'Choose a patient'),
  doctor_id: z.string().min(1, 'Choose a doctor'),
  date: z.string().min(1, 'Pick a date'),
  time: z.string().min(1, 'Pick a time'),
  duration: z.coerce.number().int().positive(),
  type: z.enum(['new', 'follow_up', 'walk_in', 'emergency']),
  reason: z.string().max(2000).optional(),
})

type FormValues = z.input<typeof schema>

/** Searchable patient picker. Shows the chosen patient as a chip once selected. */
function PatientPicker({
  value,
  onChange,
  invalid,
  id,
}: {
  value: string
  onChange: (id: string) => void
  invalid?: boolean
  id?: string
}) {
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [chosen, setChosen] = useState<PatientSummary | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setQ(search.trim()), 250)
    return () => clearTimeout(t)
  }, [search])

  const { data } = usePatients({ q: q || undefined, page: 1, page_size: 8 })
  const results = data?.items ?? []

  if (value && chosen) {
    return (
      <div className="neo-pressed bg-surface flex items-center justify-between rounded-xl px-4 py-2.5">
        <span className="font-body text-body-sm text-on-surface">
          {chosen.full_name} <span className="font-mono text-outline">· {chosen.mrn}</span>
        </span>
        <button
          type="button"
          onClick={() => {
            setChosen(null)
            onChange('')
          }}
          aria-label="Change patient"
          className="text-outline hover:text-error transition-colors"
        >
          <X className="size-4" />
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="text-outline pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
        <input
          id={id}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search name, MRN, phone…"
          aria-invalid={invalid}
          className={cn(
            'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant w-full rounded-xl py-2.5 pr-4 pl-9 outline-none focus-visible:ring-2 focus-visible:ring-secondary',
            invalid && 'ring-2 ring-error',
          )}
        />
      </div>
      {q && results.length > 0 && (
        <ul className="neo-extruded bg-surface max-h-44 overflow-y-auto rounded-xl p-1">
          {results.map((p) => (
            <li key={p.id}>
              <button
                type="button"
                onClick={() => {
                  setChosen(p)
                  onChange(p.id)
                }}
                className="hover:bg-secondary/10 flex w-full items-center justify-between rounded-lg px-3 py-2 text-left transition-colors"
              >
                <span className="font-body text-body-sm text-on-surface">{p.full_name}</span>
                <span className="font-mono text-outline text-xs">{p.mrn}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {q && results.length === 0 && (
        <p className="font-body text-outline text-xs">No patients match “{q}”.</p>
      )}
    </div>
  )
}

/** Basic appointment booking: patient + doctor + time + type. Handles the 409 conflict. */
export function BookAppointmentDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [conflict, setConflict] = useState<string | null>(null)
  const book = useBookAppointment()
  const { data: doctorsPage } = useDoctors({ page: 1, page_size: 100 })
  const doctors = doctorsPage?.items ?? []

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { patient_id: '', doctor_id: '', date: '', time: '', duration: 15, type: 'new', reason: '' },
  })

  function closeAndReset(next: boolean) {
    setOpen(next)
    if (!next) {
      reset()
      setConflict(null)
    }
  }

  async function onSubmit(values: FormValues) {
    setConflict(null)
    const start = new Date(`${values.date}T${values.time}`)
    if (Number.isNaN(start.getTime())) {
      setError('date', { message: 'Enter a valid date and time' })
      return
    }
    const durationMin = Number(values.duration)
    const payload: BookAppointmentInput = {
      patient_id: values.patient_id,
      doctor_id: values.doctor_id,
      scheduled_start: start.toISOString(),
      scheduled_end: new Date(start.getTime() + durationMin * 60_000).toISOString(),
      type: values.type,
      ...(values.reason ? { reason: values.reason } : {}),
    }

    try {
      await book.mutateAsync(payload)
      toast.success('Appointment booked')
      closeAndReset(false)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflict('That doctor already has an appointment in this window. Pick another time.')
        return
      }
      if (err instanceof ApiError && Array.isArray(err.details)) {
        const fieldErrors = err.details as Array<{ field?: string; message?: string }>
        let mapped = false
        for (const fe of fieldErrors) {
          if (fe.field && fe.field in schema.shape) {
            setError(fe.field as keyof FormValues, { message: fe.message ?? 'Invalid value' })
            mapped = true
          }
        }
        if (!mapped) toast.error(err.message)
        return
      }
      toast.error(err instanceof ApiError ? err.message : 'Could not book the appointment.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={closeAndReset}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Book appointment</DialogTitle>
          <DialogDescription>Schedule a patient with a doctor.</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
          {conflict && (
            <Alert variant="warning" title="Time unavailable">
              {conflict}
            </Alert>
          )}

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

          <Controller
            control={control}
            name="doctor_id"
            render={({ field }) => (
              <Field label="Doctor" required error={errors.doctor_id?.message}>
                {(p) => (
                  <Select value={field.value} onValueChange={field.onChange}>
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

          <div className="grid grid-cols-2 gap-4">
            <Field label="Date" required error={errors.date?.message}>
              {(p) => <Input type="date" {...p} {...register('date')} />}
            </Field>
            <Field label="Time" required error={errors.time?.message}>
              {(p) => <Input type="time" {...p} {...register('time')} />}
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-4">
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
          </div>

          <Field label="Reason" hint="Optional" error={errors.reason?.message}>
            {(p) => <Textarea rows={2} placeholder="e.g. Persistent cough" {...p} {...register('reason')} />}
          </Field>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => closeAndReset(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={book.isPending}>
              {book.isPending ? <Loader2 className="size-4 animate-spin" /> : <Check className="size-4" />}
              Book
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
