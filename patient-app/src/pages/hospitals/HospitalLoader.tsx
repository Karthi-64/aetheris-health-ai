import type { ReactNode } from 'react'
import { Building2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button, Skeleton } from '@atheris/ui'
import { useHospital, type PatientHospital } from '@/api/hospitals'
import { BackLink } from '@/components/BackLink'
import { PageHeading } from '@/components/PageHeading'
import { usePageTitle } from '@/lib/usePageTitle'
import { loadFailureOf, type LoadFailure } from '@/pages/hospitals/loadFailure'
import { LoadProblem } from '@/pages/hospitals/LoadProblem'
import { hospitalStrings as S } from '@/pages/hospitals/strings'

interface HospitalLoaderProps {
  hospitalRef: string
  /** The page's title; it is given the hospital once there is one. */
  title: (hospital: PatientHospital | undefined) => string
  children: (hospital: PatientHospital) => ReactNode
}

/**
 * Loads one hospital for a page and shows every state but success: loading,
 * not available, no connection, and a failure with a retry. The pages under
 * `/hospitals/:hospitalRef` share it, so they answer the same way.
 */
export function HospitalLoader({ hospitalRef, title, children }: HospitalLoaderProps) {
  const hospital = useHospital(hospitalRef)
  usePageTitle(title(hospital.data))

  const problem = (failure: LoadFailure) => (
    <div className="space-y-4">
      <BackLink to="/hospitals">{S.allHospitals}</BackLink>
      <PageHeading>{S.detailTitle}</PageHeading>
      <LoadProblem
        failure={failure}
        failedMessage={S.hospitalFailed}
        onRetry={() => void hospital.refetch()}
        isRetrying={hospital.isFetching}
      />
    </div>
  )

  if (hospital.isPending) {
    // A request held back because the browser is offline never fails: it waits.
    if (hospital.fetchStatus === 'paused') return problem('offline')
    return (
      <div role="status" aria-label={S.loadingHospital} className="space-y-3 pt-11">
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="h-5 w-1/2" />
      </div>
    )
  }

  if (hospital.isError) {
    const failure = loadFailureOf(hospital.error)
    return failure === 'not_found' ? <NotAvailable /> : problem(failure)
  }

  return children(hospital.data)
}

/** Unknown, switched off, never existed: the server says one thing and so does the app. */
function NotAvailable() {
  return (
    <div className="bg-card flex flex-col items-center gap-4 rounded-2xl border px-6 py-10 text-center">
      <span className="bg-surface-container text-outline flex size-14 items-center justify-center rounded-2xl">
        <Building2 className="size-7" aria-hidden />
      </span>
      <div className="space-y-1">
        <PageHeading className="text-title-lg">{S.notFoundTitle}</PageHeading>
        <p className="text-body-sm text-on-surface-variant mx-auto max-w-sm">{S.notFoundBody}</p>
      </div>
      <Button asChild size="touch">
        <Link to="/hospitals">{S.browseHospitals}</Link>
      </Button>
    </div>
  )
}
