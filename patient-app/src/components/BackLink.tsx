import type { ReactNode } from 'react'
import { ChevronLeft } from 'lucide-react'
import { Link } from 'react-router-dom'

/** The way back up from a page, as a 44 px target at the top of it. */
export function BackLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="text-secondary text-body-sm -ml-1 inline-flex min-h-11 items-center gap-1 font-semibold hover:underline"
    >
      <ChevronLeft className="size-5 shrink-0" aria-hidden />
      <span className="break-words">{children}</span>
    </Link>
  )
}
