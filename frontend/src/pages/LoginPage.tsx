import { useState } from 'react'
import { isAxiosError } from 'axios'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { Logo } from '@/components/brand/Logo'
import { toAuthUser, useAuthStore, type SignOutReason } from '@/store/auth-store'
import { api } from '@/lib/api'
import { MOCK_PERMISSIONS_BY_ROLE } from '@/lib/rbac'
import { cn } from '@/lib/utils'

// Mock auth only runs in dev AND when explicitly enabled. Production builds
// can never authenticate against the mock (defect F1).
const USE_MOCK_AUTH = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK_AUTH === 'true'

const loginSchema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
})

type LoginValues = z.infer<typeof loginSchema>

// The verify route accepts exactly six digits (`MfaVerifyRequest.code`).
const mfaSchema = z.object({
  code: z.string().regex(/^\d{6}$/, 'Enter the 6-digit code'),
})

type MfaValues = z.infer<typeof mfaSchema>

// POST /auth/mfa/verify gives this one answer for every refusal of a
// well-formed ticket: a wrong code, too many attempts, a password changed
// since, an account no longer active. The server does not say which. Any
// other 401 means the ticket itself has expired or is malformed.
const WRONG_MFA_CODE = 'Invalid MFA code.'

/** Why the user is back on this page without having asked to sign out. */
const SIGN_OUT_NOTICE: Record<SignOutReason, string> = {
  session_ended: 'Your session has ended. Sign in again.',
  password_changed: 'Your password was changed. Sign in again with your new password.',
}

const MFA_EXPIRED_NOTICE = 'That sign-in attempt expired. Enter your password to start again.'

const INPUT_CLASS =
  'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant w-full rounded-xl px-4 py-3 outline-none focus:ring-2'

const SUBMIT_CLASS =
  'shadow-neo-base bg-primary text-on-primary font-label text-label-caps flex w-full items-center justify-center gap-2 rounded-xl py-3.5 font-bold transition-all duration-300 hover:-translate-y-0.5 active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-70'

interface LocationState {
  from?: { pathname?: string }
}

export default function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const setAuth = useAuthStore((s) => s.setAuth)
  const signOutReason = useAuthStore((s) => s.signOutReason)
  const [showPassword, setShowPassword] = useState(false)
  // Set while the second step is showing: the ticket the login call issued.
  const [mfaTicket, setMfaTicket] = useState<string | null>(null)
  const [mfaExpired, setMfaExpired] = useState(false)

  const from = (location.state as LocationState | null)?.from?.pathname ?? '/dashboard'

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: '', password: '' },
  })

  const mfaForm = useForm<MfaValues>({
    resolver: zodResolver(mfaSchema),
    defaultValues: { code: '' },
  })
  const mfaErrors = mfaForm.formState.errors
  const mfaSubmitting = mfaForm.formState.isSubmitting

  const notice = mfaExpired
    ? MFA_EXPIRED_NOTICE
    : signOutReason
      ? SIGN_OUT_NOTICE[signOutReason]
      : null

  /** Both the password step and the MFA step end with the same token payload. */
  function completeSignIn(payload: {
    user: Record<string, unknown>
    access_token: string
    refresh_token: string
  }) {
    setAuth(toAuthUser(payload.user), payload.access_token, payload.refresh_token)
    toast.success('Welcome back')
    navigate(from, { replace: true })
  }

  function backToPassword() {
    setMfaTicket(null)
    mfaForm.reset()
  }

  async function onVerify(values: MfaValues) {
    try {
      const { data } = await api.post('/auth/mfa/verify', {
        mfa_ticket: mfaTicket,
        code: values.code,
      })
      completeSignIn(data.data)
    } catch (err) {
      if (isAxiosError(err) && err.response?.status === 401) {
        const message = (err.response.data as { message?: string } | undefined)?.message
        if (message === WRONG_MFA_CODE) {
          mfaForm.setError('code', {
            message:
              'That code was not accepted. Check the app and try again. If it keeps failing, sign in again.',
          })
          return
        }
        // The ticket lasts five minutes and cannot be renewed: start over.
        backToPassword()
        setMfaExpired(true)
        return
      }
      toast.error('Could not verify the code. Please try again.')
    }
  }

  async function onSubmit(values: LoginValues) {
    try {
      if (USE_MOCK_AUTH) {
        // Dev-only stand-in. Never reachable in a production build.
        await new Promise((r) => setTimeout(r, 400))
        const role = 'hospital_admin'
        setAuth(
          {
            id: 'demo-user',
            name: 'Dr. A. Chen',
            email: values.email,
            role,
            permissions: [...MOCK_PERMISSIONS_BY_ROLE[role]],
          },
          'demo-access-token',
        )
        toast.success('Welcome back')
        navigate(from, { replace: true })
      } else {
        // Real login: server returns access_token + refresh_token + user
        // in the standard envelope. Both tokens are stored in memory. The
        // user's `permissions` array drives every nav/route gate client-side.
        const { data } = await api.post('/auth/login', {
          email: values.email,
          password: values.password,
        })
        // An account with MFA enabled gets a short-lived ticket instead of
        // tokens; the code step exchanges it (backend/app/api/v1/auth.py).
        if (typeof data?.data?.mfa_ticket === 'string') {
          setMfaExpired(false)
          setMfaTicket(data.data.mfa_ticket)
          return
        }
        completeSignIn(data.data)
      }
    } catch {
      // Never reveal whether the email exists (spec 2B §12).
      // One wording for every failure, whatever the cause. It always points
      // at the emailed link, because a correct password can be refused too
      // while sign-in attempts on an account are being slowed down.
      toast.error('Invalid credentials, or the server is unavailable.', {
        description:
          'If you are sure of your password, use "Forgot password?" to get a sign-in link by email.',
      })
    }
  }

  return (
    <div className="relative flex min-h-[100dvh] items-center justify-center overflow-hidden px-4 py-12">
      {/* Floating decorative shapes */}
      <div className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
        <div className="bg-primary-fixed-dim/30 absolute -top-[10%] -left-[5%] h-96 w-96 rounded-full blur-3xl" />
        <div className="bg-secondary-fixed/30 absolute right-[-10%] bottom-[5%] h-[500px] w-[500px] rounded-full blur-3xl" />
      </div>

      <div className="glassmorphism shadow-glass-panel animate-in fade-in zoom-in-95 w-full max-w-md rounded-3xl p-8 duration-500 sm:p-10">
        <Link to="/" className="mb-8 flex justify-center">
          <Logo variant="mark" className="h-14" />
        </Link>

        <div className="mb-8 text-center">
          <h1 className="font-display text-headline-lg text-primary">
            {mfaTicket ? 'Enter your code' : 'Welcome back'}
          </h1>
          <p className="font-body text-body-sm text-on-surface-variant mt-2">
            {mfaTicket
              ? 'Open your authenticator app and enter the 6-digit code for Aetheris.'
              : 'Sign in to your clinical workspace.'}
          </p>
        </div>

        {notice && !mfaTicket && (
          <p
            role="status"
            className="border-secondary/40 bg-secondary/10 font-body text-body-sm text-on-surface mb-5 rounded-xl border px-4 py-3"
          >
            {notice}
          </p>
        )}

        {mfaTicket ? (
          <form
            onSubmit={(e) => mfaForm.handleSubmit(onVerify)(e)}
            className="space-y-5"
            noValidate
          >
            <div className="space-y-2">
              <label
                htmlFor="mfa-code"
                className="font-label text-label-caps text-on-surface-variant"
              >
                Authentication code
              </label>
              <input
                id="mfa-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                placeholder="123456"
                aria-invalid={!!mfaErrors.code}
                aria-describedby={mfaErrors.code ? 'mfa-code-error' : undefined}
                className={cn(
                  INPUT_CLASS,
                  mfaErrors.code ? 'focus:ring-error' : 'focus:ring-secondary',
                )}
                {...mfaForm.register('code')}
              />
              {mfaErrors.code && (
                <p id="mfa-code-error" role="alert" className="font-body text-error text-xs">
                  {mfaErrors.code.message}
                </p>
              )}
            </div>

            <button type="submit" disabled={mfaSubmitting} className={SUBMIT_CLASS}>
              {mfaSubmitting ? (
                <>
                  <Loader2 className="size-5 animate-spin" />
                  Verifying...
                </>
              ) : (
                'Verify'
              )}
            </button>

            <button
              type="button"
              onClick={backToPassword}
              className="font-body text-body-sm text-secondary w-full text-center hover:underline"
            >
              Back to sign in
            </button>
          </form>
        ) : (
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-5" noValidate>
            {/* Email */}
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
                  INPUT_CLASS,
                  errors.email ? 'focus:ring-error' : 'focus:ring-secondary',
                )}
                {...register('email')}
              />
              {errors.email && (
                <p className="font-body text-error text-xs">{errors.email.message}</p>
              )}
            </div>

            {/* Password */}
            <div className="space-y-2">
              <label
                htmlFor="password"
                className="font-label text-label-caps text-on-surface-variant"
              >
                Password
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  placeholder="••••••••"
                  aria-invalid={!!errors.password}
                  className={cn(
                    INPUT_CLASS,
                    'pr-11',
                    errors.password ? 'focus:ring-error' : 'focus:ring-secondary',
                  )}
                  {...register('password')}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className="text-outline hover:text-secondary absolute top-1/2 right-3 -translate-y-1/2 transition-colors"
                >
                  {showPassword ? <EyeOff className="size-5" /> : <Eye className="size-5" />}
                </button>
              </div>
              {errors.password && (
                <p className="font-body text-error text-xs">{errors.password.message}</p>
              )}
            </div>

            {/* Options */}
            <div className="flex items-center justify-end">
              <Link
                to="/forgot-password"
                className="font-body text-body-sm text-secondary hover:underline"
              >
                Forgot password?
              </Link>
            </div>

            <button type="submit" disabled={isSubmitting} className={SUBMIT_CLASS}>
              {isSubmitting ? (
                <>
                  <Loader2 className="size-5 animate-spin" />
                  Signing in...
                </>
              ) : (
                'Sign in'
              )}
            </button>
          </form>
        )}

        <p className="font-body text-body-sm text-on-surface-variant mt-8 text-center">
          New to Aetheris?{' '}
          <Link to="/contact" className="text-secondary font-bold hover:underline">
            Get started
          </Link>
        </p>
      </div>
    </div>
  )
}
