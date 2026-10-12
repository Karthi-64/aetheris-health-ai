import { useState } from 'react'
import { Building2 } from 'lucide-react'
import { safeImageUrl } from '@/lib/safeUrl'
import { hospitalStrings as S } from '@/pages/hospitals/strings'

const frameClass = 'size-16 shrink-0 overflow-hidden rounded-2xl border'

/**
 * A hospital's logo when it has one the app will load, otherwise the first
 * letter of its name. The address is used for nothing but this `<img src>`,
 * and only after `safeImageUrl` has accepted it; a logo that fails to load
 * falls back to the letter as well.
 */
export function HospitalMark({ name, logoUrl }: { name: string; logoUrl: string | null }) {
  const src = safeImageUrl(logoUrl)
  const [failed, setFailed] = useState(false)

  if (src && !failed) {
    return (
      <img
        src={src}
        alt={S.logoAlt(name)}
        width={64}
        height={64}
        decoding="async"
        // The logo's host learns nothing about the page that asked for it.
        referrerPolicy="no-referrer"
        onError={() => setFailed(true)}
        className={`${frameClass} bg-surface-container-lowest block object-contain`}
      />
    )
  }

  // Decorative: the name is right beside it.
  const initial = Array.from(name.trim())[0]?.toUpperCase()
  return (
    <span
      aria-hidden
      className={`${frameClass} bg-secondary-fixed/50 text-secondary font-display text-headline-md flex items-center justify-center`}
    >
      {initial ?? <Building2 className="size-7" />}
    </span>
  )
}
