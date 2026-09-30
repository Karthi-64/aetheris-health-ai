import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, RotateCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { formatDate } from '@/lib/format'
import { usePatient, type Patient } from '@/api/patients'

const GENDER_LABEL: Record<Patient['gender'], string> = {
  male: 'Male',
  female: 'Female',
  other: 'Other',
  unspecified: 'Unspecified',
}

function formatAddress(address: Record<string, unknown> | null): string | null {
  if (!address) return null
  const parts = ['line1', 'line2', 'city', 'state', 'postal_code', 'country']
    .map((k) => address[k])
    .filter((v): v is string => typeof v === 'string' && v.length > 0)
  return parts.length ? parts.join(', ') : null
}

function names(items: Array<Record<string, unknown>>): string {
  const list = items.map((i) => (typeof i.name === 'string' ? i.name : null)).filter(Boolean)
  return list.length ? list.join(', ') : ''
}

export default function PatientDetailPage() {
  const { patientId } = useParams<{ patientId: string }>()
  const { data: patient, isError, refetch } = usePatient(patientId ?? '')

  const backLink = (
    <Link
      to="/patients"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to patients
    </Link>
  )

  if (isError) {
    return (
      <div className="w-full space-y-4">
        {backLink}
        <Alert variant="error" title="Couldn't load this patient">
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

  if (!patient) {
    return (
      <div className="w-full space-y-6">
        {backLink}
        <Skeleton className="h-20 w-full max-w-md rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
      </div>
    )
  }

  const allergies = names(patient.allergies)
  const conditions = names(patient.chronic_conditions)
  const medications = names(patient.current_medications)
  const hasHistory = allergies || conditions || medications

  return (
    <div className="w-full space-y-6">
      {backLink}

      <header className="glassmorphism shadow-glass-panel flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6">
        <div className="flex items-center gap-4">
          <span className="neo-extruded bg-primary-container flex size-14 items-center justify-center rounded-2xl text-lg font-bold text-white">
            {patient.first_name[0]}
            {patient.last_name[0]}
          </span>
          <div>
            <h1 className="font-display text-headline-md text-primary font-bold">
              {patient.full_name}
            </h1>
            <p className="font-mono text-outline text-sm">{patient.mrn}</p>
          </div>
        </div>
        <Badge variant={patient.status === 'active' ? 'success' : 'neutral'} className="capitalize">
          {patient.status}
        </Badge>
      </header>

      <InfoCard title="Demographics">
        <Detail label="Date of birth" value={formatDate(patient.date_of_birth)} />
        <Detail label="Age" value={`${patient.age} years`} />
        <Detail label="Gender" value={GENDER_LABEL[patient.gender]} />
        <Detail label="Blood group" value={patient.blood_group} />
        <Detail label="Marital status" value={patient.marital_status} />
        <Detail label="Occupation" value={patient.occupation} />
      </InfoCard>

      <InfoCard title="Contact">
        <Detail label="Phone" value={patient.phone} />
        <Detail label="Email" value={patient.email} />
        <Detail label="Address" value={formatAddress(patient.address)} />
      </InfoCard>

      {hasHistory && (
        <InfoCard title="Medical history">
          <Detail label="Allergies" value={allergies || null} />
          <Detail label="Chronic conditions" value={conditions || null} />
          <Detail label="Current medications" value={medications || null} />
        </InfoCard>
      )}
    </div>
  )
}
