import type { ReactNode } from 'react'
import { Clock, MapPin, Phone, Stethoscope, type LucideIcon } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { Button } from '@atheris/ui'
import type { PatientHospital } from '@/api/hospitals'
import { BackLink } from '@/components/BackLink'
import { PageHeading } from '@/components/PageHeading'
import { addressLines, telHref } from '@/pages/hospitals/address'
import { HospitalBadges } from '@/pages/hospitals/HospitalBadges'
import { HospitalLoader } from '@/pages/hospitals/HospitalLoader'
import { HospitalMark } from '@/pages/hospitals/HospitalMark'
import { linkStateFor } from '@/pages/hospitals/linkState'
import { hospitalDoctorsPath } from '@/pages/hospitals/paths'
import { hospitalStrings as S } from '@/pages/hospitals/strings'

/** One hospital: how to reach it, and the ways on from here. */
export function HospitalDetailPage() {
  const { hospitalRef = '' } = useParams()
  return (
    // Keyed by the hospital, so moving to another one starts a fresh page.
    <HospitalLoader key={hospitalRef} hospitalRef={hospitalRef} title={(hospital) => hospital?.name || S.detailTitle}>
      {(hospital) => <HospitalDetail hospital={hospital} />}
    </HospitalLoader>
  )
}

function HospitalDetail({ hospital }: { hospital: PatientHospital }) {
  const address = addressLines(hospital.address)
  const callHref = telHref(hospital.phone)
  const hasDetails = address.length > 0 || !!hospital.phone || !!hospital.timezone

  return (
    <div className="space-y-6">
      <BackLink to="/hospitals">{S.allHospitals}</BackLink>

      <header className="flex items-start gap-4">
        <HospitalMark name={hospital.name} logoUrl={hospital.logo_url} />
        <div className="min-w-0 flex-1 space-y-2">
          <PageHeading>{hospital.name}</PageHeading>
          <HospitalBadges hospital={hospital} />
        </div>
      </header>

      {hospital.linked && <p className="text-body-lg text-on-surface-variant">{S.linkedNote}</p>}

      {hasDetails && (
        <dl className="bg-card divide-y rounded-2xl border">
          {address.length > 0 && (
            <DetailRow icon={MapPin} label={S.addressLabel}>
              {address.map((line, index) => (
                <span key={index} className="block break-words">
                  {line}
                </span>
              ))}
            </DetailRow>
          )}
          {hospital.phone && (
            <DetailRow icon={Phone} label={S.phoneLabel}>
              {callHref ? (
                <a
                  href={callHref}
                  className="text-secondary -my-2.5 inline-flex min-h-11 items-center font-semibold underline underline-offset-4"
                >
                  {hospital.phone}
                </a>
              ) : (
                hospital.phone
              )}
            </DetailRow>
          )}
          {hospital.timezone && (
            <DetailRow icon={Clock} label={S.timezoneLabel}>
              {hospital.timezone.replaceAll('_', ' ')}
            </DetailRow>
          )}
        </dl>
      )}

      <div className="flex flex-col gap-3">
        <Button asChild size="touch" className="w-full">
          <Link to={hospitalDoctorsPath(hospital.ref)}>
            <Stethoscope aria-hidden />
            {S.viewDoctors}
          </Link>
        </Button>
        {!hospital.linked && (
          <Button asChild variant="outline" size="touch" className="w-full">
            {/* The code travels in router state; the link form still lets it be changed. */}
            <Link to="/link-patient" state={linkStateFor(hospital.ref)}>
              {S.linkRecord}
            </Link>
          </Button>
        )}
      </div>
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
