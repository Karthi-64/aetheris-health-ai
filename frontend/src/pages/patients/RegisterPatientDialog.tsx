import { type ReactNode, useState } from 'react'
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
import { ApiError } from '@/api/types'
import { GENDER_LABELS, useCreatePatient, type Gender } from '@/api/patients'

/**
 * Registers a patient against `POST /api/v1/patients`
 * (`docs/18-API_CONTRACTS.md` §2.3).
 *
 * The form collects what the backend actually accepts: a first and last name
 * rather than one `name` field, a date of birth rather than an age (the API
 * computes and returns `age`), and no department — patients are not assigned to
 * one. The MRN is generated server-side and cannot be supplied.
 */

const GENDERS: Gender[] = ['male', 'female', 'other', 'unspecified']

const BLOOD_GROUPS = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'] as const

/** Backend rule: date of birth cannot be in the future, and age cannot exceed 130. */
const MAX_AGE_YEARS = 130

function isPlausibleBirthDate(value: string): boolean {
  const dob = new Date(value)
  if (Number.isNaN(dob.getTime())) return false
  const today = new Date()
  if (dob > today) return false
  const oldest = new Date()
  oldest.setFullYear(oldest.getFullYear() - MAX_AGE_YEARS)
  return dob >= oldest
}

const schema = z.object({
  first_name: z.string().trim().min(1, 'First name is required').max(100),
  last_name: z.string().trim().min(1, 'Last name is required').max(100),
  date_of_birth: z
    .string()
    .min(1, 'Date of birth is required')
    .refine(isPlausibleBirthDate, 'Enter a date in the past, within the last 130 years'),
  gender: z.enum(['male', 'female', 'other', 'unspecified'], { message: 'Select a gender' }),
  // Optional, but must be E.164-able if given — the backend normalizes
  // `9876543210` to `+919876543210` and rejects anything it cannot parse.
  phone: z
    .string()
    .trim()
    .refine((v) => v === '' || /^\+?[0-9\s-]{7,20}$/.test(v), 'Enter a valid phone number')
    .optional(),
  email: z
    .string()
    .trim()
    .refine((v) => v === '' || /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(v), 'Enter a valid email')
    .optional(),
  blood_group: z.string().optional(),
})

type FormValues = z.input<typeof schema>

export function RegisterPatientDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  const createPatient = useCreatePatient()

  const {
    register,
    control,
    handleSubmit,
    reset,
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
    const parsed = schema.parse(values)
    try {
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
      // The backend's message is written for end users, so show it rather than
      // replacing it with a generic failure.
      toast.error(
        err instanceof ApiError ? err.message : 'Could not register the patient. Please try again.',
      )
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
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

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
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
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createPatient.isPending}>
              {createPatient.isPending && <Loader2 className="size-4 animate-spin" />}
              Register
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
