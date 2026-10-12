import { type ReactNode, type Ref, useId, useRef } from 'react'
import { flushSync } from 'react-dom'
import { useFieldArray, useFormContext } from 'react-hook-form'
import { Plus, X } from 'lucide-react'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { SelectField, type SelectOption } from './SelectField'
import {
  EMPTY_ALLERGY,
  EMPTY_CONDITION,
  EMPTY_MEDICATION,
  SEVERITIES,
  type EditPatientValues,
} from './editPatient'

const SEVERITY_OPTIONS: SelectOption[] = SEVERITIES.map((severity) => ({
  value: severity,
  label: severity[0].toUpperCase() + severity.slice(1),
}))

/**
 * Removing a row unmounts the button that was pressed, and the dialog then
 * puts focus back on itself — the next Tab starts again from the top of the
 * form. Keep the user in the list instead: on the row that took the removed
 * one's place, else the row before it, else the list's Add button.
 */
function useRowRemoval(remove: (index: number) => void) {
  const removeButtons = useRef<(HTMLButtonElement | null)[]>([])
  const addButton = useRef<HTMLButtonElement>(null)

  return {
    addButton,
    removeButton: (index: number) => (button: HTMLButtonElement | null) => {
      removeButtons.current[index] = button
    },
    removeRow(index: number) {
      // Rendered at once, so the rows are renumbered before focus moves to one.
      flushSync(() => remove(index))
      const next = removeButtons.current[index] ?? removeButtons.current[index - 1] ?? addButton.current
      next?.focus()
    },
  }
}

function HistoryList({
  title,
  emptyText,
  addLabel,
  addRef,
  onAdd,
  error,
  children,
}: {
  title: string
  emptyText: string
  addLabel: string
  addRef: Ref<HTMLButtonElement>
  onAdd: () => void
  /** An error about the list as a whole, as opposed to one of its rows. */
  error?: string
  children: ReactNode[]
}) {
  const headingId = useId()
  return (
    <div role="group" aria-labelledby={headingId} className="space-y-3">
      <h3 id={headingId} className="font-label text-label-caps text-on-surface-variant">
        {title}
      </h3>
      {children.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">{emptyText}</p>
      ) : (
        <ul className="space-y-3">{children}</ul>
      )}
      {error && (
        <p className="font-body text-error text-xs" role="alert">
          {error}
        </p>
      )}
      <Button ref={addRef} type="button" variant="outline" size="sm" onClick={onAdd}>
        <Plus className="size-4" /> {addLabel}
      </Button>
    </div>
  )
}

/** One entry. `label` ("Allergy 2") names the row, its first field and its remove button. */
function HistoryRow({
  label,
  removeRef,
  onRemove,
  children,
}: {
  label: string
  removeRef: Ref<HTMLButtonElement>
  onRemove: () => void
  children: ReactNode
}) {
  return (
    <li aria-label={label} className="neo-pressed bg-surface flex items-start gap-3 rounded-xl p-4">
      <div className="grid min-w-0 flex-1 gap-3 sm:grid-cols-2">{children}</div>
      <button
        ref={removeRef}
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${label.toLowerCase()}`}
        className="text-outline hover:text-error focus-visible:ring-secondary mt-7 rounded-lg p-1 transition-colors focus-visible:ring-2 focus-visible:outline-none"
      >
        <X className="size-4" />
      </button>
    </li>
  )
}

function AllergyList() {
  const {
    control,
    register,
    formState: { errors },
  } = useFormContext<EditPatientValues>()
  const { fields, append, remove } = useFieldArray({ control, name: 'allergies' })
  const rows = useRowRemoval(remove)

  return (
    <HistoryList
      title="Allergies"
      emptyText="No allergies recorded."
      addLabel="Add allergy"
      addRef={rows.addButton}
      onAdd={() => append({ ...EMPTY_ALLERGY })}
      error={errors.allergies?.root?.message ?? errors.allergies?.message}
    >
      {fields.map((field, index) => {
        const label = `Allergy ${index + 1}`
        const rowErrors = errors.allergies?.[index]
        return (
          <HistoryRow
            key={field.id}
            label={label}
            removeRef={rows.removeButton(index)}
            onRemove={() => rows.removeRow(index)}
          >
            <Field label={label} required error={rowErrors?.name?.message}>
              {(p) => (
                <Input placeholder="e.g. Penicillin" {...p} {...register(`allergies.${index}.name`)} />
              )}
            </Field>
            <SelectField<EditPatientValues>
              name={`allergies.${index}.severity`}
              label="Severity"
              options={SEVERITY_OPTIONS}
            />
            <Field label="Reaction" error={rowErrors?.reaction?.message}>
              {(p) => (
                <Input placeholder="e.g. Hives" {...p} {...register(`allergies.${index}.reaction`)} />
              )}
            </Field>
            <Field label="Noted on" error={rowErrors?.noted_on?.message}>
              {(p) => <Input type="date" {...p} {...register(`allergies.${index}.noted_on`)} />}
            </Field>
          </HistoryRow>
        )
      })}
    </HistoryList>
  )
}

function ConditionList() {
  const {
    control,
    register,
    formState: { errors },
  } = useFormContext<EditPatientValues>()
  const { fields, append, remove } = useFieldArray({ control, name: 'chronic_conditions' })
  const rows = useRowRemoval(remove)

  return (
    <HistoryList
      title="Chronic conditions"
      emptyText="No chronic conditions recorded."
      addLabel="Add condition"
      addRef={rows.addButton}
      onAdd={() => append({ ...EMPTY_CONDITION })}
      error={errors.chronic_conditions?.root?.message ?? errors.chronic_conditions?.message}
    >
      {fields.map((field, index) => {
        const label = `Condition ${index + 1}`
        const rowErrors = errors.chronic_conditions?.[index]
        return (
          <HistoryRow
            key={field.id}
            label={label}
            removeRef={rows.removeButton(index)}
            onRemove={() => rows.removeRow(index)}
          >
            <Field label={label} required error={rowErrors?.name?.message}>
              {(p) => (
                <Input
                  placeholder="e.g. Type 2 diabetes"
                  {...p}
                  {...register(`chronic_conditions.${index}.name`)}
                />
              )}
            </Field>
            <Field label="Since year" error={rowErrors?.since_year?.message}>
              {(p) => (
                <Input
                  inputMode="numeric"
                  placeholder="e.g. 2015"
                  {...p}
                  {...register(`chronic_conditions.${index}.since_year`)}
                />
              )}
            </Field>
            <Field label="Notes" className="sm:col-span-2" error={rowErrors?.notes?.message}>
              {(p) => <Input {...p} {...register(`chronic_conditions.${index}.notes`)} />}
            </Field>
          </HistoryRow>
        )
      })}
    </HistoryList>
  )
}

function MedicationList() {
  const {
    control,
    register,
    formState: { errors },
  } = useFormContext<EditPatientValues>()
  const { fields, append, remove } = useFieldArray({ control, name: 'current_medications' })
  const rows = useRowRemoval(remove)

  return (
    <HistoryList
      title="Current medications"
      emptyText="No current medications recorded."
      addLabel="Add medication"
      addRef={rows.addButton}
      onAdd={() => append({ ...EMPTY_MEDICATION })}
      error={errors.current_medications?.root?.message ?? errors.current_medications?.message}
    >
      {fields.map((field, index) => {
        const label = `Medication ${index + 1}`
        const rowErrors = errors.current_medications?.[index]
        return (
          <HistoryRow
            key={field.id}
            label={label}
            removeRef={rows.removeButton(index)}
            onRemove={() => rows.removeRow(index)}
          >
            <Field label={label} required error={rowErrors?.name?.message}>
              {(p) => (
                <Input
                  placeholder="e.g. Metformin"
                  {...p}
                  {...register(`current_medications.${index}.name`)}
                />
              )}
            </Field>
            <Field label="Dosage" error={rowErrors?.dosage?.message}>
              {(p) => (
                <Input
                  placeholder="e.g. 500mg"
                  {...p}
                  {...register(`current_medications.${index}.dosage`)}
                />
              )}
            </Field>
            <Field label="Frequency" error={rowErrors?.frequency?.message}>
              {(p) => (
                <Input
                  placeholder="e.g. Twice daily"
                  {...p}
                  {...register(`current_medications.${index}.frequency`)}
                />
              )}
            </Field>
            <Field label="Started on" error={rowErrors?.started_on?.message}>
              {(p) => (
                <Input type="date" {...p} {...register(`current_medications.${index}.started_on`)} />
              )}
            </Field>
          </HistoryRow>
        )
      })}
    </HistoryList>
  )
}

/**
 * The three history lists of the edit form. Must be rendered inside a
 * `FormProvider` whose values include them.
 *
 * Each list is saved whole (the API replaces it rather than merging), so rows
 * are only ever added or removed here — there is no per-row save.
 */
export function PatientHistoryFields() {
  return (
    <div className="space-y-6">
      <AllergyList />
      <ConditionList />
      <MedicationList />
    </div>
  )
}
