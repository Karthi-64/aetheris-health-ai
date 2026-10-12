import { ShieldCheck } from 'lucide-react'
import { Outlet } from 'react-router-dom'
import { Brand } from '@/components/Brand'

/** Shell for the two sign-in screens: phone entry and code entry. */
export function AuthLayout() {
  return (
    <div className="from-secondary-fixed/40 to-background flex min-h-dvh flex-col bg-linear-to-b to-60%">
      <header className="mx-auto w-full max-w-md px-5 pt-8">
        <Brand />
      </header>
      <main className="mx-auto w-full max-w-md flex-1 px-5 py-8">
        <div className="bg-card shadow-glass-panel rounded-3xl border p-6 sm:p-8">
          <Outlet />
        </div>
      </main>
      <footer className="text-body-sm text-on-surface-variant mx-auto flex w-full max-w-md items-start gap-2 px-5 pb-8">
        <ShieldCheck className="text-secondary mt-0.5 size-4 shrink-0" aria-hidden />
        <p>Your number is used only to sign you in. We never share it.</p>
      </footer>
    </div>
  )
}
