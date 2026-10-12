import { useId } from 'react'
import { Stethoscope } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '@atheris/ui'
import type { PatientDoctor } from '@/api/doctors'
import { doctorPath } from '@/pages/doctors/paths'
import { doctorStrings as S } from '@/pages/doctors/strings'

/**
 * One doctor in a hospital's list: the name, the specialisation, the
 * department, the degrees and the languages — what the API gives, and nothing
 * else. There is no photo, rating, fee, experience or availability to show,
 * so none is. The reference is used for the link and never shown.
 */
export function DoctorCard({ hospitalRef, doctor }: { hospitalRef: string; doctor: PatientDoctor }) {
  const id = useId()
  const degrees = doctor.qualifications.map((qualification) => qualification.degree).join(S.listSeparator)
  const languages = doctor.languages.join(S.listSeparator)

  return (
    <li className="bg-card space-y-4 rounded-2xl border p-4">
      <div className="flex items-start gap-3">
        <span className="bg-secondary-fixed/50 text-secondary flex size-11 shrink-0 items-center justify-center rounded-xl">
          <Stethoscope className="size-5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <h3 id={`${id}-name`} className="text-body-lg text-on-surface font-semibold break-words">
            {doctor.name}
          </h3>
          {doctor.specialization && (
            <p className="text-body-sm text-on-surface break-words">{doctor.specialization}</p>
          )}
          {doctor.department && (
            <p className="text-body-sm text-on-surface-variant break-words">{doctor.department.name}</p>
          )}
          {(degrees || languages) && (
            <dl className="space-y-1 pt-1">
              {degrees && <CardFact label={S.qualificationsLabel}>{degrees}</CardFact>}
              {languages && <CardFact label={S.languagesLabel}>{languages}</CardFact>}
            </dl>
          )}
        </div>
      </div>
      <Button asChild variant="outline" size="touch" className="w-full">
        {/* Named with the doctor, so twenty of these are not twenty identical links. */}
        <Link
          id={`${id}-profile`}
          to={doctorPath(hospitalRef, doctor.ref)}
          aria-labelledby={`${id}-profile ${id}-name`}
        >
          {S.viewProfile}
        </Link>
      </Button>
    </li>
  )
}

function CardFact({ label, children }: { label: string; children: string }) {
  return (
    <div className="text-body-sm">
      <dt className="text-on-surface-variant inline font-medium">{label}: </dt>
      <dd className="text-on-surface inline break-words">{children}</dd>
    </div>
  )
}
