import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { http } from '@/api/http'
import { ApiError, type ListQueryOptions } from '@/api/types'
import { api } from '@/lib/api'
import { fieldErrorsOf } from '@/lib/apiErrors'
import { blobApiError, filenameFromContentDisposition } from '@/lib/download'

/**
 * Reports and dashboards API — typed hooks over the backend contract
 * (`backend/app/api/v1/reports.py`, `backend/app/schemas/report.py`,
 * `backend/app/services/report_service.py`; docs/modules/10-reports-dashboard.md).
 * Four dashboards under `/dashboards`, four reports and one CSV export under
 * `/reports`. Every route is a `GET`.
 *
 * Not built on the server, so not here: AI summaries, PDF export (`format=pdf`
 * is a 422), scheduled delivery, and the pharmacy, laboratory and low-stock
 * figures.
 *
 * Every figure is computed by the server, in the hospital's timezone and
 * currency (`meta.timezone`, `meta.currency`). Money is a decimal string on
 * the wire ("1850.00", "-350.00") and counts are integers. Nothing here adds,
 * subtracts, averages or re-buckets a figure, and no screen may either: the
 * two sums the dashboards show (`in_clinic`, `to_see`) are server fields.
 * "Today" is `meta.today`, never the browser's clock.
 *
 * Each route needs its own permission code (see the hooks). A query a user may
 * not make must not be sent: pass `{ enabled }` from `usePermissions()`.
 *
 * This file imports nothing from `appointments.ts`, `billing.ts` or
 * `patients.ts` — those import the key objects below to refetch figures after
 * a write, so an import the other way would be a cycle.
 */

// ── Shared types ────────────────────────────────────────────────────────────

/** A decimal string with exactly two decimals; may be negative ("-350.00"). */
export type Money = string
/** A calendar date in the hospital's timezone, `YYYY-MM-DD`. */
export type ISODate = string
/**
 * RFC 3339 in UTC with a "Z" suffix. Parse it (`new Date(...)`); never compare
 * it as text — values read from the database may carry fractional seconds.
 */
export type ISODateTime = string
export type Granularity = 'day' | 'week' | 'month'
export type AppointmentStatus =
  | 'booked'
  | 'checked_in'
  | 'in_progress'
  | 'completed'
  | 'cancelled'
  | 'no_show'
export type AppointmentType = 'new' | 'follow_up' | 'walk_in' | 'emergency'
export type PaymentMethod = 'cash' | 'card' | 'upi' | 'bank_transfer' | 'insurance'
export type ReportId = 'patients' | 'appointments' | 'revenue' | 'outstanding'

/** Sent with every report and dashboard: what the figures are measured in. */
export interface ReportMeta {
  hospital_name: string
  /** The IANA zone every figure was computed in. */
  timezone: string
  /** ISO 4217, e.g. "INR". */
  currency: string
  /** Today in `timezone`. */
  today: ISODate
  generated_at: ISODateTime
}

/**
 * One period of a report. A week starts on Monday. `bucket_start` may be
 * before the requested `from` and `bucket_end` after `to`; when either is,
 * `partial` is true and the figures cover only the part inside the range.
 */
export interface Bucket {
  bucket_start: ISODate
  bucket_end: ISODate
  partial: boolean
}

/** Appointments by status. All six statuses are always present. */
export interface AppointmentCounts {
  total: number
  booked: number
  checked_in: number
  in_progress: number
  completed: number
  cancelled: number
  no_show: number
}

/**
 * Billed, collected and refunded money. Billed is dated by the invoice's issue
 * date; collected and refunded by the day the payment or refund was recorded,
 * so they are not the same cohort. `net_collected_amount` may be negative.
 */
export interface RevenueFigures {
  invoice_count: number
  invoiced_amount: Money
  payment_count: number
  collected_amount: Money
  refund_count: number
  refunded_amount: Money
  net_collected_amount: Money
}

export interface DateRange {
  from: ISODate
  to: ISODate
}

export interface NamedRef {
  id: string
  name: string
}

// ── Reports ─────────────────────────────────────────────────────────────────

export type PatientsBucket = Bucket & { registered: number }

/** `GET /reports/patients` (`report.admin.read`). */
export interface PatientsReport {
  meta: ReportMeta
  /** The period the server resolved, defaults filled in. */
  filters: DateRange & { granularity: Granularity }
  summary: {
    /** Registered in the whole range. Deactivated patients are not counted. */
    registered: number
    /** Active patients as of now, whatever the range. */
    active_total: number
    by_gender: { male: number; female: number; other: number; unspecified: number }
  }
  buckets: PatientsBucket[]
}

export type AppointmentsBucket = Bucket & AppointmentCounts

export interface AppointmentsByDoctorRow {
  doctor_id: string
  doctor_name: string
  department_id: string | null
  department_name: string | null
  total: number
  completed: number
  cancelled: number
  no_show: number
}

/** A null department is the row for doctors with none: show it as "Unassigned". */
export interface AppointmentsByDepartmentRow {
  department_id: string | null
  department_name: string | null
  total: number
  completed: number
  cancelled: number
  no_show: number
}

/** `GET /reports/appointments` (`report.admin.read`). */
export interface AppointmentsReport {
  meta: ReportMeta
  filters: DateRange & {
    granularity: Granularity
    /** The doctor filtered on, deactivated ones included; null for all. */
    doctor: NamedRef | null
    department: NamedRef | null
  }
  summary: AppointmentCounts & {
    /**
     * No-shows as a share of appointments that reached an outcome
     * (completed + no-show), one decimal, e.g. "25.0". Null when none did.
     */
    no_show_rate_percent: string | null
  }
  buckets: AppointmentsBucket[]
  /** Doctors with at least one appointment, busiest first. */
  by_doctor: AppointmentsByDoctorRow[]
  by_department: AppointmentsByDepartmentRow[]
}

export type RevenueBucket = Bucket & RevenueFigures

export interface RevenueByMethodRow {
  method: PaymentMethod
  payment_count: number
  collected_amount: Money
  refund_count: number
  refunded_amount: Money
  net_collected_amount: Money
}

/** `GET /reports/revenue` (`report.admin.read` or `report.billing.read`). */
export interface RevenueReport {
  meta: ReportMeta
  filters: DateRange & { granularity: Granularity }
  summary: RevenueFigures
  /** Always five rows: cash, card, upi, bank_transfer, insurance. */
  by_method: RevenueByMethodRow[]
  buckets: RevenueBucket[]
}

export type AgeingBucketKey = '0_30' | '31_60' | '61_90' | 'over_90'

export interface AgeingBucket {
  bucket: AgeingBucketKey
  min_days: number
  /** Null for the open-ended last bucket. */
  max_days: number | null
  invoice_count: number
  outstanding_amount: Money
}

export interface OutstandingInvoiceRow {
  invoice_id: string
  invoice_number: string
  issued_at: ISODateTime
  /** The issue date in the hospital's timezone. */
  issued_date: ISODate
  age_days: number
  patient_id: string
  patient_name: string
  patient_mrn: string
  status: 'issued' | 'partially_paid'
  total: Money
  amount_paid: Money
  balance_due: Money
}

/**
 * `GET /reports/outstanding` (`report.admin.read` or `report.billing.read`).
 * A point-in-time report: it takes no query parameters.
 */
export interface OutstandingReport {
  meta: ReportMeta
  filters: { as_of_date: ISODate }
  summary: {
    invoice_count: number
    outstanding_amount: Money
    issued_count: number
    partially_paid_count: number
  }
  /** Always four rows, youngest first. */
  ageing: AgeingBucket[]
  /**
   * Oldest first, capped by the server. Show `invoices.length`, never a
   * hard-coded limit.
   */
  invoices: OutstandingInvoiceRow[]
  invoices_total: number
  /** True when `invoices` holds fewer rows than `invoices_total`. */
  invoices_truncated: boolean
}

// ── Dashboards ──────────────────────────────────────────────────────────────

/** `GET /dashboards/admin` (`report.admin.read`). */
export interface AdminDashboard {
  meta: ReportMeta
  /** `in_clinic` is checked in + in progress, added by the server. */
  appointments_today: { date: ISODate; in_clinic: number } & AppointmentCounts
  /** Monday of this week to today. */
  revenue_this_week: DateRange & RevenueFigures
  /** The 1st of this month to today. */
  patient_registrations_this_month: DateRange & { registered: number; active_total: number }
}

export interface DoctorScheduleAppointment {
  appointment_id: string
  scheduled_start: ISODateTime
  scheduled_end: ISODateTime
  status: AppointmentStatus
  type: AppointmentType
  patient_id: string
  patient_name: string
  patient_mrn: string
  checked_in_at: ISODateTime | null
}

/**
 * `GET /dashboards/doctor` (`report.doctor.read`). The caller's own schedule
 * and patients; it carries no money. A caller with no active doctor profile
 * gets a 404 — see {@link isMissingDoctorProfile}.
 */
export interface DoctorDashboard {
  meta: ReportMeta
  doctor: NamedRef
  /** `to_see` is booked + checked in, added by the server. */
  schedule_today: { date: ISODate; to_see: number } & AppointmentCounts & {
      appointments: DoctorScheduleAppointment[]
    }
  my_patients: { count: number }
  /** Monday to Sunday of this week, upcoming days included. */
  this_week: DateRange & AppointmentCounts
}

export interface AtRiskAppointment {
  appointment_id: string
  scheduled_start: ISODateTime
  minutes_late: number
  patient_id: string
  patient_name: string
  patient_mrn: string
  doctor_id: string
  doctor_name: string
}

/** `GET /dashboards/reception` (`report.reception.read`). It carries no money. */
export interface ReceptionDashboard {
  meta: ReportMeta
  schedule_today: { date: ISODate; in_clinic: number } & AppointmentCounts
  walk_in_queue: {
    waiting: number
    not_arrived: number
    in_consultation: number
    /** Null when nobody is waiting. */
    longest_wait_minutes: number | null
  }
  no_show_alerts: {
    marked_today: number
    /** Booked, past their start time and not checked in. Not limited by the list below. */
    at_risk: number
    /** At most 20, earliest first. */
    at_risk_appointments: AtRiskAppointment[]
  }
}

/** `GET /dashboards/billing` (`report.billing.read`). */
export interface BillingDashboard {
  meta: ReportMeta
  unpaid_invoices: {
    invoice_count: number
    outstanding_amount: Money
    issued_count: number
    partially_paid_count: number
  }
  revenue: {
    today: DateRange & RevenueFigures
    this_week: DateRange & RevenueFigures
    this_month: DateRange & RevenueFigures
  }
  discounts_pending_approval: { invoice_count: number; discount_amount: Money }
}

// ── Parameters ──────────────────────────────────────────────────────────────

/**
 * The period of a report. Anything left out is not sent, and the server's
 * default (`to` = today, `from` = `to` minus 29 days, `day`) comes back in
 * `data.filters`.
 */
export interface PeriodParams {
  from?: ISODate
  to?: ISODate
  granularity?: Granularity
}

export type AppointmentsReportParams = PeriodParams & {
  doctor_id?: string
  department_id?: string
}

/** The filters of whichever report is being exported. */
export type ReportExportParams = AppointmentsReportParams

const PERIOD_KEYS = ['from', 'to', 'granularity'] as const
const APPOINTMENT_KEYS = [...PERIOD_KEYS, 'doctor_id', 'department_id'] as const

/**
 * The query parameters each report accepts. The server answers any other key
 * with a 422, so only these are ever sent.
 */
const REPORT_PARAM_KEYS: Record<ReportId, readonly (keyof AppointmentsReportParams)[]> = {
  patients: PERIOD_KEYS,
  appointments: APPOINTMENT_KEYS,
  revenue: PERIOD_KEYS,
  outstanding: [],
}

/** The parameters of `reportId` that have a value: nothing undefined or empty is sent. */
function paramsFor(reportId: ReportId, params: AppointmentsReportParams): AppointmentsReportParams {
  const sent: Record<string, string> = {}
  for (const key of REPORT_PARAM_KEYS[reportId]) {
    const value = params[key]
    if (value !== undefined && value !== '') sent[key] = value
  }
  return sent
}

// ── Keys ────────────────────────────────────────────────────────────────────

/**
 * Report query keys. A write that changes a figure invalidates
 * `reportKeys.all` and `dashboardKeys.all` (see `appointments.ts`,
 * `billing.ts`, `patients.ts`).
 */
export const reportKeys = {
  all: ['reports'] as const,
  patients: (params: PeriodParams = {}) =>
    [...reportKeys.all, 'patients', paramsFor('patients', params)] as const,
  appointments: (params: AppointmentsReportParams = {}) =>
    [...reportKeys.all, 'appointments', paramsFor('appointments', params)] as const,
  revenue: (params: PeriodParams = {}) =>
    [...reportKeys.all, 'revenue', paramsFor('revenue', params)] as const,
  outstanding: () => [...reportKeys.all, 'outstanding'] as const,
}

export const dashboardKeys = {
  all: ['dashboards'] as const,
  admin: () => [...dashboardKeys.all, 'admin'] as const,
  doctor: () => [...dashboardKeys.all, 'doctor'] as const,
  reception: () => [...dashboardKeys.all, 'reception'] as const,
  billing: () => [...dashboardKeys.all, 'billing'] as const,
}

// ── Report hooks ────────────────────────────────────────────────────────────

const REPORT_STALE_MS = 30_000
const DASHBOARD_STALE_MS = 60_000

/** Patients registered per period (`report.admin.read`). */
export function usePatientsReport(params: PeriodParams = {}, options: ListQueryOptions = {}) {
  const sent = paramsFor('patients', params)
  return useQuery({
    queryKey: reportKeys.patients(sent),
    queryFn: () => http.get<PatientsReport>('/reports/patients', { params: sent }),
    enabled: options.enabled ?? true,
    staleTime: REPORT_STALE_MS,
    placeholderData: keepPreviousData,
  })
}

/**
 * Appointments per period, by doctor and by department (`report.admin.read`).
 * An unknown `doctor_id` or `department_id` is a 422.
 */
export function useAppointmentsReport(
  params: AppointmentsReportParams = {},
  options: ListQueryOptions = {},
) {
  const sent = paramsFor('appointments', params)
  return useQuery({
    queryKey: reportKeys.appointments(sent),
    queryFn: () => http.get<AppointmentsReport>('/reports/appointments', { params: sent }),
    enabled: options.enabled ?? true,
    staleTime: REPORT_STALE_MS,
    placeholderData: keepPreviousData,
  })
}

/** Billed, collected and refunded per period (`report.admin.read` or `report.billing.read`). */
export function useRevenueReport(params: PeriodParams = {}, options: ListQueryOptions = {}) {
  const sent = paramsFor('revenue', params)
  return useQuery({
    queryKey: reportKeys.revenue(sent),
    queryFn: () => http.get<RevenueReport>('/reports/revenue', { params: sent }),
    enabled: options.enabled ?? true,
    staleTime: REPORT_STALE_MS,
    placeholderData: keepPreviousData,
  })
}

/**
 * Unpaid invoices as of now (`report.admin.read` or `report.billing.read`).
 * The route takes no query parameters, so this hook takes none.
 */
export function useOutstandingReport(options: ListQueryOptions = {}) {
  return useQuery({
    queryKey: reportKeys.outstanding(),
    queryFn: () => http.get<OutstandingReport>('/reports/outstanding'),
    enabled: options.enabled ?? true,
    staleTime: REPORT_STALE_MS,
    placeholderData: keepPreviousData,
  })
}

// ── Dashboard hooks ─────────────────────────────────────────────────────────

/** The hospital-wide tiles (`report.admin.read`). */
export function useAdminDashboard(options: ListQueryOptions = {}) {
  return useQuery({
    queryKey: dashboardKeys.admin(),
    queryFn: () => http.get<AdminDashboard>('/dashboards/admin'),
    enabled: options.enabled ?? true,
    staleTime: DASHBOARD_STALE_MS,
  })
}

/**
 * The signed-in doctor's own schedule (`report.doctor.read`). A 404 means the
 * account has no active doctor profile; that is an answer, not a fault, so it
 * is not retried.
 */
export function useDoctorDashboard(options: ListQueryOptions = {}) {
  return useQuery({
    queryKey: dashboardKeys.doctor(),
    queryFn: () => http.get<DoctorDashboard>('/dashboards/doctor'),
    enabled: options.enabled ?? true,
    staleTime: DASHBOARD_STALE_MS,
    retry: false,
  })
}

/** Today's schedule, the walk-in queue and possible no-shows (`report.reception.read`). */
export function useReceptionDashboard(options: ListQueryOptions = {}) {
  return useQuery({
    queryKey: dashboardKeys.reception(),
    queryFn: () => http.get<ReceptionDashboard>('/dashboards/reception'),
    enabled: options.enabled ?? true,
    staleTime: DASHBOARD_STALE_MS,
  })
}

/** Unpaid invoices, revenue and discounts awaiting approval (`report.billing.read`). */
export function useBillingDashboard(options: ListQueryOptions = {}) {
  return useQuery({
    queryKey: dashboardKeys.billing(),
    queryFn: () => http.get<BillingDashboard>('/dashboards/billing'),
    enabled: options.enabled ?? true,
    staleTime: DASHBOARD_STALE_MS,
  })
}

// ── CSV export ──────────────────────────────────────────────────────────────

export interface ReportExportFile {
  blob: Blob
  filename: string
}

/**
 * The period the page is showing, as the server resolved it (`data.filters`).
 * Only used to name the file when the server's own name cannot be read.
 */
export interface ReportExportPeriod {
  from?: ISODate
  to?: ISODate
  as_of_date?: ISODate
}

const pad = (n: number) => String(n).padStart(2, '0')

/**
 * A filename of the shape the server uses (`revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv`,
 * `outstanding-report-as-of-2026-10-06-20261006-083211.csv`), for when the
 * `Content-Disposition` header is not readable. The stamp is `now` in UTC.
 * Without a resolved period the name carries the stamp alone.
 */
export function reportExportFilename(
  reportId: ReportId,
  period: ReportExportPeriod = {},
  now: Date = new Date(),
): string {
  const stamp =
    `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}` +
    `-${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`
  let scope = ''
  if (reportId === 'outstanding') {
    if (period.as_of_date) scope = `as-of-${period.as_of_date}-`
  } else if (period.from && period.to) {
    scope = `${period.from}_to_${period.to}-`
  }
  return `${reportId}-report-${scope}${stamp}.csv`
}

/**
 * Fetch a report as a CSV file (requires `report.export` and the report's own
 * read code). `params` are the filters the page is showing; only the ones that
 * report accepts are sent, with `format=csv`.
 *
 * The response is the file itself, not an API envelope, so this uses the raw
 * Axios instance. The server names the file in `Content-Disposition`; where
 * that header is not readable (a cross-origin API does not expose it) a name
 * of the same shape is built from `period`.
 *
 * A failure is thrown as an `ApiError` carrying the server's message — a 403
 * for a missing permission, a 422 for a bad filter.
 */
export async function fetchReportExport(
  reportId: ReportId,
  params: ReportExportParams = {},
  period?: ReportExportPeriod,
): Promise<ReportExportFile> {
  try {
    const res = await api.get<Blob>(`/reports/${reportId}/export`, {
      params: { format: 'csv', ...paramsFor(reportId, params) },
      responseType: 'blob',
    })
    const headers = res.headers as Record<string, unknown> | undefined
    return {
      blob: res.data,
      filename:
        filenameFromContentDisposition(headers?.['content-disposition']) ??
        reportExportFilename(reportId, period),
    }
  } catch (err) {
    throw await blobApiError(err)
  }
}

export interface ExportReportInput {
  reportId: ReportId
  params?: ReportExportParams
  /** `data.filters` of the report on screen, for the fallback filename. */
  period?: ReportExportPeriod
}

/** Export a report and return the file for the caller to save. */
export function useExportReport() {
  return useMutation({
    mutationFn: ({ reportId, params, period }: ExportReportInput) =>
      fetchReportExport(reportId, params, period),
  })
}

// ── Errors ──────────────────────────────────────────────────────────────────

/** What a screen says when a failure carries nothing written for a user. */
export const REPORT_ERROR_FALLBACK = "Couldn't load this. Try again."

/** FastAPI's message for a malformed parameter; the useful text is in `errors`. */
const GENERIC_VALIDATION_MESSAGE = 'Validation failed.'

/**
 * The message to show for a failed report, dashboard or export call.
 *
 * The API writes its 400, 403, 404 and 422 messages for users, so those are
 * shown as sent — except a 422 whose message is exactly "Validation failed.",
 * where the first field error says what was wrong. Anything else (a 5xx, a
 * dropped connection, a value that is not an `ApiError`) gets `fallback`
 * rather than internal text.
 */
export function apiErrorMessage(err: unknown, fallback: string = REPORT_ERROR_FALLBACK): string {
  if (!(err instanceof ApiError) || ![400, 403, 404, 422].includes(err.status ?? 0)) {
    return fallback
  }
  if (err.message === GENERIC_VALIDATION_MESSAGE) {
    return fieldErrorsOf(err)[0]?.message ?? fallback
  }
  return err.message || fallback
}

/**
 * True for the doctor dashboard's 404: the account holds `report.doctor.read`
 * but has no active doctor profile. Show a note, not an error.
 */
export function isMissingDoctorProfile(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404
}
