import { describe, it, expect } from 'vitest'
import { toAppointmentQuery } from './appointments'

/**
 * The API reads `date`, `status` and `type` (docs/18-API_CONTRACTS.md §5.3)
 * and ignores query parameters it does not know. So a wrong name is not an
 * error — it is an unfiltered list that looks like it worked.
 */
describe('toAppointmentQuery', () => {
  it('sends the filter names the API actually reads', () => {
    const query = toAppointmentQuery({
      appointment_date: '2030-01-07',
      appointment_status: 'booked',
      appointment_type: 'walk_in',
    })

    expect(query).toMatchObject({ date: '2030-01-07', status: 'booked', type: 'walk_in' })
    expect(query).not.toHaveProperty('appointment_date')
    expect(query).not.toHaveProperty('appointment_status')
    expect(query).not.toHaveProperty('appointment_type')
  })

  it('passes the other parameters through unchanged', () => {
    const query = toAppointmentQuery({ patient_id: 'p1', doctor_id: 'd1', page: 2, page_size: 50 })

    expect(query).toMatchObject({ patient_id: 'p1', doctor_id: 'd1', page: 2, page_size: 50 })
  })

  it('sends no timezone offset — the server uses the hospital timezone', () => {
    // PR #29 review finding 7: a whole-hour offset rounded India's +5:30 to +6.
    expect(toAppointmentQuery({ appointment_date: '2030-01-07' })).not.toHaveProperty(
      'tz_offset_hours',
    )
  })
})
