import type { ReactNode } from 'react'
import { Stethoscope } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button, Skeleton } from '@atheris/ui'
import { useDoctor, type PatientDoctor } from '@/api/doctors'
import { useHospital, type PatientHospital } from '@/api/hospitals'
import { BackLink } from '@/components/BackLink'
import { PageHeading } from '@/components/PageHeading'
import { doctorStrings as S } from '@/pages/doctors/strings'
import { HospitalLoader } from '@/pages/hospitals/HospitalLoader'
import { loadFailureOf, type LoadFailure } from '@/pages/hospitals/loadFailure'
import { LoadProblem } from '@/pages/hospitals/LoadProblem'
import { hospitalDoctorsPath } from '@/pages/hospitals/paths'

interface DoctorLoaderProps {
  hospitalRef: string
  doctorRef: string
  /** The page's title; it is given the doctor once there is one. */
  title: (doctor: PatientDoctor | undefined) => string
  children: (doctor: PatientDoctor, hospital: PatientHospital) => ReactNode
}

/**
 * Loads one doctor of one hospital for a page and shows every state but
 * success. The hospital comes first, through the loader every hospital page
 * shares: an unknown hospital is its "not available" page, and nothing is
 * asked about a doctor there. Then the doctor: loading, not available, no
 * connection, and a failure with a retry. The pages under
 * `/hospitals/:hospitalRef/doctors/:doctorRef` share it, so they answer the
 * same way.
 */
export function DoctorLoader({ hospitalRef, doctorRef, title, children }: DoctorLoaderProps) {
  // The same read the hospital loader makes: one request, two readers. The
  // doctor is asked for by the reference the server gave for the hospital.
  const hospital = useHospital(hospitalRef)
  const doctor = useDoctor(hospital.data?.ref ?? '', doctorRef)

  const problem = (found: PatientHospital, failure: LoadFailure) => (
    <div className="space-y-4">
      <BackLink to={hospitalDoctorsPath(found.ref)}>
        {found.name ? S.allDoctors(found.name) : S.allDoctorsFallback}
      </BackLink>
      <PageHeading>{S.profileTitle}</PageHeading>
      <LoadProblem
        failure={failure}
        failedMessage={S.doctorFailed}
        onRetry={() => void doctor.refetch()}
        isRetrying={doctor.isFetching}
      />
    </div>
  )

  const withHospital = (found: PatientHospital) => {
    if (doctor.isPending) {
      // A request held back because the browser is offline never fails: it waits.
      if (doctor.fetchStatus === 'paused') return problem(found, 'offline')
      return (
        <div role="status" aria-label={S.loadingDoctor} className="space-y-3 pt-11">
          <Skeleton className="h-8 w-3/4" />
          <Skeleton className="h-5 w-1/2" />
          <Skeleton className="h-32 w-full rounded-2xl" />
        </div>
      )
    }

    if (doctor.isError) {
      const failure = loadFailureOf(doctor.error)
      return failure === 'not_found' ? <DoctorNotAvailable hospital={found} /> : problem(found, failure)
    }

    return children(doctor.data, found)
  }

  return (
    <HospitalLoader hospitalRef={hospitalRef} title={() => title(doctor.data)}>
      {withHospital}
    </HospitalLoader>
  )
}

/**
 * Unknown, not listed, at another hospital: the server says one thing and so
 * does the app. `focusOnMount` is for a page that learns it only after the
 * patient acted on it.
 */
export function DoctorNotAvailable({ hospital, focusOnMount }: { hospital: PatientHospital; focusOnMount?: boolean }) {
  return (
    <div className="bg-card flex flex-col items-center gap-4 rounded-2xl border px-6 py-10 text-center">
      <span className="bg-surface-container text-outline flex size-14 items-center justify-center rounded-2xl">
        <Stethoscope className="size-7" aria-hidden />
      </span>
      <div className="space-y-1">
        <PageHeading className="text-title-lg" focusOnMount={focusOnMount}>
          {S.notFoundTitle}
        </PageHeading>
        <p className="text-body-sm text-on-surface-variant mx-auto max-w-sm">{S.notFoundBody}</p>
      </div>
      <Button asChild size="touch">
        <Link to={hospitalDoctorsPath(hospital.ref)}>{S.browseDoctors}</Link>
      </Button>
    </div>
  )
}
