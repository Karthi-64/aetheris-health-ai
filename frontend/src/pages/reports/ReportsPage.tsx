import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { usePermissions } from '@/hooks/usePermissions'
import { ReportsTabs } from './ReportsTabs'
import { REPORT_SECTIONS, holdsAny } from './reportSections'

/**
 * The Reports landing page: one link per report this user may open. It makes
 * no request of its own — each report asks for its own figures when opened.
 */
export default function ReportsPage() {
  const { can } = usePermissions()
  const reports = REPORT_SECTIONS.filter((s) => s.description && holdsAny(can, s.anyOf))

  return (
    <div className="w-full">
      <ReportsTabs />
      <PageHeader
        title="Reports"
        subtitle="Registrations, appointments, revenue and unpaid invoices for your hospital."
      />

      {reports.length === 0 ? (
        <Alert variant="error" title="You can't view reports">
          Your account is not permitted to open any report. If that has just changed, sign in again.
        </Alert>
      ) : (
        <ul aria-label="Reports" className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {reports.map((report) => (
            <li key={report.to} className="min-w-0">
              <Link
                to={report.to}
                aria-label={`${report.label} report`}
                className="neo-extruded bg-surface focus-visible:ring-secondary group flex h-full items-start justify-between gap-4 rounded-2xl p-5 transition-shadow outline-none hover:shadow-md focus-visible:ring-2"
              >
                <span className="min-w-0">
                  <span className="font-display text-title-lg text-primary block font-bold">{report.label}</span>
                  <span className="font-body text-body-sm text-on-surface-variant mt-1 block">
                    {report.description}
                  </span>
                </span>
                <ArrowRight
                  className="text-outline group-hover:text-secondary mt-1 size-5 shrink-0 transition-colors"
                  aria-hidden
                />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
