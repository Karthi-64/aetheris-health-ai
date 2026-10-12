import { useState } from 'react'
import { zodResolver } from '@hookform/resolvers/zod'
import { useForm, useWatch } from 'react-hook-form'
import { z } from 'zod'
import { Alert, Button } from '@atheris/ui'
import { useLinkPatient } from '@/api/links'
import { checkboxClass, fieldControlClass } from '@/components/fieldStyles'
import { CheckboxField, FormField } from '@/components/FormField'
import { isIsoDate, todayIsoDate } from '@/lib/format'
import {
  linkFailureOf,
  type LinkAttempt,
  type LinkFailure,
  type LinkSuccess,
} from '@/pages/hospitals/outcomes'
import { linkStrings as S } from '@/pages/hospitals/strings'

const schema = z.object({
  hospitalCode: z.string().trim().min(1, S.hospitalCodeRequired).max(100, S.invalid),
  dateOfBirth: z
    .string()
    .min(1, S.dobRequired)
    .refine(isIsoDate, S.dobInvalid)
    .refine((value) => value <= todayIsoDate(), S.dobFuture),
  mrn: z.string().trim().max(30, S.invalid),
  consent: z.boolean().refine((agreed) => agreed, S.consentRequired),
})

type LinkValues = z.infer<typeof schema>

/** What the last submission was, and whether it carried an MRN. */
interface Submitted extends LinkAttempt {
  withMrn: boolean
}

const sameDetails = (a: LinkAttempt | null, b: LinkAttempt) =>
  a !== null && a.hospitalRef === b.hospitalRef && a.dateOfBirth === b.dateOfBirth

const FAILURE_MESSAGE: Record<Exclude<LinkFailure, 'mrn_required' | 'not_found'>, string> = {
  unavailable: S.contactHospital,
  conflict: S.alreadyLinkedElsewhere,
  stale: S.reload,
  policies_pending: S.policiesPending,
  invalid: S.invalid,
  busy: S.busy,
  error: S.failed,
}

interface LinkDetailsFormProps {
  /** A code to start with, when the patient came from a hospital's page. */
  initialHospitalCode?: string
  onDone: (outcome: LinkSuccess) => void
  onRegister: (attempt: LinkAttempt) => void
}

/** Hospital code + date of birth, and the MRN once the server asks for it. */
export function LinkDetailsForm({ initialHospitalCode = '', onDone, onRegister }: LinkDetailsFormProps) {
  const link = useLinkPatient()
  const [submitted, setSubmitted] = useState<Submitted | null>(null)
  // The MRN is asked for only for the exact details the server asked it for.
  const [mrnNeededFor, setMrnNeededFor] = useState<LinkAttempt | null>(null)
  const {
    register,
    handleSubmit,
    setError,
    control,
    formState: { errors },
  } = useForm<LinkValues>({
    resolver: zodResolver(schema),
    defaultValues: { hospitalCode: initialHospitalCode, dateOfBirth: '', mrn: '', consent: false },
  })

  const [hospitalCode, dateOfBirth] = useWatch({ control, name: ['hospitalCode', 'dateOfBirth'] })
  const askMrn = sameDetails(mrnNeededFor, { hospitalRef: hospitalCode.trim(), dateOfBirth })

  const onSubmit = handleSubmit((values) => {
    const attempt: LinkAttempt = { hospitalRef: values.hospitalCode, dateOfBirth: values.dateOfBirth }
    const withMrn = sameDetails(mrnNeededFor, attempt)
    if (withMrn && values.mrn === '') {
      setError('mrn', { message: S.mrnRequired }, { shouldFocus: true })
      return
    }
    setSubmitted({ ...attempt, withMrn })
    link.mutate(
      { ...attempt, mrn: withMrn ? values.mrn : undefined },
      {
        onSuccess: (outcome) => onDone(outcome === 'created' ? 'linked' : 'already_linked'),
        onError: (err) => {
          if (linkFailureOf(err) === 'mrn_required') setMrnNeededFor(attempt)
        },
      },
    )
  })

  const failure = link.isError ? linkFailureOf(link.error) : null

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="font-display text-headline-md text-primary">{S.heading}</h1>
        <p className="text-body-lg text-on-surface-variant">{S.intro}</p>
      </div>

      {failure === 'mrn_required' && <Alert variant="info">{S.mrnNeeded}</Alert>}
      {failure === 'not_found' && submitted && (
        <Alert variant="warning" title={S.notFound}>
          <p>{S.notFoundHelp}</p>
          {/* Registration is not offered after an MRN attempt (spec 4.5). */}
          {!submitted.withMrn && (
            <div className="mt-3 space-y-3">
              <p>{S.registerOffer}</p>
              <Button
                type="button"
                variant="outline"
                size="touch"
                className="w-full whitespace-normal"
                onClick={() => onRegister({ hospitalRef: submitted.hospitalRef, dateOfBirth: submitted.dateOfBirth })}
              >
                {S.registerCta}
              </Button>
            </div>
          )}
        </Alert>
      )}
      {failure && failure !== 'mrn_required' && failure !== 'not_found' && (
        <Alert variant="error">{FAILURE_MESSAGE[failure]}</Alert>
      )}

      <form onSubmit={onSubmit} noValidate className="bg-card space-y-5 rounded-2xl border p-5">
        <FormField label={S.hospitalCodeLabel} hint={S.hospitalCodeHint} error={errors.hospitalCode?.message}>
          {(field) => (
            <input
              {...field}
              {...register('hospitalCode')}
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              required
              className={fieldControlClass}
            />
          )}
        </FormField>

        <FormField label={S.dobLabel} error={errors.dateOfBirth?.message}>
          {(field) => (
            <input
              {...field}
              {...register('dateOfBirth')}
              type="date"
              autoComplete="bday"
              max={todayIsoDate()}
              required
              className={fieldControlClass}
            />
          )}
        </FormField>

        {askMrn && (
          <FormField label={S.mrnLabel} hint={S.mrnHint} error={errors.mrn?.message}>
            {(field) => (
              <input
                {...field}
                {...register('mrn')}
                type="text"
                autoComplete="off"
                autoCapitalize="characters"
                maxLength={30}
                spellCheck={false}
                autoFocus
                required
                className={fieldControlClass}
              />
            )}
          </FormField>
        )}

        <CheckboxField label={S.linkConsent} error={errors.consent?.message}>
          {(field) => <input {...field} {...register('consent')} type="checkbox" className={checkboxClass} />}
        </CheckboxField>

        <Button type="submit" size="touch" className="w-full" disabled={link.isPending}>
          {link.isPending ? S.submitting : S.submit}
        </Button>
      </form>
    </div>
  )
}
