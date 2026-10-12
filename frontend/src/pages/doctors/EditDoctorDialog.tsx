import { Controller, useFieldArray, useFormContext } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Pencil, Plus, X } from 'lucide-react'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Detail } from '@/components/ui/detail-card'
import { FormDialog } from '@/components/forms/FormDialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useUpdateDoctor, type Doctor } from '@/api/doctors'
import { useDepartments } from '@/api/departments'
import {
  EMPTY_QUALIFICATION,
  UNASSIGNED,
  changedKeys,
  doctorSchema,
  toFormValues,
  type DoctorFormValues,
} from './doctorForm'

function DepartmentField({ doctor }: { doctor: Doctor }) {
  const { control } = useFormContext<DoctorFormValues>()
  const { data: departments, isError } = useDepartments()
  // Only active departments are listed, and a doctor can still belong to one
  // that has been deactivated. It needs an option of its own, or the select
  // would open empty and a save could move the doctor without anyone choosing to.
  const unlisted =
    doctor.department_id !== null && !departments?.some((d) => d.id === doctor.department_id)
  const currentName = doctor.department_name ?? 'Current department'
  // Until the list has loaded, its absence from it says nothing.
  const currentLabel = departments ? `${currentName} (inactive)` : currentName

  return (
    <Controller
      control={control}
      name="department_id"
      render={({ field, fieldState }) => (
        <Field
          label="Department"
          error={fieldState.error?.message}
          hint={isError ? "The department list couldn't be loaded." : undefined}
        >
          {(p) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger
                ref={field.ref}
                id={p.id}
                aria-invalid={p['aria-invalid']}
                aria-describedby={p['aria-describedby']}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={UNASSIGNED}>Unassigned</SelectItem>
                {unlisted && doctor.department_id && (
                  <SelectItem value={doctor.department_id}>{currentLabel}</SelectItem>
                )}
                {departments?.map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </Field>
      )}
    />
  )
}

function QualificationRow({ index, onRemove }: { index: number; onRemove: () => void }) {
  const {
    register,
    formState: { errors },
  } = useFormContext<DoctorFormValues>()
  const rowErrors = errors.qualifications?.[index]
  const n = index + 1

  return (
    <li aria-label={`Qualification ${n}`} className="neo-pressed bg-surface flex items-start gap-2 rounded-xl p-4">
      <div className="grid flex-1 gap-3 sm:grid-cols-[1fr_1fr_6rem]">
        <Field label="Degree" required error={rowErrors?.degree?.message}>
          {(p) => <Input placeholder="e.g. MBBS" {...p} {...register(`qualifications.${index}.degree`)} />}
        </Field>
        <Field label="Institution" error={rowErrors?.institution?.message}>
          {(p) => <Input {...p} {...register(`qualifications.${index}.institution`)} />}
        </Field>
        <Field label="Year" error={rowErrors?.year?.message}>
          {(p) => (
            <Input
              inputMode="numeric"
              placeholder="YYYY"
              {...p}
              {...register(`qualifications.${index}.year`)}
            />
          )}
        </Field>
      </div>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        onClick={onRemove}
        aria-label={`Remove qualification ${n}`}
        className="text-outline hover:text-error mt-7"
      >
        <X className="size-4" />
      </Button>
    </li>
  )
}

/** The qualification list. Saved whole: the API replaces the list rather than patching rows. */
function QualificationsFields() {
  const {
    control,
    formState: { errors },
  } = useFormContext<DoctorFormValues>()
  const { fields, append, remove } = useFieldArray({ control, name: 'qualifications' })
  const listError = errors.qualifications?.message ?? errors.qualifications?.root?.message

  return (
    <fieldset className="min-w-0 space-y-3">
      <legend className="font-label text-label-caps text-on-surface-variant mb-2">Qualifications</legend>
      {fields.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">No qualifications listed.</p>
      ) : (
        <ul className="space-y-3">
          {fields.map((f, index) => (
            <QualificationRow key={f.id} index={index} onRemove={() => remove(index)} />
          ))}
        </ul>
      )}
      {listError && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}
      <Button type="button" variant="outline" size="sm" onClick={() => append({ ...EMPTY_QUALIFICATION })}>
        <Plus className="size-4" /> Add qualification
      </Button>
    </fieldset>
  )
}

/**
 * Edit a doctor's profile (`PATCH /doctors/{id}`). Only what was changed is
 * sent: the API rejects an empty body and any key it does not know, and takes
 * an unsent key to mean "leave as is".
 */
export function EditDoctorDialog({ doctor }: { doctor: Doctor }) {
  const update = useUpdateDoctor(doctor.id)

  return (
    <FormDialog<DoctorFormValues>
      trigger={
        <Button variant="outline" size="sm">
          <Pencil className="size-4" /> Edit
        </Button>
      }
      title="Edit doctor"
      description="Update the practice details shown on this doctor's profile."
      resolver={zodResolver(doctorSchema)}
      defaults={() => toFormValues(doctor)}
      submitLabel="Save changes"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't save the changes. Please try again."
      onSubmit={async (values, opened) => {
        const body = changedKeys(opened, values)
        // An edit can come to nothing once it is trimmed — a trailing space, say.
        if (Object.keys(body).length === 0) return 'Nothing to save — the profile is unchanged'
        await update.mutateAsync(body)
        return 'Doctor updated'
      }}
    >
      {({ register, formState: { errors } }) => (
        <>
          <div className="neo-pressed bg-surface space-y-3 rounded-xl p-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Detail label="Name" value={doctor.full_name} />
              <Detail label="Email" value={doctor.email} />
            </div>
            <p className="font-body text-outline text-xs">
              Name and email belong to the doctor's user account and can't be changed here.
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Specialization" required error={errors.specialization?.message}>
              {(p) => <Input {...p} {...register('specialization')} />}
            </Field>
            <Field label="Licence number" required error={errors.license_number?.message}>
              {(p) => <Input {...p} {...register('license_number')} />}
            </Field>
            <Field
              label="Consultation fee"
              required
              hint="Enter 0 for a free consultation"
              error={errors.consultation_fee?.message}
            >
              {(p) => <Input inputMode="decimal" {...p} {...register('consultation_fee')} />}
            </Field>
            <DepartmentField doctor={doctor} />
          </div>

          <QualificationsFields />

          <Field
            label="Languages"
            hint="Separate languages with commas, e.g. English, Hindi"
            error={errors.languages?.message}
          >
            {(p) => <Input {...p} {...register('languages')} />}
          </Field>
          <Field label="Bio" hint="Optional" error={errors.bio?.message}>
            {(p) => <Textarea rows={4} {...p} {...register('bio')} />}
          </Field>
        </>
      )}
    </FormDialog>
  )
}
