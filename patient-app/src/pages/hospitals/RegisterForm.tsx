import { zodResolver } from '@hookform/resolvers/zod'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { Alert, Button } from '@atheris/ui'
import { GENDERS, useRegisterPatient } from '@/api/links'
import { checkboxClass, fieldControlClass } from '@/components/fieldStyles'
import { CheckboxField, FormField } from '@/components/FormField'
import { formatCalendarDate } from '@/lib/format'
import { linkFailureOf, type LinkAttempt, type LinkFailure } from '@/pages/hospitals/outcomes'
import { genderLabels, linkStrings as S } from '@/pages/hospitals/strings'

const schema = z.object({
  firstName: z.string().trim().min(1, S.firstNameRequired).max(100, S.nameTooLong),
  lastName: z.string().trim().min(1, S.lastNameRequired).max(100, S.nameTooLong),
  gender: z.enum(GENDERS, S.genderRequired),
  consent: z.boolean().refine((agreed) => agreed, S.consentRequired),
})

type RegisterValues = z.input<typeof schema>

const FAILURE_MESSAGE: Record<LinkFailure, string> = {
  // The server never asks for an MRN here; if it did, nothing is claimed about a record.
  mrn_required: S.failed,
  conflict: S.registerConflict,
  stale: S.reload,
  policies_pending: S.policiesPending,
  not_found: S.registerNotFound,
  unavailable: S.contactHospital,
  invalid: S.invalid,
  busy: S.busy,
  error: S.failed,
}

interface RegisterFormProps {
  /** The details that found no record; registration is for exactly these. */
  attempt: LinkAttempt
  onBack: () => void
  onDone: () => void
}

/**
 * Self-registration, as a confirmation step. A mistyped date of birth looks
 * the same as "no record" (spec 4.5), so the date the patient entered is shown
 * back to them before a new record is created with it.
 */
export function RegisterForm({ attempt, onBack, onDone }: RegisterFormProps) {
  const registerPatient = useRegisterPatient()
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<RegisterValues>({
    resolver: zodResolver(schema),
    defaultValues: { firstName: '', lastName: '', consent: false },
  })

  const onSubmit = handleSubmit((values) => {
    registerPatient.mutate(
      {
        hospitalRef: attempt.hospitalRef,
        dateOfBirth: attempt.dateOfBirth,
        firstName: values.firstName,
        lastName: values.lastName,
        gender: values.gender,
      },
      { onSuccess: onDone },
    )
  })

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="font-display text-headline-md text-primary">{S.registerHeading}</h1>
        <p className="text-body-lg text-on-surface-variant">{S.registerIntro}</p>
      </div>

      <div className="bg-surface-container-low space-y-3 rounded-2xl border p-5">
        <dl className="space-y-3">
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.confirmDob}</dt>
            <dd className="text-title-lg text-on-surface">{formatCalendarDate(attempt.dateOfBirth)}</dd>
          </div>
          <div>
            <dt className="text-body-sm text-on-surface-variant">{S.confirmHospitalCode}</dt>
            <dd className="text-body-lg text-on-surface font-semibold break-all">{attempt.hospitalRef}</dd>
          </div>
        </dl>
        <Button type="button" variant="link" size="touch" className="h-auto min-h-11 px-0 whitespace-normal" onClick={onBack}>
          {S.changeDetails}
        </Button>
      </div>

      {registerPatient.isError && (
        <Alert variant="error">{FAILURE_MESSAGE[linkFailureOf(registerPatient.error)]}</Alert>
      )}

      <form onSubmit={onSubmit} noValidate className="bg-card space-y-5 rounded-2xl border p-5">
        <FormField label={S.firstNameLabel} error={errors.firstName?.message}>
          {(field) => (
            <input
              {...field}
              {...register('firstName')}
              type="text"
              autoComplete="given-name"
              autoFocus
              required
              className={fieldControlClass}
            />
          )}
        </FormField>

        <FormField label={S.lastNameLabel} error={errors.lastName?.message}>
          {(field) => (
            <input
              {...field}
              {...register('lastName')}
              type="text"
              autoComplete="family-name"
              required
              className={fieldControlClass}
            />
          )}
        </FormField>

        <FormField label={S.genderLabel} error={errors.gender?.message}>
          {(field) => (
            <select {...field} {...register('gender')} autoComplete="sex" required defaultValue="" className={fieldControlClass}>
              <option value="" disabled>
                {S.genderPlaceholder}
              </option>
              {GENDERS.map((gender) => (
                <option key={gender} value={gender}>
                  {genderLabels[gender]}
                </option>
              ))}
            </select>
          )}
        </FormField>

        <CheckboxField label={S.registerConsent} error={errors.consent?.message}>
          {(field) => <input {...field} {...register('consent')} type="checkbox" className={checkboxClass} />}
        </CheckboxField>

        <Button type="submit" size="touch" className="w-full" disabled={registerPatient.isPending}>
          {registerPatient.isPending ? S.registerSubmitting : S.registerSubmit}
        </Button>
      </form>
    </div>
  )
}
