import type { PatientHospital } from '@/api/hospitals'
import { hospitalStrings as S } from '@/pages/hospitals/strings'

/**
 * The marks a hospital can carry: "Linked" for the patient's own link, and
 * "Sponsored" for a promoted listing. No hospital is promoted today, but the
 * label is not optional — a promoted hospital is never shown without it.
 */
export function HospitalBadges({ hospital }: { hospital: Pick<PatientHospital, 'linked' | 'listing'> }) {
  const isPromoted = hospital.listing === 'promoted'
  if (!hospital.linked && !isPromoted) return null
  return (
    <span className="flex flex-wrap gap-2">
      {isPromoted && (
        <span className="border-outline text-on-surface-variant text-body-sm rounded-full border px-2.5 py-0.5 font-medium">
          {S.sponsored}
        </span>
      )}{' '}
      {hospital.linked && (
        <span className="bg-secondary-fixed/60 text-on-secondary-container text-body-sm rounded-full px-2.5 py-0.5 font-medium">
          {S.linked}
        </span>
      )}
    </span>
  )
}
