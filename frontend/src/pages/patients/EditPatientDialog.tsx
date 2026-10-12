import type { ReactNode } from 'react'
import { useFormContext } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Pencil } from 'lucide-react'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { FormDialog } from '@/components/forms/FormDialog'
import { FormRefusal } from '@/components/forms/formRefusal'
import {
  GENDER_LABELS,
  useFetchCurrentPatient,
  useUpdatePatient,
  type Patient,
} from '@/api/patients'
import { PatientHistoryFields } from './PatientHistoryFields'
import { SelectField, type SelectOption } from './SelectField'
import {
  NOT_RECORDED,
  changedFields,
  editPatientSchema,
  overwrittenParts,
  replacesWholeParts,
  toFormValues,
  type EditPatientValues,
} from './editPatient'
import { BLOOD_GROUPS, GENDERS } from './patientForm'

const GENDER_OPTIONS: SelectOption[] = GENDERS.map((g) => ({ value: g, label: GENDER_LABELS[g] }))

const BLOOD_GROUP_OPTIONS: SelectOption[] = [
  { value: NOT_RECORDED, label: 'Not recorded' },
  ...BLOOD_GROUPS.map((b) => ({ value: b, label: b })),
]

function Section({
  title,
  hint,
  error,
  children,
}: {
  title: string
  hint?: string
  /** An error about the group as a whole, as opposed to one of its fields. */
  error?: string
  children: ReactNode
}) {
  return (
    <fieldset className="border-outline-variant/30 min-w-0 space-y-3 border-t pt-4">
      <legend className="font-display text-primary pr-3 text-base font-bold">{title}</legend>
      {hint && <p className="font-body text-outline text-xs">{hint}</p>}
      {error && (
        <p className="font-body text-error text-xs" role="alert">
          {error}
        </p>
      )}
      {children}
    </fieldset>
  )
}

function EditPatientFields({ mrn }: { mrn: string }) {
  const {
    register,
    formState: { errors },
  } = useFormContext<EditPatientValues>()

  return (
    <>
      <div className="neo-pressed bg-surface rounded-xl px-4 py-3">
        <dl className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <dt className="font-label text-label-caps text-on-surface-variant">Medical record number</dt>
          <dd className="text-primary font-mono text-sm">{mrn}</dd>
        </dl>
        <p className="font-body text-outline mt-1 text-xs">
          Assigned at registration. It can't be changed.
        </p>
      </div>

      <Section title="Identity">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="First name" required error={errors.first_name?.message}>
            {(p) => <Input {...p} {...register('first_name')} />}
          </Field>
          <Field label="Last name" required error={errors.last_name?.message}>
            {(p) => <Input {...p} {...register('last_name')} />}
          </Field>
          <Field
            label="Date of birth"
            required
            hint="Age is worked out from this"
            error={errors.date_of_birth?.message}
          >
            {(p) => <Input type="date" {...p} {...register('date_of_birth')} />}
          </Field>
          <SelectField<EditPatientValues> name="gender" label="Gender" required options={GENDER_OPTIONS} />
          <SelectField<EditPatientValues>
            name="blood_group"
            label="Blood group"
            options={BLOOD_GROUP_OPTIONS}
          />
          <Field label="Marital status" error={errors.marital_status?.message}>
            {(p) => <Input {...p} {...register('marital_status')} />}
          </Field>
          <Field label="Occupation" className="sm:col-span-2" error={errors.occupation?.message}>
            {(p) => <Input {...p} {...register('occupation')} />}
          </Field>
        </div>
      </Section>

      <Section title="Contact">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Phone" error={errors.phone?.message}>
            {(p) => <Input placeholder="+91 98123 45678" {...p} {...register('phone')} />}
          </Field>
          <Field label="Email" error={errors.email?.message}>
            {(p) => <Input type="email" {...p} {...register('email')} />}
          </Field>
        </div>
      </Section>

      <Section
        title="Address"
        hint="Line 1, city and country are needed for an address. Clear every field to remove it."
        error={errors.address?.message}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Line 1" className="sm:col-span-2" error={errors.address?.line1?.message}>
            {(p) => <Input {...p} {...register('address.line1')} />}
          </Field>
          <Field label="Line 2" className="sm:col-span-2" error={errors.address?.line2?.message}>
            {(p) => <Input {...p} {...register('address.line2')} />}
          </Field>
          <Field label="City" error={errors.address?.city?.message}>
            {(p) => <Input {...p} {...register('address.city')} />}
          </Field>
          <Field label="State" error={errors.address?.state?.message}>
            {(p) => <Input {...p} {...register('address.state')} />}
          </Field>
          <Field label="Postal code" error={errors.address?.postal_code?.message}>
            {(p) => <Input {...p} {...register('address.postal_code')} />}
          </Field>
          <Field
            label="Country"
            hint="Two-letter code, e.g. IN"
            error={errors.address?.country?.message}
          >
            {(p) => <Input className="uppercase" {...p} {...register('address.country')} />}
          </Field>
        </div>
      </Section>

      <Section
        title="Emergency contact"
        hint="A contact needs all three. Clear every field to remove them."
        error={errors.emergency_contact?.message}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" className="sm:col-span-2" error={errors.emergency_contact?.name?.message}>
            {(p) => <Input {...p} {...register('emergency_contact.name')} />}
          </Field>
          <Field label="Phone" error={errors.emergency_contact?.phone?.message}>
            {(p) => (
              <Input placeholder="+91 98123 45678" {...p} {...register('emergency_contact.phone')} />
            )}
          </Field>
          <Field label="Relationship" error={errors.emergency_contact?.relation?.message}>
            {(p) => (
              <Input placeholder="e.g. Spouse" {...p} {...register('emergency_contact.relation')} />
            )}
          </Field>
        </div>
      </Section>

      <Section title="Medical history">
        <PatientHistoryFields />
      </Section>

      <Section title="Notes">
        <Field
          label="Administrative notes"
          hint="Not clinical documentation"
          error={errors.notes?.message}
        >
          {(p) => <Textarea rows={3} {...p} {...register('notes')} />}
        </Field>
      </Section>
    </>
  )
}

const SAVE_FAILED = "Couldn't save the changes. Please try again."

/** The refusal for a save that would undo an edit somebody made since the form opened. */
function staleRecord(parts: string[]): FormRefusal {
  const changed = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts.at(-1)}` : parts[0]
  return new FormRefusal(
    `The ${changed} on this record changed after this form was opened, and saving would undo ` +
      'that change. Close the form and open it again to edit the current record.',
  )
}

/**
 * Edit a patient against `PATCH /api/v1/patients/{id}`.
 *
 * The API takes a partial update, rejects an empty one and rejects every key
 * that is not editable, so the body is never the record sent back: it holds
 * only what this user changed.
 */
export function EditPatientDialog({ patient }: { patient: Patient }) {
  const update = useUpdatePatient(patient.id)
  const fetchCurrent = useFetchCurrentPatient(patient.id)

  async function save(values: EditPatientValues, opened: EditPatientValues): Promise<string> {
    const body = changedFields(opened, values)
    // Retyping a value, or only adding spaces, dirties the form without
    // changing the record — and the API rejects an empty update.
    if (Object.keys(body).length === 0) return 'Nothing to save — the record is unchanged'
    // The form was filled from a cached record of any age, and a list or
    // nested object goes back whole. Look at what is stored before replacing
    // one, so an entry added since is not silently deleted.
    if (replacesWholeParts(body)) {
      const overwritten = overwrittenParts(body, opened, await fetchCurrent())
      if (overwritten.length > 0) throw staleRecord(overwritten)
    }
    const saved = await update.mutateAsync(body)
    return `Saved changes to ${saved.full_name}`
  }

  return (
    <FormDialog<EditPatientValues>
      trigger={
        <Button variant="outline" size="sm">
          <Pencil className="size-4" /> Edit patient
        </Button>
      }
      title="Edit patient"
      description={`Update the record for ${patient.full_name}. Only what you change is saved.`}
      resolver={zodResolver(editPatientSchema)}
      defaults={() => toFormValues(patient)}
      submitLabel="Save changes"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError={SAVE_FAILED}
      onSubmit={save}
    >
      {() => <EditPatientFields mrn={patient.mrn} />}
    </FormDialog>
  )
}
