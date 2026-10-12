import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ChartCard } from './ChartCard'
import { AreaChart, BarChart, LineChart } from './index'

const SUMMARY = 'Billed ₹4,350.00, collected ₹2,800.00 and refunded ₹950.00 between 5 Oct and 6 Oct.'

function card(overrides: Partial<Parameters<typeof ChartCard>[0]> = {}) {
  const props = {
    title: 'Revenue by day',
    isLoading: false,
    isError: false,
    onRetry: vi.fn(),
    isEmpty: false,
    emptyTitle: 'No invoices, payments or refunds in this period.',
    summary: SUMMARY,
    ...overrides,
  }
  const view = render(
    <ChartCard {...props}>
      <div data-testid="chart">drawing</div>
    </ChartCard>,
  )
  return { ...view, props }
}

describe('ChartCard', () => {
  it('is a region named by its title', () => {
    card({ description: 'Billed by issue date.' })

    const region = screen.getByRole('region', { name: 'Revenue by day' })
    expect(within(region).getByRole('heading', { level: 2, name: 'Revenue by day' })).toBeInTheDocument()
    expect(within(region).getByText('Billed by issue date.')).toBeInTheDocument()
  })

  it('shows a skeleton and no chart while loading', () => {
    const { container } = card({ isLoading: true })

    expect(screen.getByRole('status', { name: 'Loading Revenue by day' })).toBeInTheDocument()
    expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument()
    expect(screen.queryByTestId('chart')).not.toBeInTheDocument()
    expect(screen.queryByText(SUMMARY)).not.toBeInTheDocument()
    // The heading stays, so the page does not jump when the data arrives.
    expect(screen.getByRole('heading', { name: 'Revenue by day' })).toBeInTheDocument()
  })

  it('shows an error with Retry, and neither the chart nor its summary', async () => {
    const { props } = card({ isError: true })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load this chart")
    expect(screen.queryByTestId('chart')).not.toBeInTheDocument()
    expect(screen.queryByText(SUMMARY)).not.toBeInTheDocument()

    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(props.onRetry).toHaveBeenCalledTimes(1)
  })

  it('loading wins over a stale error, and an error over empty', () => {
    const { unmount } = card({ isLoading: true, isError: true, isEmpty: true })
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    unmount()

    card({ isError: true, isEmpty: true })
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('No invoices, payments or refunds in this period.')).not.toBeInTheDocument()
  })

  it('shows the empty state instead of an empty chart', () => {
    card({ isEmpty: true, emptyDescription: 'Try a longer period.' })

    expect(screen.getByText('No invoices, payments or refunds in this period.')).toBeInTheDocument()
    expect(screen.getByText('Try a longer period.')).toBeInTheDocument()
    expect(screen.queryByTestId('chart')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('draws the chart with the summary as its text alternative', () => {
    card()

    const figure = screen.getByRole('figure')
    // The sentence is in the page for a screen reader; the drawing is not.
    expect(within(figure).getByText(SUMMARY)).toBeInTheDocument()
    expect(screen.getByTestId('chart').closest('[aria-hidden="true"]')).not.toBeNull()
    expect(within(figure).getByText(SUMMARY).closest('[aria-hidden="true"]')).toBeNull()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders actions beside the title in every state', () => {
    card({ isLoading: true, actions: <button type="button">Daily</button> })

    expect(screen.getByRole('button', { name: 'Daily' })).toBeInTheDocument()
  })
})

describe('chart wrappers', () => {
  // jsdom gives the responsive container no size, so nothing is drawn; these
  // only prove the wrappers accept the report props and mount without error.
  const data = [
    { period: '2026-10-05', billed: 2950, collected: 1400 },
    { period: '2026-10-06', billed: 1400, collected: 1400 },
  ]
  const series = [
    { key: 'billed', label: 'Billed' },
    { key: 'collected', label: 'Collected' },
  ]
  const formatters = {
    valueFormatter: (n: number) => `₹${n}`,
    xTickFormatter: (v: string) => v.slice(5),
    yAxisWidth: 72,
  }

  it('mount with the existing props alone', () => {
    expect(() => {
      render(<BarChart data={data} xKey="period" series={series} />)
      render(<LineChart data={data} xKey="period" series={series} />)
      render(<AreaChart data={data} xKey="period" series={series} />)
    }).not.toThrow()
  })

  it('mount with formatters, a wider axis and stacking', () => {
    expect(() => {
      render(<BarChart data={data} xKey="period" series={series} stacked {...formatters} />)
      render(<LineChart data={data} xKey="period" series={series} {...formatters} />)
      render(<AreaChart data={data} xKey="period" series={series} {...formatters} />)
    }).not.toThrow()
  })
})
