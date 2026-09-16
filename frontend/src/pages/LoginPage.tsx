import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { Logo } from '@/components/brand/Logo'
import { toAuthUser, useAuthStore } from '@/store/auth-store'
import { api } from '@/lib/api'
import { MOCK_PERMISSIONS_BY_ROLE } from '@/lib/rbac'
import { cn } from '@/lib/utils'

// Mock auth only runs in dev AND when explicitly enabled. Production builds
// can never authenticate against the mock (defect F1).
const USE_MOCK_AUTH = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK_AUTH === 'true'

const loginSchema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
  remember: z.boolean().optional(),
})

type LoginValues = z.infer<typeof loginSchema>

interface LocationState {
  from?: { pathname?: string }
}

export default function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [showPassword, setShowPassword] = useState(false)

  const from = (location.state as LocationState | null)?.from?.pathname ?? '/dashboard'

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: '', password: '', remember: true },
  })

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
      } else {
        // Real login: server returns access_token + refresh_token + user
        // in the standard envelope. Both tokens are stored in memory. The
        // user's `permissions` array drives every nav/route gate client-side.
        const { data } = await api.post('/auth/login', {
          email: values.email,
          password: values.password,
        })
        setAuth(
          toAuthUser(data.data.user),
          data.data.access_token,
          data.data.refresh_token,
        )
      }
      toast.success('Welcome back')
      navigate(from, { replace: true })
    } catch {
      // Never reveal whether the email exists (spec 2B §12).
      toast.error('Invalid credentials, or the server is unavailable.')
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
          <h1 className="font-display text-headline-lg text-primary">Welcome back</h1>
          <p className="font-body text-body-sm text-on-surface-variant mt-2">
            Sign in to your clinical workspace.
          </p>
        </div>

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
                'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant w-full rounded-xl px-4 py-3 outline-none focus:ring-2',
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
            <label htmlFor="password" className="font-label text-label-caps text-on-surface-variant">
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
                  'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant w-full rounded-xl px-4 py-3 pr-11 outline-none focus:ring-2',
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
          <div className="flex items-center justify-between">
            <label className="font-body text-body-sm text-on-surface-variant flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                className="accent-secondary h-4 w-4 rounded"
                {...register('remember')}
              />
              Remember me
            </label>
            <Link
              to="/forgot-password"
              className="font-body text-body-sm text-secondary hover:underline"
            >
              Forgot password?
            </Link>
          </div>

          <button
            type="submit"
            disabled={isSubmitting}
            className="shadow-neo-base bg-primary text-on-primary font-label text-label-caps flex w-full items-center justify-center gap-2 rounded-xl py-3.5 font-bold transition-all duration-300 hover:-translate-y-0.5 active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-70"
          >
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
