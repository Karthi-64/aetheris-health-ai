import { useEffect, useState } from 'react'
import { zodResolver } from '@hookform/resolvers/zod'
import { useForm } from 'react-hook-form'
import { Link, Navigate } from 'react-router-dom'
import { z } from 'zod'
import { Alert, Button } from '@atheris/ui'
import { useRequestOtp, useVerifyOtp } from '@/api/auth'
import { fieldControlClass } from '@/components/fieldStyles'
import { FormField } from '@/components/FormField'
import { usePageTitle } from '@/lib/usePageTitle'
import { otpErrorMessage } from '@/pages/auth/errors'
import { authStrings as S } from '@/pages/auth/strings'
import { useOtpChallengeStore, type OtpChallenge } from '@/store/otp-challenge-store'

const schema = z.object({
  code: z.string().trim().regex(/^\d{6}$/, S.codeFormat),
})

type VerifyValues = z.infer<typeof schema>

/** Whole seconds left until `at` (epoch ms), ticking down to 0. */
function useSecondsUntil(at: number): number {
  const [now, setNow] = useState(() => Date.now())
  const remaining = Math.max(0, Math.ceil((at - now) / 1000))
  const isWaiting = remaining > 0

  useEffect(() => {
    if (!isWaiting) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [isWaiting])

  return remaining
}

interface ResendButtonProps {
  /** When another code may be requested (epoch ms). */
  resendAt: number
  isSending: boolean
  onResend: () => void
}

/**
 * "Send a new code", locked until the server's resend delay has passed. The
 * lock is a courtesy to the patient — the server enforces its own limits.
 */
function ResendButton({ resendAt, isSending, onResend }: ResendButtonProps) {
  const seconds = useSecondsUntil(resendAt)
  return (
    <Button type="button" variant="outline" size="touch" onClick={onResend} disabled={seconds > 0 || isSending}>
      {seconds > 0 ? S.resendIn(seconds) : S.resend}
    </Button>
  )
}

/** Code entry. Without a pending challenge there is nothing to verify. */
export function VerifyOtpPage() {
  const challenge = useOtpChallengeStore((s) => s.challenge)
  if (!challenge) return <Navigate to="/login" replace />
  return <VerifyOtpForm challenge={challenge} />
}

function VerifyOtpForm({ challenge }: { challenge: OtpChallenge }) {
  usePageTitle(S.verifyTitle)
  const verifyOtp = useVerifyOtp()
  const resendOtp = useRequestOtp()
  const {
    register,
    handleSubmit,
    reset,
    setFocus,
    formState: { errors },
  } = useForm<VerifyValues>({ resolver: zodResolver(schema), defaultValues: { code: '' } })

  const onSubmit = handleSubmit(({ code }) => {
    resendOtp.reset()
    verifyOtp.mutate(
      { challengeId: challenge.id, code },
      // On success the guard above this page takes the signed-in patient home.
      { onError: () => setFocus('code', { shouldSelect: true }) },
    )
  })

  const resend = () => {
    verifyOtp.reset()
    resendOtp.mutate(challenge.phone, {
      onSuccess: (sent) => {
        useOtpChallengeStore.getState().setChallenge({
          id: sent.challenge_id,
          phone: challenge.phone,
          resendAt: Date.now() + sent.resend_after * 1000,
        })
        reset({ code: '' })
        setFocus('code')
      },
    })
  }

  const failure = verifyOtp.error ?? resendOtp.error

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="font-display text-headline-md text-primary">{S.verifyHeading}</h1>
        <p className="text-body-lg text-on-surface-variant">
          {S.verifySentTo} <span className="text-on-surface font-semibold whitespace-nowrap">{challenge.phone}</span>.
        </p>
      </div>

      {failure && <Alert variant="error">{otpErrorMessage(failure)}</Alert>}
      {resendOtp.isSuccess && !failure && (
        <Alert variant="success" role="status">
          {S.resent}
        </Alert>
      )}

      <form onSubmit={onSubmit} noValidate className="space-y-5">
        <FormField label={S.codeLabel} error={errors.code?.message}>
          {(field) => (
            <input
              {...field}
              {...register('code')}
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="\d{6}"
              maxLength={6}
              autoFocus
              required
              className={`${fieldControlClass} text-center font-mono text-2xl tracking-[0.4em]`}
            />
          )}
        </FormField>

        <Button type="submit" size="touch" className="w-full" disabled={verifyOtp.isPending}>
          {verifyOtp.isPending ? S.verifying : S.verify}
        </Button>
      </form>

      <div className="flex flex-col items-stretch gap-2">
        {/* Keyed by the deadline: a new challenge starts a fresh countdown. */}
        <ResendButton
          key={challenge.resendAt}
          resendAt={challenge.resendAt}
          isSending={resendOtp.isPending}
          onResend={resend}
        />
        <Button asChild variant="link" size="touch">
          <Link to="/login">{S.changeNumber}</Link>
        </Button>
      </div>
    </div>
  )
}
