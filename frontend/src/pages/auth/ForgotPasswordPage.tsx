import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ArrowLeft, CheckCircle, Loader2, Mail } from 'lucide-react'
import { Logo } from '@/components/brand/Logo'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

const forgotSchema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
})

type ForgotValues = z.infer<typeof forgotSchema>

/**
 * Forgot Password page (task 5).
 *
 * The backend always returns a generic success message regardless of whether
 * the email exists (enumeration prevention — security rule 10). The UI shows
 * the same state in both cases. It does not claim an email was delivered: the
 * backend only sends one when SMTP is configured (`backend/app/core/email.py`).
 */
export default function ForgotPasswordPage() {
  const [sent, setSent] = useState(false)
  // `isSubmitting` only disables the button after a re-render; this stops a
  // second request (and a second reset token) fired before that happens.
  const submitting = useRef(false)

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<ForgotValues>({
    resolver: zodResolver(forgotSchema),
    defaultValues: { email: '' },
  })

  async function onSubmit(values: ForgotValues) {
    if (submitting.current) return
    submitting.current = true
    try {
      await api.post('/auth/password/forgot', { email: values.email })
      setSent(true)
    } catch {
      // Even on error, show the same success state to prevent enumeration.
      setSent(true)
    } finally {
      submitting.current = false
    }
  }

  // Success state — the same whether or not the account exists
  if (sent) {
    return (
      <div className="relative flex min-h-[100dvh] items-center justify-center overflow-hidden px-4 py-12">
        <div className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
          <div className="bg-primary-fixed-dim/30 absolute -top-[10%] -left-[5%] h-96 w-96 rounded-full blur-3xl" />
          <div className="bg-secondary-fixed/30 absolute right-[-10%] bottom-[5%] h-[500px] w-[500px] rounded-full blur-3xl" />
        </div>

        <div className="glassmorphism shadow-glass-panel animate-in fade-in zoom-in-95 w-full max-w-md rounded-3xl p-8 duration-500 sm:p-10">
          <Link to="/" className="mb-8 flex justify-center">
            <Logo variant="mark" className="h-14" />
          </Link>

          <div className="flex flex-col items-center text-center">
            <div className="bg-success/10 mb-4 flex size-16 items-center justify-center rounded-full">
              <CheckCircle className="text-success size-8" />
            </div>
            <h1 className="font-display text-headline-lg text-primary mb-2">Request received</h1>
            <p className="font-body text-body-sm text-on-surface-variant mb-8 max-w-xs">
              If an account exists for that address, a reset link is sent to it. Email delivery has
              to be configured for this hospital — if nothing arrives, contact your hospital
              administrator.
            </p>
            <Link
              to="/login"
              className="font-body text-body-sm text-secondary hover:underline flex items-center gap-1"
            >
              <ArrowLeft className="size-4" />
              Back to sign in
            </Link>
          </div>
        </div>
      </div>
    )
  }

  // Form state
  return (
    <div className="relative flex min-h-[100dvh] items-center justify-center overflow-hidden px-4 py-12">
      <div className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
        <div className="bg-primary-fixed-dim/30 absolute -top-[10%] -left-[5%] h-96 w-96 rounded-full blur-3xl" />
        <div className="bg-secondary-fixed/30 absolute right-[-10%] bottom-[5%] h-[500px] w-[500px] rounded-full blur-3xl" />
      </div>

      <div className="glassmorphism shadow-glass-panel animate-in fade-in zoom-in-95 w-full max-w-md rounded-3xl p-8 duration-500 sm:p-10">
        <Link to="/" className="mb-8 flex justify-center">
          <Logo variant="mark" className="h-14" />
        </Link>

        <div className="mb-8 text-center">
          <div className="bg-secondary/10 mx-auto mb-4 flex size-14 items-center justify-center rounded-full">
            <Mail className="text-secondary size-7" />
          </div>
          <h1 className="font-display text-headline-lg text-primary">Forgot password?</h1>
          <p className="font-body text-body-sm text-on-surface-variant mt-2">
            Enter your email to request a password reset link.
          </p>
        </div>

        <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-5" noValidate>
          <div className="space-y-2">
            <label htmlFor="email" className="font-label text-label-caps text-on-surface-variant">
              Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              placeholder="clinician@hospital.org"
              aria-invalid={!!errors.email}
              className={cn(
                'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant w-full rounded-xl px-4 py-3 outline-none focus:ring-2',
                errors.email ? 'focus:ring-error' : 'focus:ring-secondary',
              )}
              {...register('email')}
            />
            {errors.email && (
              <p className="font-body text-error text-xs">{errors.email.message}</p>
            )}
          </div>

          <button
            type="submit"
            disabled={isSubmitting}
            className="shadow-neo-base bg-primary text-on-primary font-label text-label-caps flex w-full items-center justify-center gap-2 rounded-xl py-3.5 font-bold transition-all duration-300 hover:-translate-y-0.5 active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-70"
          >
            {isSubmitting ? (
              <>
                <Loader2 className="size-5 animate-spin" />
                Sending…
              </>
            ) : (
              'Send reset link'
            )}
          </button>
        </form>

        <p className="font-body text-body-sm text-on-surface-variant mt-8 text-center">
          <Link to="/login" className="text-secondary font-bold hover:underline flex items-center justify-center gap-1">
            <ArrowLeft className="size-4" />
            Back to sign in
          </Link>
        </p>
      </div>
    </div>
  )
}
