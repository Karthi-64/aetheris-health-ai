import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, RotateCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { formatMoney } from '@/lib/format'
import { useDoctor } from '@/api/doctors'

function initials(name: string): string {
  return name
    .trim()
    .split(/\s+/)
    .slice(-2)
    .map((p) => p[0])
    .join('')
    .toUpperCase()
}

function qualificationList(items: Array<Record<string, unknown>>): string {
  const parts = items
    .map((q) => {
      const degree = typeof q.degree === 'string' ? q.degree : null
      if (!degree) return null
      const inst = typeof q.institution === 'string' ? q.institution : null
      const year = typeof q.year === 'number' ? q.year : null
      const suffix = [inst, year].filter(Boolean).join(', ')
      return suffix ? `${degree} (${suffix})` : degree
    })
    .filter(Boolean)
  return parts.join(' · ')
}

export default function DoctorDetailPage() {
  const { doctorId } = useParams<{ doctorId: string }>()
  const { data: doctor, isError, refetch } = useDoctor(doctorId)

  const backLink = (
    <Link
      to="/doctors"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to doctors
    </Link>
  )

  if (isError) {
    return (
      <div className="w-full space-y-4">
        {backLink}
        <Alert variant="error" title="Couldn't load this doctor">
          <div className="flex flex-col items-start gap-3">
            <p>The record could not be reached. It may have been removed, or the server is down.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      </div>
    )
  }

  if (!doctor) {
    return (
      <div className="w-full space-y-6">
        {backLink}
        <Skeleton className="h-20 w-full max-w-md rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
      </div>
    )
  }

  const quals = qualificationList(doctor.qualifications)

  return (
    <div className="w-full space-y-6">
      {backLink}

      <header className="glassmorphism shadow-glass-panel flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6">
        <div className="flex items-center gap-4">
          <span className="neo-extruded bg-primary-container flex size-14 items-center justify-center rounded-2xl text-lg font-bold text-white">
            {initials(doctor.full_name)}
          </span>
          <div>
            <h1 className="font-display text-headline-md text-primary font-bold">
              {doctor.full_name}
            </h1>
            <p className="font-body text-on-surface-variant text-sm">{doctor.specialization}</p>
          </div>
        </div>
        <Badge variant={doctor.status === 'active' ? 'success' : 'neutral'} className="capitalize">
          {doctor.status}
        </Badge>
      </header>

      <InfoCard title="Practice">
        <Detail label="Department" value={doctor.department_name} />
        <Detail label="Specialization" value={doctor.specialization} />
        <Detail label="Licence" value={<span className="font-mono">{doctor.license_number}</span>} />
        <Detail label="Consultation fee" value={formatMoney(doctor.consultation_fee)} />
        <Detail label="Languages" value={doctor.languages.length ? doctor.languages.join(', ') : null} />
        <Detail label="Email" value={doctor.email} />
      </InfoCard>

      {quals && (
        <InfoCard title="Qualifications">
          <div className="col-span-full">
            <p className="font-body text-body-md text-on-surface">{quals}</p>
          </div>
        </InfoCard>
      )}

      {doctor.bio && (
        <section className="neo-extruded bg-surface rounded-2xl p-6">
          <h2 className="font-display text-title-lg text-primary mb-3 font-bold">About</h2>
          <p className="font-body text-body-md text-on-surface-variant whitespace-pre-line">
            {doctor.bio}
          </p>
        </section>
      )}
    </div>
  )
}
