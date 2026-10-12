import type { ReactNode } from 'react'
import { Building2, CalendarClock, GraduationCap, Languages, Layers, Stethoscope, type LucideIcon } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { Button } from '@atheris/ui'
import type { PatientDoctor } from '@/api/doctors'
import type { PatientHospital } from '@/api/hospitals'
import { BackLink } from '@/components/BackLink'
import { PageHeading } from '@/components/PageHeading'
import { DoctorLoader } from '@/pages/doctors/DoctorLoader'
import { doctorAvailabilityPath } from '@/pages/doctors/paths'
import { doctorStrings as S } from '@/pages/doctors/strings'
import { hospitalDoctorsPath, hospitalPath } from '@/pages/hospitals/paths'

/** One doctor: who they are, where they work, and the way on to their availability. */
export function DoctorProfilePage() {
  const { hospitalRef = '', doctorRef = '' } = useParams()
  return (
    // Keyed by the doctor, so moving to another one starts a fresh page.
    <DoctorLoader
      key={`${hospitalRef}/${doctorRef}`}
      hospitalRef={hospitalRef}
      doctorRef={doctorRef}
      title={(doctor) => doctor?.name || S.profileTitle}
    >
      {(doctor, hospital) => <DoctorProfile doctor={doctor} hospital={hospital} />}
    </DoctorLoader>
  )
}

function DoctorProfile({ doctor, hospital }: { doctor: PatientDoctor; hospital: PatientHospital }) {
  return (
    <div className="space-y-6">
      <BackLink to={hospitalDoctorsPath(hospital.ref)}>
        {hospital.name ? S.allDoctors(hospital.name) : S.allDoctorsFallback}
      </BackLink>

      <header className="flex items-start gap-4">
        {/* Decorative: there is no photo of a doctor to show. */}
        <span
          aria-hidden
          className="bg-secondary-fixed/50 text-secondary flex size-16 shrink-0 items-center justify-center rounded-2xl border"
        >
          <Stethoscope className="size-7" />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <PageHeading>{doctor.name}</PageHeading>
          {doctor.specialization && (
            <p className="text-body-lg text-on-surface-variant break-words">{doctor.specialization}</p>
          )}
        </div>
      </header>

      <dl className="bg-card divide-y rounded-2xl border">
        {doctor.department && (
          <DetailRow icon={Layers} label={S.departmentLabel}>
            {doctor.department.name}
          </DetailRow>
        )}
        <DetailRow icon={Building2} label={S.hospitalLabel}>
          <Link
            to={hospitalPath(hospital.ref)}
            className="text-secondary -my-2.5 inline-flex min-h-11 items-center font-semibold underline underline-offset-4"
          >
            {hospital.name || S.hospitalFallback}
          </Link>
        </DetailRow>
        {doctor.languages.length > 0 && (
          <DetailRow icon={Languages} label={S.languagesLabel}>
            {doctor.languages.join(S.listSeparator)}
          </DetailRow>
        )}
        {doctor.qualifications.length > 0 && (
          <DetailRow icon={GraduationCap} label={S.qualificationsLabel}>
            <ul className="space-y-2">
              {doctor.qualifications.map((qualification, index) => {
                const from = [qualification.institution, qualification.year].filter((part) => part !== null)
                return (
                  <li key={index}>
                    <span className="block font-semibold break-words">{qualification.degree}</span>
                    {from.length > 0 && (
                      <span className="text-body-sm text-on-surface-variant block break-words">
                        {from.join(S.listSeparator)}
                      </span>
                    )}
                  </li>
                )
              })}
            </ul>
          </DetailRow>
        )}
      </dl>

      {doctor.bio && (
        <section className="space-y-2">
          <h2 className="font-display text-title-lg text-primary">{S.aboutHeading}</h2>
          {/* Plain text: line breaks are kept by CSS, and nothing in it is markup. */}
          <p className="text-body-lg text-on-surface break-words whitespace-pre-line">{doctor.bio}</p>
        </section>
      )}

      <Button asChild size="touch" className="w-full">
        <Link to={doctorAvailabilityPath(hospital.ref, doctor.ref)}>
          <CalendarClock aria-hidden />
          {S.viewAvailability}
        </Link>
      </Button>
    </div>
  )
}

function DetailRow({ icon: Icon, label, children }: { icon: LucideIcon; label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 p-4">
      <Icon className="text-secondary mt-0.5 size-5 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        <dt className="text-body-sm text-on-surface-variant">{label}</dt>
        <dd className="text-body-lg text-on-surface break-words">{children}</dd>
      </div>
    </div>
  )
}
