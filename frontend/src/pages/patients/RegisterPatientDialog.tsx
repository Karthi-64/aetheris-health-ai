import { type ReactNode, useRef, useState } from 'react'
import { Controller, useForm } from 'react-hook-form'
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
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { apiErrorMessage, splitFieldErrors } from '@/lib/apiErrors'
import { GENDER_LABELS, useCreatePatient } from '@/api/patients'
import {
  BLOOD_GROUPS,
  GENDERS,
  birthDateField,
  emailField,
  firstNameField,
  genderField,
  lastNameField,
  phoneField,
} from './patientForm'

/**
 * Registers a patient against `POST /api/v1/patients`
 * (`docs/18-API_CONTRACTS.md` §2.3).
 *
 * The form collects what the backend actually accepts: a first and last name
 * rather than one `name` field, a date of birth rather than an age (the API
 * computes and returns `age`), and no department — patients are not assigned to
 * one. The MRN is generated server-side and cannot be supplied.
 */

const schema = z.object({
  first_name: firstNameField,
  last_name: lastNameField,
  date_of_birth: birthDateField,
  gender: genderField,
  phone: phoneField.optional(),
  email: emailField.optional(),
  blood_group: z.string().optional(),
})

type FormValues = z.input<typeof schema>

export function RegisterPatientDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  // `isPending` only disables the button after a re-render; this also stops a
  // second submit (double click, Enter held down) fired before that happens —
  // each one would otherwise create its own patient record and MRN.
  const submitting = useRef(false)
  const createPatient = useCreatePatient()

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      first_name: '',
      last_name: '',
      date_of_birth: '',
      phone: '',
      email: '',
      blood_group: '',
    },
  })

  async function onSubmit(values: FormValues) {
    if (submitting.current) return
    submitting.current = true
    setNotice(null)
    try {
      // Inside the try so that a throw here still releases the guard below.
      const parsed = schema.parse(values)
      const created = await createPatient.mutateAsync({
        first_name: parsed.first_name,
        last_name: parsed.last_name,
        date_of_birth: parsed.date_of_birth,
        gender: parsed.gender,
        // Omit rather than send an empty string: the backend treats a blank
        // optional as absent, but a malformed one as a validation error.
        ...(parsed.phone ? { phone: parsed.phone } : {}),
        ...(parsed.email ? { email: parsed.email } : {}),
        ...(parsed.blood_group ? { blood_group: parsed.blood_group } : {}),
      })
      toast.success(`Registered ${created.full_name} · ${created.mrn}`)
      reset()
      setOpen(false)
    } catch (err) {
      // A 422 names the fields it rejects: each goes under its input, and one
      // this form does not collect goes above the form rather than being lost.
      const { onFields, other } = splitFieldErrors(err, (field) => field in schema.shape)
      onFields.forEach((fe, index) =>
        setError(fe.field as keyof FormValues, { message: fe.message }, { shouldFocus: index === 0 }),
      )
      if (other.length > 0) setNotice(other.join(' '))
      if (onFields.length > 0 || other.length > 0) {
        toast.error("Couldn't register the patient. Check the highlighted fields.")
        return
      }
      // The API's sentence for a refusal it explains; never transport text.
      toast.error(apiErrorMessage(err, 'Could not register the patient. Please try again.'))
    } finally {
      submitting.current = false
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        setNotice(null)
        if (!next) reset()
      }}
    >
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Register patient</DialogTitle>
          <DialogDescription>
            Create a new patient record. The Medical Record Number is generated automatically.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-4" noValidate>
          {notice && (
            <Alert variant="error" title="Couldn't register the patient">
              {notice}
            </Alert>
          )}
          <div className="grid grid-cols-2 gap-4">
            <Field label="First name" required error={errors.first_name?.message}>
              {(p) => <Input placeholder="e.g. Ananya" {...p} {...register('first_name')} />}
            </Field>

            <Field label="Last name" required error={errors.last_name?.message}>
              {(p) => <Input placeholder="e.g. Rao" {...p} {...register('last_name')} />}
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <Field label="Date of birth" required error={errors.date_of_birth?.message}>
              {(p) => <Input type="date" {...p} {...register('date_of_birth')} />}
            </Field>

            <Controller
              control={control}
              name="gender"
              render={({ field }) => (
                <Field label="Gender" required error={errors.gender?.message}>
                  {(p) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id={p.id} aria-invalid={p['aria-invalid']}>
                        <SelectValue placeholder="Select" />
                      </SelectTrigger>
                      <SelectContent>
                        {GENDERS.map((g) => (
                          <SelectItem key={g} value={g}>
                            {GENDER_LABELS[g]}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </Field>
              )}
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <Field label="Phone" error={errors.phone?.message}>
              {(p) => <Input placeholder="+91 98123 45678" {...p} {...register('phone')} />}
            </Field>

            <Controller
              control={control}
              name="blood_group"
              render={({ field }) => (
                <Field label="Blood group" error={errors.blood_group?.message}>
                  {(p) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id={p.id} aria-invalid={p['aria-invalid']}>
                        <SelectValue placeholder="Select" />
                      </SelectTrigger>
                      <SelectContent>
                        {BLOOD_GROUPS.map((b) => (
                          <SelectItem key={b} value={b}>
                            {b}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </Field>
              )}
            />
          </div>

          <Field label="Email" error={errors.email?.message}>
            {(p) => (
              <Input type="email" placeholder="ananya@example.com" {...p} {...register('email')} />
            )}
          </Field>

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setOpen(false)
                setNotice(null)
              }}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={createPatient.isPending} aria-busy={createPatient.isPending}>
              {createPatient.isPending && <Loader2 className="size-4 animate-spin" />}
              Register
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
