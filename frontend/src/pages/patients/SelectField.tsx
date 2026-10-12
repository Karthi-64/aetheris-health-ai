import { Controller, useFormContext, type FieldPath, type FieldValues } from 'react-hook-form'
import { Field } from '@/components/ui/field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

export interface SelectOption {
  /** Never `''` — a Radix select item cannot have an empty value. */
  value: string
  label: string
}

/** A labelled select bound to a field of the surrounding form. */
export function SelectField<T extends FieldValues>({
  name,
  label,
  required,
  options,
}: {
  name: FieldPath<T>
  label: string
  required?: boolean
  options: readonly SelectOption[]
}) {
  const { control } = useFormContext<T>()
  return (
    <Controller
      control={control}
      name={name}
      render={({ field, fieldState }) => (
        <Field label={label} required={required} error={fieldState.error?.message}>
          {(p) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger
                ref={field.ref}
                id={p.id}
                aria-invalid={p['aria-invalid']}
                aria-describedby={p['aria-describedby']}
              >
                <SelectValue placeholder="Select" />
              </SelectTrigger>
              <SelectContent>
                {options.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
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
