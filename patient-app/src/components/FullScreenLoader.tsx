import { Loader2 } from 'lucide-react'

/** Whole-screen wait, for the moments before any page can be shown. */
export function FullScreenLoader({ label }: { label: string }) {
  return (
    <div role="status" className="bg-background flex min-h-dvh flex-col items-center justify-center gap-4">
      <Loader2 className="text-secondary size-8 animate-spin motion-reduce:animate-none" aria-hidden />
      <p className="text-body-sm text-on-surface-variant">{label}</p>
    </div>
  )
}
