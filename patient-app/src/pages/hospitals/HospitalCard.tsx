import { useId } from 'react'
import { Building2, ChevronRight } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { PatientHospital } from '@/api/hospitals'
import { locationLine } from '@/pages/hospitals/address'
import { HospitalBadges } from '@/pages/hospitals/HospitalBadges'
import { hospitalPath } from '@/pages/hospitals/paths'

/**
 * One hospital in the list. It shows the name, where it is, its phone and its
 * marks, and nothing else — no logo, and never a distance: the app does not
 * know where the patient is. The whole card is the link to the hospital's page.
 */
export function HospitalCard({ hospital }: { hospital: PatientHospital }) {
  const id = useId()
  const location = locationLine(hospital.address)

  return (
    <li>
      <Link
        to={hospitalPath(hospital.ref)}
        aria-labelledby={`${id}-name`}
        aria-describedby={`${id}-details`}
        className="bg-card hover:border-secondary flex min-h-20 items-start gap-3 rounded-2xl border p-4 transition-colors"
      >
        <span className="bg-secondary-fixed/50 text-secondary flex size-11 shrink-0 items-center justify-center rounded-xl">
          <Building2 className="size-5" aria-hidden />
        </span>
        <span className="min-w-0 flex-1 space-y-1">
          <span id={`${id}-name`} className="text-body-lg text-on-surface block font-semibold break-words">
            {hospital.name}
          </span>
          {/* The spaces are for a screen reader, which reads these lines as one description. */}
          <span id={`${id}-details`} className="block space-y-1.5">
            {location && <span className="text-body-sm text-on-surface-variant block break-words">{location}</span>}{' '}
            {hospital.phone && <span className="text-body-sm text-on-surface-variant block">{hospital.phone}</span>}{' '}
            <HospitalBadges hospital={hospital} />
          </span>
        </span>
        <ChevronRight className="text-outline mt-3 size-5 shrink-0" aria-hidden />
      </Link>
    </li>
  )
}
