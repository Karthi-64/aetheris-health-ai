import { zodResolver } from '@hookform/resolvers/zod'
import { useForm } from 'react-hook-form'
import { useNavigate } from 'react-router-dom'
import { z } from 'zod'
import { ApiError } from '@atheris/api-core'
import { Alert, Button } from '@atheris/ui'
import { useRequestOtp } from '@/api/auth'
import { fieldControlClass } from '@/components/fieldStyles'
import { FormField } from '@/components/FormField'
import { normalisePhone } from '@/lib/phone'
import { usePageTitle } from '@/lib/usePageTitle'
import { otpErrorMessage } from '@/pages/auth/errors'
import { authStrings as S } from '@/pages/auth/strings'
import { useOtpChallengeStore } from '@/store/otp-challenge-store'
import { usePatientAuthStore } from '@/store/patient-auth-store'

const schema = z.object({
  phone: z
    .string()
    .trim()
    .min(1, S.phoneRequired)
    .refine((value) => normalisePhone(value) !== null, S.phoneInvalid),
})

type LoginValues = z.infer<typeof schema>

/** Phone entry: asks the server to text a one-time code. */
export function LoginPage() {
  usePageTitle(S.loginTitle)
  const navigate = useNavigate()
  const requestOtp = useRequestOtp()
  const signOutReason = usePatientAuthStore((s) => s.signOutReason)
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<LoginValues>({ resolver: zodResolver(schema), defaultValues: { phone: '' } })

  const onSubmit = handleSubmit(({ phone }) => {
    const e164 = normalisePhone(phone)
    if (e164 === null) return
    requestOtp.mutate(e164, {
      onSuccess: (sent) => {
        useOtpChallengeStore.getState().setChallenge({
          id: sent.challenge_id,
          phone: e164,
          resendAt: Date.now() + sent.resend_after * 1000,
        })
        // The challenge travels in memory — never in the URL.
        void navigate('/verify-otp')
      },
      onError: (err) => {
        if (err instanceof ApiError && err.status === 422) {
          setError('phone', { message: S.phoneNotAccepted }, { shouldFocus: true })
        }
      },
    })
  })

  const isRejectedNumber = requestOtp.error instanceof ApiError && requestOtp.error.status === 422
  const formError = requestOtp.isError && !isRejectedNumber ? otpErrorMessage(requestOtp.error) : null

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="font-display text-headline-md text-primary">{S.loginHeading}</h1>
        <p className="text-body-lg text-on-surface-variant">{S.loginIntro}</p>
      </div>

      {signOutReason === 'session_ended' && !requestOtp.isError && <Alert variant="info">{S.sessionEnded}</Alert>}
      {formError && <Alert variant="error">{formError}</Alert>}

      <form onSubmit={onSubmit} noValidate className="space-y-5">
        <FormField label={S.phoneLabel} hint={S.phoneHint} error={errors.phone?.message}>
          {(field) => (
            <input
              {...field}
              {...register('phone')}
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              autoFocus
              required
              className={fieldControlClass}
            />
          )}
        </FormField>

        <Button type="submit" size="touch" className="w-full" disabled={requestOtp.isPending}>
          {requestOtp.isPending ? S.sendingCode : S.sendCode}
        </Button>
      </form>
    </div>
  )
}
