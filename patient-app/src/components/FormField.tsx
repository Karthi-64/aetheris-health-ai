import { type ReactNode, useId } from 'react'
import { cn } from '@atheris/ui'

/** What a control needs to be tied to its label, hint and error. */
export interface FieldControlProps {
  id: string
  'aria-invalid': boolean
  'aria-describedby': string | undefined
}

interface FormFieldProps {
  label: string
  /** Validation error; when present the field is in the error state. */
  error?: string
  /** Help text under the label. It stays visible when there is an error. */
  hint?: string
  className?: string
  /** Render-prop receiving `id`, `aria-invalid` and `aria-describedby` for the control. */
  children: (props: FieldControlProps) => ReactNode
}

/**
 * Form-field wrapper enforcing the label + help text + error contract and
 * wiring the ids, so no control is ever without an accessible name or its
 * error left unannounced.
 */
export function FormField({ label, error, hint, className, children }: FormFieldProps) {
  const id = useId()
  const hintId = hint ? `${id}-hint` : undefined
  const errorId = error ? `${id}-error` : undefined
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined

  return (
    <div className={cn('space-y-1.5', className)}>
      <label htmlFor={id} className="text-body-sm text-on-surface block font-semibold">
        {label}
      </label>
      {hint && (
        <p id={hintId} className="text-body-sm text-on-surface-variant">
          {hint}
        </p>
      )}
      {children({ id, 'aria-invalid': !!error, 'aria-describedby': describedBy })}
      {error && (
        <p id={errorId} role="alert" className="text-body-sm text-error font-medium">
          {error}
        </p>
      )}
    </div>
  )
}

interface CheckboxFieldProps {
  label: string
  error?: string
  /** Render-prop receiving the wiring for the `<input type="checkbox">`. */
  children: (props: FieldControlProps) => ReactNode
}

/** A consent tick: the whole row is the touch target, not just the box. */
export function CheckboxField({ label, error, children }: CheckboxFieldProps) {
  const id = useId()
  const errorId = error ? `${id}-error` : undefined

  return (
    <div className="space-y-1.5">
      <label
        htmlFor={id}
        className="text-body-sm text-on-surface flex min-h-11 cursor-pointer items-start gap-3 py-2"
      >
        {children({ id, 'aria-invalid': !!error, 'aria-describedby': errorId })}
        <span>{label}</span>
      </label>
      {error && (
        <p id={errorId} role="alert" className="text-body-sm text-error font-medium">
          {error}
        </p>
      )}
    </div>
  )
}
