import { createElement, type ReactNode } from 'react'
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import { useAppointmentTransition } from '@/api/appointments'
import { useIssueInvoice } from '@/api/billing'
import { useCreatePatient, type CreatePatientInput } from '@/api/patients'
import { ApiError } from '@/api/types'
import { fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import {
  adminDashboardFixture,
  appointmentsReportFixture,
  billingDashboardFixture,
  doctorDashboardFixture,
  outstandingReportFixture,
  patientsReportFixture,
  receptionDashboardFixture,
  revenueReportFixture,
} from '@/test/reportFixtures'
import {
  apiErrorMessage,
  dashboardKeys,
  fetchReportExport,
  isMissingDoctorProfile,
  REPORT_ERROR_FALLBACK,
  reportExportFilename,
  reportKeys,
  useAdminDashboard,
  useAppointmentsReport,
  useBillingDashboard,
  useDoctorDashboard,
  useExportReport,
  useOutstandingReport,
  usePatientsReport,
  useReceptionDashboard,
  useRevenueReport,
} from './reports'

/**
 * The reports client against the contract's exact shapes. The real hooks,
 * `http` wrapper and Axios instance run; only the network adapter is replaced
 * by an in-memory server that answers each route with the contract's example
 * payload and, like the real one, refuses a query parameter it does not know.
 */

const DOCTOR_ID = '5b0c1c7e-2f0a-4d0e-9c53-0a1d6f3b9a11'
const DEPARTMENT_ID = 'c2a4e0f1-7b55-4a0c-8f0e-3d2b1a9e6c01'
const CSV = '﻿Hospital,Demo Hospital\r\nReport,Revenue\r\n'

const PERIOD = ['from', 'to', 'granularity']
const ROUTES: Record<string, { data: unknown; allowed: string[] }> = {
  '/reports/patients': { data: patientsReportFixture, allowed: PERIOD },
  '/reports/appointments': { data: appointmentsReportFixture, allowed: [...PERIOD, 'doctor_id', 'department_id'] },
  '/reports/revenue': { data: revenueReportFixture, allowed: PERIOD },
  '/reports/outstanding': { data: outstandingReportFixture, allowed: [] },
  '/dashboards/admin': { data: adminDashboardFixture, allowed: [] },
  '/dashboards/doctor': { data: doctorDashboardFixture, allowed: [] },
  '/dashboards/reception': { data: receptionDashboardFixture, allowed: [] },
  '/dashboards/billing': { data: billingDashboardFixture, allowed: [] },
}

/** A failure in the envelope the API sends, inside the blob an export request asked for. */
const blobFail = (status: number, body: Record<string, unknown>): Outcome => ({
  status,
  data: new Blob([JSON.stringify({ success: false, ...body })], { type: 'application/json' }),
})

const unknownParam = (key: string) => {
  const message = `Unknown query parameter: \`${key}\`.`
  return { message, error_code: 'VALIDATION_ERROR', errors: { errors: [{ field: key, message }] } }
}

let api: FakeApi
/** Set by a test to answer the next matching request differently. */
let override: ((config: InternalAxiosRequestConfig) => Outcome | undefined) | undefined
/** Whether the server's `Content-Disposition` reaches the client (it does not cross-origin). */
let exposeFilename: boolean

function server(config: InternalAxiosRequestConfig): Outcome {
  const overridden = override?.(config)
  if (overridden) return overridden
  const url = config.url ?? ''
  const params = (config.params ?? {}) as Record<string, unknown>

  const exported = /^\/reports\/([^/]+)\/export$/.exec(url)
  if (config.method === 'get' && exported) {
    const route = ROUTES[`/reports/${exported[1]}`]
    if (!route) {
      return blobFail(404, { message: `Unknown report: \`${exported[1]}\`.`, error_code: 'RESOURCE_NOT_FOUND' })
    }
    const stray = Object.keys(params).find((k) => k !== 'format' && !route.allowed.includes(k))
    if (stray) return blobFail(422, unknownParam(stray))
    if (params.format !== 'csv') {
      const message = 'PDF export is not available yet. Use format=csv.'
      return blobFail(422, {
        message,
        error_code: 'VALIDATION_ERROR',
        errors: { errors: [{ field: 'format', message }] },
      })
    }
    return {
      status: 200,
      data: new Blob([CSV], { type: 'text/csv' }),
      headers: exposeFilename
        ? {
            'content-disposition':
              'attachment; filename="revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv"',
          }
        : {},
    }
  }

  const route = ROUTES[url]
  if (config.method === 'get' && route) {
    const stray = Object.keys(params).find((k) => !route.allowed.includes(k))
    if (stray) return fail(422, unknownParam(stray).message, unknownParam(stray))
    return ok(route.data)
  }
  if (config.method === 'post') return ok({ id: 'written' })
  return fail(404, 'Not found.', { error_code: 'RESOURCE_NOT_FOUND' })
}

let client: QueryClient
const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client }, children)

/** The single request of a test, as it would leave the browser. */
function only(urlPart: string) {
  const sent = api.requests('get', urlPart)
  expect(sent).toHaveLength(1)
  return sent[0]
}

beforeEach(() => {
  override = undefined
  exposeFilename = true
  api = installFakeApi(server)
  // No retry by default, as in the other suites; `useDoctorDashboard` is
  // checked against a client that would retry.
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  api.restore()
  client.clear()
})

describe('report hooks', () => {
  it('usePatientsReport asks for the report with no parameters when none are set', async () => {
    const { result } = renderHook(() => usePatientsReport(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    const sent = only('/reports/patients')
    expect(sent.method).toBe('get')
    expect(sent.url).toBe('/reports/patients')
    expect(sent.params).toEqual({})
    expect(result.current.data).toEqual(patientsReportFixture)
    expect(api.sent).toHaveLength(1)
  })

  it('usePatientsReport sends only the parameters that have a value', async () => {
    const { result } = renderHook(
      () => usePatientsReport({ from: '2026-10-04', to: undefined, granularity: 'week' }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    const sent = only('/reports/patients')
    expect(sent.params).toEqual({ from: '2026-10-04', granularity: 'week' })
    expect(Object.keys(sent.params as object)).not.toContain('to')
  })

  it('usePatientsReport never sends a parameter that report does not take', async () => {
    // A page that shares one filter object between reports must not get a 422
    // for a doctor filter the patients report has no use for.
    const shared = { from: '2026-10-04', to: '2026-10-06', doctor_id: DOCTOR_ID }
    const { result } = renderHook(() => usePatientsReport(shared), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(only('/reports/patients').params).toEqual({ from: '2026-10-04', to: '2026-10-06' })
  })

  it('useAppointmentsReport sends the period and both filters', async () => {
    const { result } = renderHook(
      () =>
        useAppointmentsReport({
          from: '2026-10-05',
          to: '2026-10-08',
          granularity: 'day',
          doctor_id: DOCTOR_ID,
          department_id: DEPARTMENT_ID,
        }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    const sent = only('/reports/appointments')
    expect(sent.method).toBe('get')
    expect(sent.url).toBe('/reports/appointments')
    expect(sent.params).toEqual({
      from: '2026-10-05',
      to: '2026-10-08',
      granularity: 'day',
      doctor_id: DOCTOR_ID,
      department_id: DEPARTMENT_ID,
    })
    expect(result.current.data?.summary.no_show_rate_percent).toBe('25.0')
    expect(result.current.data?.by_department[3].department_name).toBeNull()
  })

  it('useAppointmentsReport leaves out an empty filter', async () => {
    const { result } = renderHook(() => useAppointmentsReport({ doctor_id: '', department_id: undefined }), {
      wrapper,
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(only('/reports/appointments').params).toEqual({})
  })

  it('useRevenueReport returns money as the strings the server sent', async () => {
    const { result } = renderHook(() => useRevenueReport({ from: '2026-10-05', to: '2026-10-06' }), {
      wrapper,
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    const sent = only('/reports/revenue')
    expect(sent.method).toBe('get')
    expect(sent.url).toBe('/reports/revenue')
    expect(sent.params).toEqual({ from: '2026-10-05', to: '2026-10-06' })
    expect(result.current.data?.summary.net_collected_amount).toBe('1850.00')
    expect(result.current.data?.by_method.map((m) => m.method)).toEqual([
      'cash',
      'card',
      'upi',
      'bank_transfer',
      'insurance',
    ])
  })

  it('useOutstandingReport sends no query at all', async () => {
    const { result } = renderHook(() => useOutstandingReport(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    const sent = only('/reports/outstanding')
    expect(sent.method).toBe('get')
    expect(sent.url).toBe('/reports/outstanding')
    expect(sent.params).toBeUndefined()
    expect(result.current.data?.invoices).toHaveLength(2)
    expect(result.current.data?.summary.outstanding_amount).toBe('1550.00')
  })

  it('a changed period is a new request; the same period is not', async () => {
    const { result, rerender } = renderHook(
      ({ granularity }: { granularity: 'day' | 'week' }) => useRevenueReport({ granularity }),
      { wrapper, initialProps: { granularity: 'day' } },
    )
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    rerender({ granularity: 'day' })
    expect(api.requests('get', '/reports/revenue')).toHaveLength(1)

    rerender({ granularity: 'week' })
    await waitFor(() => expect(api.requests('get', '/reports/revenue')).toHaveLength(2))
    expect(api.requests('get', '/reports/revenue')[1].params).toEqual({ granularity: 'week' })
    // The last answer stays on screen while the next one loads.
    expect(result.current.data).toEqual(revenueReportFixture)
  })

  it('surfaces a refusal as an ApiError with the server\'s status and message', async () => {
    override = () =>
      fail(403, 'Permission denied. Required: report.admin.read.', { error_code: 'PERMISSION_DENIED' })
    const { result } = renderHook(() => usePatientsReport(), { wrapper })
    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(result.current.error).toBeInstanceOf(ApiError)
    expect((result.current.error as ApiError).status).toBe(403)
    expect((result.current.error as ApiError).code).toBe('PERMISSION_DENIED')
  })
})

describe('dashboard hooks', () => {
  it.each([
    ['admin', useAdminDashboard, adminDashboardFixture],
    ['doctor', useDoctorDashboard, doctorDashboardFixture],
    ['reception', useReceptionDashboard, receptionDashboardFixture],
    ['billing', useBillingDashboard, billingDashboardFixture],
  ] as const)('the %s dashboard is one GET with no query', async (name, useDashboard, fixture) => {
    const { result } = renderHook(() => useDashboard(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.sent).toHaveLength(1)
    expect(api.sent[0].method).toBe('get')
    expect(api.sent[0].url).toBe(`/dashboards/${name}`)
    expect(api.sent[0].params).toBeUndefined()
    expect(result.current.data).toEqual(fixture)
  })

  it('the server\'s own sums arrive untouched', async () => {
    const admin = renderHook(() => useAdminDashboard(), { wrapper })
    const doctor = renderHook(() => useDoctorDashboard(), { wrapper })
    await waitFor(() => expect(admin.result.current.isSuccess && doctor.result.current.isSuccess).toBe(true))

    expect(admin.result.current.data?.appointments_today.in_clinic).toBe(4)
    expect(doctor.result.current.data?.schedule_today.to_see).toBe(1)
  })

  it('the doctor dashboard is not retried when the account has no doctor profile', async () => {
    client = new QueryClient({ defaultOptions: { queries: { retry: 2, retryDelay: 0 } } })
    override = () =>
      fail(404, 'No active doctor profile is linked to this account.', { error_code: 'RESOURCE_NOT_FOUND' })
    const { result } = renderHook(() => useDoctorDashboard(), { wrapper })
    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(api.requests('get', '/dashboards/doctor')).toHaveLength(1)
    expect(isMissingDoctorProfile(result.current.error)).toBe(true)
  })
})

describe('a hook that is not enabled sends nothing', () => {
  it.each([
    ['usePatientsReport', () => usePatientsReport({}, { enabled: false })],
    ['useAppointmentsReport', () => useAppointmentsReport({}, { enabled: false })],
    ['useRevenueReport', () => useRevenueReport({}, { enabled: false })],
    ['useOutstandingReport', () => useOutstandingReport({ enabled: false })],
    ['useAdminDashboard', () => useAdminDashboard({ enabled: false })],
    ['useDoctorDashboard', () => useDoctorDashboard({ enabled: false })],
    ['useReceptionDashboard', () => useReceptionDashboard({ enabled: false })],
    ['useBillingDashboard', () => useBillingDashboard({ enabled: false })],
  ] as const)('%s', async (_name, useHook) => {
    const { result } = renderHook(() => useHook(), { wrapper })
    // Give a request the chance to leave, had one been made.
    await act(async () => {
      await Promise.resolve()
    })
    expect(api.sent).toHaveLength(0)
    expect(result.current.fetchStatus).toBe('idle')
    expect(result.current.data).toBeUndefined()
  })
})

describe('query keys', () => {
  it('sit under one root per family, so a write can refetch them all', () => {
    expect(reportKeys.all).toEqual(['reports'])
    expect(reportKeys.outstanding()).toEqual(['reports', 'outstanding'])
    expect(dashboardKeys.all).toEqual(['dashboards'])
    expect(dashboardKeys.admin()).toEqual(['dashboards', 'admin'])
    expect(dashboardKeys.doctor()).toEqual(['dashboards', 'doctor'])
    expect(dashboardKeys.reception()).toEqual(['dashboards', 'reception'])
    expect(dashboardKeys.billing()).toEqual(['dashboards', 'billing'])
  })

  it('ignore parameters that would not be sent', () => {
    expect(reportKeys.revenue({ from: '2026-10-05', to: undefined })).toEqual(
      reportKeys.revenue({ from: '2026-10-05' }),
    )
    expect(reportKeys.patients()).toEqual(['reports', 'patients', {}])
    expect(reportKeys.appointments({ doctor_id: DOCTOR_ID })).toEqual([
      'reports',
      'appointments',
      { doctor_id: DOCTOR_ID },
    ])
  })
})

describe('writes that move a figure refetch the dashboards and reports on screen', () => {
  /** Mount one dashboard and one report, then wait for both to load. */
  async function mounted() {
    const dashboard = renderHook(() => useAdminDashboard(), { wrapper })
    const report = renderHook(() => useRevenueReport(), { wrapper })
    await waitFor(() =>
      expect(dashboard.result.current.isSuccess && report.result.current.isSuccess).toBe(true),
    )
    expect(api.requests('get', '/dashboards/admin')).toHaveLength(1)
    expect(api.requests('get', '/reports/revenue')).toHaveLength(1)
  }

  async function refetched() {
    await waitFor(() => {
      expect(api.requests('get', '/dashboards/admin')).toHaveLength(2)
      expect(api.requests('get', '/reports/revenue')).toHaveLength(2)
    })
  }

  it('a check-in', async () => {
    await mounted()
    const { result } = renderHook(() => useAppointmentTransition(), { wrapper })
    await act(() => result.current.mutateAsync({ id: 'a1', action: 'check-in' }))

    expect(api.requests('post', '/appointments/a1/check-in')).toHaveLength(1)
    await refetched()
  })

  it('an issued invoice', async () => {
    await mounted()
    const { result } = renderHook(() => useIssueInvoice('i1'), { wrapper })
    await act(() => result.current.mutateAsync())

    expect(api.requests('post', '/invoices/i1/issue')).toHaveLength(1)
    await refetched()
  })

  it('a registered patient', async () => {
    await mounted()
    const { result } = renderHook(() => useCreatePatient(), { wrapper })
    await act(() => result.current.mutateAsync({ first_name: 'Ananya', last_name: 'Rao' } as CreatePatientInput))

    expect(api.requests('post', '/patients')).toHaveLength(1)
    await refetched()
  })

  it('a write that fails refetches nothing', async () => {
    await mounted()
    override = (config) =>
      config.method === 'post' ? fail(500, 'Internal Server Error', { error_code: 'INTERNAL_ERROR' }) : undefined
    const { result } = renderHook(() => useAppointmentTransition(), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ id: 'a1', action: 'check-in' }).catch(() => undefined)
    })

    expect(api.requests('get', '/dashboards/admin')).toHaveLength(1)
    expect(api.requests('get', '/reports/revenue')).toHaveLength(1)
  })
})

describe('fetchReportExport', () => {
  it('asks for the CSV of the report with the page\'s filters', async () => {
    const file = await fetchReportExport('revenue', { from: '2026-10-05', to: '2026-10-06', granularity: 'day' })

    expect(api.sent).toHaveLength(1)
    const sent = api.sent[0]
    expect(sent.method).toBe('get')
    expect(sent.url).toBe('/reports/revenue/export')
    expect(sent.params).toEqual({ format: 'csv', from: '2026-10-05', to: '2026-10-06', granularity: 'day' })
    expect(sent.responseType).toBe('blob')
    // The file is handed on byte for byte, byte-order mark included (text()
    // would decode it away, so the bytes are compared).
    expect(file.blob).toBeInstanceOf(Blob)
    expect(file.blob.type).toBe('text/csv')
    const bytes = new Uint8Array(await file.blob.arrayBuffer())
    expect(Array.from(bytes.slice(0, 3))).toEqual([0xef, 0xbb, 0xbf])
    expect(Array.from(bytes)).toEqual(Array.from(new TextEncoder().encode(CSV)))
  })

  it('uses the filename the server names', async () => {
    const file = await fetchReportExport('revenue', { from: '2026-10-05', to: '2026-10-06' })
    expect(file.filename).toBe('revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv')
  })

  it('builds a name of the same shape when the header cannot be read', async () => {
    exposeFilename = false
    const file = await fetchReportExport(
      'revenue',
      { granularity: 'week' },
      { from: '2026-10-05', to: '2026-10-06' },
    )
    expect(file.filename).toMatch(/^revenue-report-2026-10-05_to_2026-10-06-\d{8}-\d{6}\.csv$/)
    // The period only names the file; it is not sent as a filter.
    expect(api.sent[0].params).toEqual({ format: 'csv', granularity: 'week' })
  })

  it('sends only format for a filterless export, and only the filters a report takes', async () => {
    await fetchReportExport('outstanding', { from: '2026-10-05', to: '2026-10-06', doctor_id: DOCTOR_ID })
    await fetchReportExport('appointments', { doctor_id: DOCTOR_ID, department_id: undefined })
    await fetchReportExport('patients', { from: '2026-10-04', doctor_id: DOCTOR_ID })

    expect(api.sent.map((c) => [c.url, c.params])).toEqual([
      ['/reports/outstanding/export', { format: 'csv' }],
      ['/reports/appointments/export', { format: 'csv', doctor_id: DOCTOR_ID }],
      ['/reports/patients/export', { format: 'csv', from: '2026-10-04' }],
    ])
  })

  it('reads the server\'s refusal out of the blob it arrives in', async () => {
    override = () =>
      blobFail(403, { message: 'Permission denied. Required: report.export.', error_code: 'PERMISSION_DENIED' })

    const err = await fetchReportExport('revenue').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toBe('Permission denied. Required: report.export.')
    expect((err as ApiError).status).toBe(403)
    expect((err as ApiError).code).toBe('PERMISSION_DENIED')
  })

  it('keeps the field errors of a 422, so the message for a bad filter can be shown', async () => {
    override = () =>
      blobFail(422, {
        message: 'Validation failed.',
        error_code: 'VALIDATION_ERROR',
        errors: [{ field: 'query.from', message: 'Input should be a valid date' }],
      })

    const err = await fetchReportExport('revenue', { from: '2026-10-05' }).catch((e: unknown) => e)
    expect((err as ApiError).status).toBe(422)
    expect(apiErrorMessage(err)).toBe('Input should be a valid date')
  })

  it('falls back to the transport error when the body is not the API\'s envelope', async () => {
    override = () => ({ status: 502, data: new Blob(['<html>Bad Gateway</html>'], { type: 'text/html' }) })

    const err = await fetchReportExport('revenue').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(502)
    expect((err as ApiError).code).toBe('network_error')
    // Nothing a user should read: the screen shows its own sentence.
    expect(apiErrorMessage(err, "Couldn't export the report. Try again.")).toBe(
      "Couldn't export the report. Try again.",
    )
  })

  it('useExportReport returns the file for the caller to save', async () => {
    const { result } = renderHook(() => useExportReport(), { wrapper })
    let file: Awaited<ReturnType<typeof fetchReportExport>> | undefined
    await act(async () => {
      file = await result.current.mutateAsync({
        reportId: 'outstanding',
        period: { as_of_date: '2026-10-06' },
      })
    })

    expect(api.sent).toHaveLength(1)
    expect(api.sent[0].url).toBe('/reports/outstanding/export')
    expect(api.sent[0].params).toEqual({ format: 'csv' })
    expect(file?.filename).toBe('revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv')
  })
})

describe('reportExportFilename', () => {
  const now = new Date('2026-10-06T08:32:11.482Z')

  it('follows the server\'s pattern for a period report', () => {
    expect(reportExportFilename('revenue', { from: '2026-10-05', to: '2026-10-06' }, now)).toBe(
      'revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv',
    )
    expect(reportExportFilename('patients', { from: '2026-01-01', to: '2026-12-31' }, now)).toBe(
      'patients-report-2026-01-01_to_2026-12-31-20261006-083211.csv',
    )
  })

  it('follows the server\'s pattern for the outstanding report', () => {
    expect(reportExportFilename('outstanding', { as_of_date: '2026-10-06' }, now)).toBe(
      'outstanding-report-as-of-2026-10-06-20261006-083211.csv',
    )
  })

  it('stamps in UTC and pads every part', () => {
    expect(reportExportFilename('appointments', {}, new Date('2026-01-02T03:04:05Z'))).toBe(
      'appointments-report-20260102-030405.csv',
    )
  })

  it('names the file by the stamp alone when the period is not known', () => {
    expect(reportExportFilename('revenue', { from: '2026-10-05' }, now)).toBe(
      'revenue-report-20261006-083211.csv',
    )
    expect(reportExportFilename('outstanding', undefined, now)).toBe('outstanding-report-20261006-083211.csv')
  })
})

describe('apiErrorMessage', () => {
  it('shows the message the API wrote for a user', () => {
    expect(
      apiErrorMessage(new ApiError('Date range must not exceed 12 months.', 'VALIDATION_ERROR', 422, {
        errors: [{ field: 'to', message: 'Date range must not exceed 12 months.' }],
      })),
    ).toBe('Date range must not exceed 12 months.')
    expect(
      apiErrorMessage(
        new ApiError(
          'This account is not scoped to a hospital, so reports cannot be read.',
          'BUSINESS_RULE_VIOLATION',
          400,
        ),
      ),
    ).toBe('This account is not scoped to a hospital, so reports cannot be read.')
    expect(
      apiErrorMessage(new ApiError('PDF export is not available yet. Use format=csv.', 'VALIDATION_ERROR', 422)),
    ).toBe('PDF export is not available yet. Use format=csv.')
  })

  it('shows the first field error when the message is the generic one', () => {
    const err = new ApiError('Validation failed.', 'VALIDATION_ERROR', 422, [
      { field: 'query.from', message: 'Input should be a valid date' },
      { field: 'query.to', message: 'Input should be a valid date or datetime' },
    ])
    expect(apiErrorMessage(err)).toBe('Input should be a valid date')
  })

  it('falls back when the generic message carries no field error', () => {
    expect(apiErrorMessage(new ApiError('Validation failed.', 'VALIDATION_ERROR', 422))).toBe(
      REPORT_ERROR_FALLBACK,
    )
  })

  it('never shows a server fault, a transport error or a stray value', () => {
    expect(apiErrorMessage(new ApiError('Internal Server Error', 'INTERNAL_ERROR', 500))).toBe(
      REPORT_ERROR_FALLBACK,
    )
    expect(apiErrorMessage(new ApiError('Network Error', 'network_error'))).toBe(REPORT_ERROR_FALLBACK)
    expect(apiErrorMessage(new Error('boom'))).toBe(REPORT_ERROR_FALLBACK)
    expect(apiErrorMessage(undefined, "Couldn't load the revenue report.")).toBe(
      "Couldn't load the revenue report.",
    )
  })
})

describe('isMissingDoctorProfile', () => {
  it('is true only for the 404', () => {
    expect(isMissingDoctorProfile(new ApiError('No active doctor profile is linked to this account.', 'RESOURCE_NOT_FOUND', 404))).toBe(true)
    expect(isMissingDoctorProfile(new ApiError('Permission denied.', 'PERMISSION_DENIED', 403))).toBe(false)
    expect(isMissingDoctorProfile(new ApiError('Network Error', 'network_error'))).toBe(false)
    expect(isMissingDoctorProfile(new Error('boom'))).toBe(false)
  })
})
