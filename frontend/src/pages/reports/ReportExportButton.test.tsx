import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import type { ReportExportParams, ReportExportPeriod, ReportId } from '@/api/reports'
import { signIn, signOut } from '@/test/auth'
import { installFakeApi, type FakeApi, type Outcome } from '@/test/fakeApi'
import { ReportExportButton } from './ReportExportButton'

/**
 * The CSV export of a report (`GET /reports/{id}/export`). The real mutation
 * and Axios instance run; the adapter is an in-memory server that answers
 * with a file, or — as the real one does for a request that asked for a blob —
 * with its JSON error envelope inside one.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({ toastSuccess: vi.fn(), toastError: vi.fn() }))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const CSV = '﻿Hospital,Demo Hospital\r\nReport,Revenue\r\n'
const SERVER_NAME = 'revenue-report-2026-10-05_to_2026-10-06-20261006-083211.csv'

const file = (filename?: string): Outcome => ({
  status: 200,
  data: new Blob([CSV], { type: 'text/csv' }),
  headers: filename ? { 'content-disposition': `attachment; filename="${filename}"` } : {},
})

/** A failure in the API's envelope, inside the blob the request asked for. */
const refused = (status: number, message: string, extra: Record<string, unknown> = {}): Outcome => ({
  status,
  data: new Blob([JSON.stringify({ success: false, message, ...extra })], { type: 'application/json' }),
})

let answer: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>
let fake: FakeApi
/** What the button handed to the browser: one entry per file saved. */
let downloads: { filename: string; blob: Blob }[]

const realCreate = URL.createObjectURL
const realRevoke = URL.revokeObjectURL

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  answer = () => file(SERVER_NAME)
  fake = installFakeApi((config) => answer(config))

  downloads = []
  const held = new Map<string, Blob>()
  URL.createObjectURL = vi.fn((blob: Blob) => {
    const url = `blob:test/${held.size}`
    held.set(url, blob)
    return url
  }) as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    const blob = held.get(this.href)
    if (blob) downloads.push({ filename: this.download, blob })
  })
})

afterEach(() => {
  fake.restore()
  signOut()
  vi.restoreAllMocks()
  URL.createObjectURL = realCreate
  URL.revokeObjectURL = realRevoke
})

const EXPORTER = ['report.admin.read', 'report.export']

function renderButton(
  permissions: string[],
  props: { reportId?: ReportId; params?: ReportExportParams; period?: ReportExportPeriod; disabled?: boolean } = {},
) {
  signIn(permissions)
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ReportExportButton reportId={props.reportId ?? 'revenue'} {...props} />
    </QueryClientProvider>,
  )
}

const button = () => screen.getByRole('button', { name: /Export CSV|Exporting/ })

describe('who is offered the export', () => {
  it('needs report.export', () => {
    renderButton(['report.admin.read'])
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('needs the report\'s own read code as well', () => {
    // Billing staff may export, but Patients is not theirs to read.
    renderButton(['report.billing.read', 'report.export'], { reportId: 'patients' })
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it.each<[ReportId, string]>([
    ['patients', 'report.admin.read'],
    ['appointments', 'report.admin.read'],
    ['revenue', 'report.admin.read'],
    ['revenue', 'report.billing.read'],
    ['outstanding', 'report.admin.read'],
    ['outstanding', 'report.billing.read'],
  ])('offers the %s export to a holder of %s and report.export', (reportId, code) => {
    renderButton([code, 'report.export'], { reportId })
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeEnabled()
    expect(fake.sent).toHaveLength(0)
  })

  it('is not usable while the filters on screen are ones the server would refuse', () => {
    renderButton(EXPORTER, { disabled: true })
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeDisabled()
  })
})

describe('exporting', () => {
  it('asks for the CSV with the filters given, saves the file and says so', async () => {
    const user = userEvent.setup()
    renderButton(EXPORTER, {
      params: { from: '2026-10-05', to: '2026-10-06', granularity: 'day' },
      period: { from: '2026-10-05', to: '2026-10-06' },
    })

    await user.click(button())

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`Downloaded ${SERVER_NAME}`))
    expect(fake.sent).toHaveLength(1)
    const sent = fake.sent[0]
    expect(sent.method).toBe('get')
    expect(sent.url).toBe('/reports/revenue/export')
    expect(sent.params).toEqual({ format: 'csv', from: '2026-10-05', to: '2026-10-06', granularity: 'day' })
    expect(sent.responseType).toBe('blob')

    expect(downloads).toHaveLength(1)
    expect(downloads[0].filename).toBe(SERVER_NAME)
    // Handed on byte for byte, byte-order mark included.
    const bytes = new Uint8Array(await downloads[0].blob.arrayBuffer())
    expect(Array.from(bytes)).toEqual(Array.from(new TextEncoder().encode(CSV)))
    expect(toastError).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeEnabled()
  })

  it('sends the appointment filters, and only format for the filterless outstanding report', async () => {
    const user = userEvent.setup()
    const doctor = '5b0c1c7e-2f0a-4d0e-9c53-0a1d6f3b9a11'
    renderButton(EXPORTER, { reportId: 'appointments', params: { granularity: 'week', doctor_id: doctor } })

    await user.click(button())
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled())
    expect(fake.sent[0].url).toBe('/reports/appointments/export')
    expect(fake.sent[0].params).toEqual({ format: 'csv', granularity: 'week', doctor_id: doctor })
  })

  it('names the file in the server\'s pattern when its header cannot be read', async () => {
    const user = userEvent.setup()
    answer = () => file()
    renderButton(EXPORTER, { reportId: 'outstanding', period: { as_of_date: '2026-10-06' } })

    await user.click(button())

    await waitFor(() => expect(downloads).toHaveLength(1))
    expect(fake.sent[0].url).toBe('/reports/outstanding/export')
    // The period names the file; it is not a filter and is not sent.
    expect(fake.sent[0].params).toEqual({ format: 'csv' })
    expect(downloads[0].filename).toMatch(/^outstanding-report-as-of-2026-10-06-\d{8}-\d{6}\.csv$/)
    expect(toastSuccess).toHaveBeenCalledWith(`Downloaded ${downloads[0].filename}`)
  })

  it('sends one request for a double click', async () => {
    const user = userEvent.setup()
    let release: (outcome: Outcome) => void = () => {}
    answer = () =>
      new Promise<Outcome>((resolve) => {
        release = resolve
      })
    renderButton(EXPORTER)

    await user.dblClick(button())
    await user.click(button())

    expect(fake.sent).toHaveLength(1)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Exporting…' })).toBeDisabled())
    expect(screen.getByRole('button', { name: 'Exporting…' })).toHaveAttribute('aria-busy', 'true')

    release(file(SERVER_NAME))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(downloads).toHaveLength(1)
    expect(fake.sent).toHaveLength(1)

    // Once it has finished, it can be used again.
    answer = () => file(SERVER_NAME)
    await user.click(await screen.findByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(fake.sent).toHaveLength(2))
  })
})

describe('a failed export', () => {
  it('says the user may not export on a 403, not the server\'s permission code', async () => {
    const user = userEvent.setup()
    answer = () =>
      refused(403, 'Permission denied. Required: report.export.', { error_code: 'PERMISSION_DENIED' })
    renderButton(EXPORTER)

    await user.click(button())

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("You don't have permission to export this report."),
    )
    expect(downloads).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('passes on the server\'s words for a format it refuses', async () => {
    const user = userEvent.setup()
    const message = 'PDF export is not available yet. Use format=csv.'
    answer = () =>
      refused(422, message, {
        error_code: 'VALIDATION_ERROR',
        errors: { errors: [{ field: 'format', message }] },
      })
    renderButton(EXPORTER)

    await user.click(button())

    await waitFor(() => expect(toastError).toHaveBeenCalledWith(message))
    expect(downloads).toHaveLength(0)
  })

  it('passes on the server\'s words for a period it refuses', async () => {
    const user = userEvent.setup()
    answer = () => refused(422, 'Date range must not exceed 12 months.', { error_code: 'VALIDATION_ERROR' })
    renderButton(EXPORTER, { params: { from: '2025-01-01', to: '2026-10-06' } })

    await user.click(button())

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Date range must not exceed 12 months.'))
  })

  it('shows the field message when the server only says "Validation failed."', async () => {
    const user = userEvent.setup()
    answer = () =>
      refused(422, 'Validation failed.', {
        error_code: 'VALIDATION_ERROR',
        errors: [{ field: 'query.from', message: 'Input should be a valid date' }],
      })
    renderButton(EXPORTER)

    await user.click(button())

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Input should be a valid date'))
  })

  it('passes on a 400 written for the user', async () => {
    const user = userEvent.setup()
    const message = 'This account is not scoped to a hospital, so reports cannot be read.'
    answer = () => refused(400, message, { error_code: 'BUSINESS_RULE_VIOLATION' })
    renderButton(EXPORTER)

    await user.click(button())

    await waitFor(() => expect(toastError).toHaveBeenCalledWith(message))
  })

  it('says to try again on a server fault, without its text', async () => {
    const user = userEvent.setup()
    answer = () => refused(500, 'sqlalchemy.exc.OperationalError', { error_code: 'INTERNAL_ERROR' })
    renderButton(EXPORTER)

    await user.click(button())

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Couldn't export the report. Try again."))
    expect(downloads).toHaveLength(0)
  })

  it('says to try again when the network drops, and can then be retried', async () => {
    const user = userEvent.setup()
    answer = (config) => {
      throw new AxiosError('Network Error', 'ERR_NETWORK', config)
    }
    renderButton(EXPORTER)

    await user.click(button())

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Couldn't export the report. Try again."))
    expect(toastSuccess).not.toHaveBeenCalled()

    answer = () => file(SERVER_NAME)
    await user.click(await screen.findByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`Downloaded ${SERVER_NAME}`))
    expect(fake.sent).toHaveLength(2)
  })
})
