import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { Users } from 'lucide-react'
import { KpiCard } from './kpi-card'
import { StatTile } from './stat-tile'

describe('StatTile', () => {
  it('shows a skeleton and no figure while loading', () => {
    const { container } = render(
      <StatTile label="Unpaid invoices" value={2} hint="₹1,550.00 outstanding" isLoading isError={false} />,
    )

    expect(screen.getByRole('status', { name: 'Loading Unpaid invoices' })).toBeInTheDocument()
    expect(container.querySelector('[data-slot="skeleton"]')).toBeInTheDocument()
    // Not even a value it was handed: a loading tile shows no number.
    expect(screen.queryByText('2')).not.toBeInTheDocument()
    expect(screen.queryByText('₹1,550.00 outstanding')).not.toBeInTheDocument()
    expect(screen.queryByText('Unpaid invoices')).not.toBeInTheDocument()
  })

  it('shows the label, the value as given and the hint', () => {
    render(
      <StatTile
        label="Billed this week"
        value="₹4,350.00"
        hint="Collected ₹2,800.00 · Refunded ₹950.00"
        icon={Users}
        isLoading={false}
        isError={false}
      />,
    )

    expect(screen.getByText('Billed this week')).toBeInTheDocument()
    expect(screen.getByText('₹4,350.00')).toBeInTheDocument()
    expect(screen.getByText('Collected ₹2,800.00 · Refunded ₹950.00')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows a zero as a figure, not as missing', () => {
    render(<StatTile label="Walk-ins waiting" value={0} isLoading={false} isError={false} />)

    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.queryByText('Not available')).not.toBeInTheDocument()
  })

  it('links the whole tile to its breakdown', () => {
    render(
      <MemoryRouter>
        <StatTile
          label="New patients this month"
          value={11}
          hint="11 active patients"
          to="/reports/patients"
          isLoading={false}
          isError={false}
        />
      </MemoryRouter>,
    )

    const link = screen.getByRole('link', { name: /New patients this month/ })
    expect(link).toHaveAttribute('href', '/reports/patients')
    expect(link).toHaveTextContent('11')
    expect(link).toHaveTextContent('11 active patients')
  })

  it('on error shows a dash and Retry, never the value or a link', async () => {
    const onRetry = vi.fn()
    render(
      <MemoryRouter>
        <StatTile
          label="Unpaid invoices"
          value={2}
          hint="₹1,550.00 outstanding"
          to="/reports/outstanding"
          isLoading={false}
          isError
          onRetry={onRetry}
        />
      </MemoryRouter>,
    )

    expect(screen.getByText('Unpaid invoices')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByText('Not available')).toBeInTheDocument()
    expect(screen.getByText("Couldn't load this figure.")).toBeInTheDocument()
    // A figure from before the failure is not passed off as current.
    expect(screen.queryByText('2')).not.toBeInTheDocument()
    expect(screen.queryByText('₹1,550.00 outstanding')).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Retry Unpaid invoices' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('on error without a retry handler offers no button', () => {
    render(<StatTile label="My patients" isLoading={false} isError />)

    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows a dash when the answer carried no value', () => {
    render(<StatTile label="My patients" isLoading={false} isError={false} />)

    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load this figure.")).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('KpiCard', () => {
  it('renders as before without a hint or a link, and needs no router', () => {
    render(<KpiCard label="Patients" value={12} />)

    expect(screen.getByText('Patients')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByText(/vs last period/)).not.toBeInTheDocument()
  })

  it('shows a hint under the value', () => {
    render(<KpiCard label="No-show" value={1} hint="25.0% of appointments that were due" />)

    expect(screen.getByText('25.0% of appointments that were due')).toBeInTheDocument()
  })

  it('becomes a link when given a destination', () => {
    render(
      <MemoryRouter>
        <KpiCard label="Billed" value="₹4,350.00" hint="5 invoices" to="/reports/revenue" />
      </MemoryRouter>,
    )

    expect(screen.getByRole('link', { name: /Billed/ })).toHaveAttribute('href', '/reports/revenue')
  })
})
