import { type ReactNode, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Controller, FormProvider, useForm, useWatch, type Path } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Loader2, Plus } from 'lucide-react'
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
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PatientPicker } from '@/components/patients/PatientPicker'
import { useAppointments } from '@/api/appointments'
import { useCreateInvoice } from '@/api/billing'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDate, formatTime } from '@/lib/format'
import { InvoiceLinesFields } from './InvoiceLinesFields'
import { EMPTY_LINE, lineSchema, toLineInput } from './invoiceLines'
import { billingErrorMessage, splitFieldErrors } from './billing'

const NO_APPOINTMENT = 'none'

const schema = z.object({
  patient_id: z.string().min(1, 'Choose a patient'),
  appointment_id: z.string(),
  items: z.array(lineSchema).max(200, 'An invoice can have at most 200 items'),
  notes: z.string().max(2000, 'Keep notes under 2000 characters'),
})

type FormValues = z.infer<typeof schema>

/** Fields a 422 can name that this form shows: the top-level ones and any line field. */
const isFormField = (field: string) =>
  ['patient_id', 'appointment_id', 'notes'].includes(field) ||
  /^items\.\d+\.(service_id|description|quantity|unit_price)$/.test(field)

/** The visits an invoice can be linked to. Only rendered for users who can read appointments. */
function AppointmentSelect({
  patientId,
  value,
  onChange,
  id,
  invalid,
}: {
  patientId: string
  value: string
  onChange: (value: string) => void
  id: string
  invalid: boolean
}) {
  const { data } = useAppointments(
    { patient_id: patientId, page_size: 50 },
    { enabled: !!patientId },
  )
  const appointments = patientId ? (data?.items ?? []) : []

  return (
    <Select value={value} onValueChange={onChange} disabled={!patientId}>
      <SelectTrigger id={id} aria-invalid={invalid}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={NO_APPOINTMENT}>Not linked to an appointment</SelectItem>
        {appointments.map((a) => (
          <SelectItem key={a.id} value={a.id}>
            {formatDate(a.scheduled_start)}, {formatTime(a.scheduled_start)} · {a.doctor_name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/**
 * Create a draft invoice (docs/18-API_CONTRACTS.md §6.4). Nothing is charged
 * yet: the draft opens on its own page, where the server's totals are shown
 * and it can be discounted and issued.
 *
 * Pass `patient` to raise the invoice from a patient's record.
 */
export function CreateInvoiceDialog({
  trigger,
  patient,
}: {
  trigger: ReactNode
  patient?: { id: string; full_name: string; mrn: string }
}) {
  const navigate = useNavigate()
  const { can } = usePermissions()
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const submitting = useRef(false)
  const create = useCreateInvoice()

  const defaults: FormValues = {
    patient_id: patient?.id ?? '',
    appointment_id: NO_APPOINTMENT,
    items: [{ ...EMPTY_LINE }],
    notes: '',
  }
  const form = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: defaults })
  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    setValue,
    formState: { errors },
  } = form
  const patientId = useWatch({ control, name: 'patient_id' })

  function onOpenChange(next: boolean) {
    setOpen(next)
    if (!next) {
      reset(defaults)
      setNotice(null)
    }
  }

  async function onSubmit(values: FormValues) {
    if (submitting.current) return
    setNotice(null)
    submitting.current = true
    try {
      const invoice = await create.mutateAsync({
        patient_id: values.patient_id,
        ...(values.appointment_id !== NO_APPOINTMENT ? { appointment_id: values.appointment_id } : {}),
        items: values.items.map(toLineInput),
        ...(values.notes.trim() ? { notes: values.notes.trim() } : {}),
      })
      toast.success('Draft invoice created')
      onOpenChange(false)
      navigate(`/billing/${invoice.id}`)
    } catch (err) {
      const { onFields, other } = splitFieldErrors(err, isFormField)
      for (const fe of onFields) setError(fe.field as Path<FormValues>, { message: fe.message })
      if (onFields.length === 0 || other.length > 0) {
        setNotice(
          other.length > 0
            ? other.join(' ')
            : billingErrorMessage(err, "Couldn't create the invoice. Please try again."),
        )
      }
    } finally {
      submitting.current = false
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>New invoice</DialogTitle>
          <DialogDescription>
            Creates a draft. You can review the totals and issue it on the next screen.
          </DialogDescription>
        </DialogHeader>

        <FormProvider {...form}>
          <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-4" noValidate>
            {notice && (
              <Alert variant="error" title="Couldn't create the invoice">
                {notice}
              </Alert>
            )}

            {patient ? (
              <Field label="Patient">
                {(p) => (
                  <p id={p.id} className="neo-pressed bg-surface font-body text-body-sm text-on-surface rounded-xl px-4 py-2.5">
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
                        onChange={(id) => {
                          field.onChange(id)
                          // An appointment belongs to one patient (422 otherwise).
                          setValue('appointment_id', NO_APPOINTMENT)
                        }}
                        invalid={p['aria-invalid']}
                      />
                    )}
                  </Field>
                )}
              />
            )}

            {can('appointment.read') && (
              <Controller
                control={control}
                name="appointment_id"
                render={({ field }) => (
                  <Field
                    label="Appointment"
                    hint="Optional. An appointment can have only one invoice."
                    error={errors.appointment_id?.message}
                  >
                    {(p) => (
                      <AppointmentSelect
                        id={p.id}
                        invalid={p['aria-invalid']}
                        patientId={patientId}
                        value={field.value}
                        onChange={field.onChange}
                      />
                    )}
                  </Field>
                )}
              />
            )}

            <div className="space-y-2">
              <p className="font-label text-label-caps text-on-surface-variant">Charges</p>
              <InvoiceLinesFields />
              {errors.items?.root?.message && (
                <p className="font-body text-error text-xs" role="alert">
                  {errors.items.root.message}
                </p>
              )}
            </div>

            <Field label="Notes" hint="Optional" error={errors.notes?.message}>
              {(p) => <Textarea rows={2} {...p} {...register('notes')} />}
            </Field>

            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={create.isPending} aria-busy={create.isPending}>
                {create.isPending ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
                {create.isPending ? 'Creating…' : 'Create draft'}
              </Button>
            </DialogFooter>
          </form>
        </FormProvider>
      </DialogContent>
    </Dialog>
  )
}
