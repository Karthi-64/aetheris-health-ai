import type {
  AdminDashboard,
  AppointmentsReport,
  BillingDashboard,
  DoctorDashboard,
  OutstandingReport,
  PatientsReport,
  ReceptionDashboard,
  RevenueReport,
} from '@/api/reports'

/**
 * The example payloads of the Reports and Dashboards API, exactly as the
 * contract gives them (the `data` of each success envelope): one hospital in
 * Asia/Kolkata on 6 Oct 2026, internally consistent across the eight routes.
 * Tests answer the fake API with these so every screen is checked against the
 * shapes and strings the server really sends.
 *
 * They are frozen in type only — a test that needs a variation copies one
 * (`{ ...revenueReportFixture, summary: { ... } }`) rather than editing it.
 */

/** `Patients report generated.` */
export const patientsReportFixture: PatientsReport = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  filters: {
    from: '2026-10-04',
    to: '2026-10-06',
    granularity: 'day'
  },
  summary: {
    registered: 11,
    active_total: 11,
    by_gender: {
      male: 6,
      female: 5,
      other: 0,
      unspecified: 0
    }
  },
  buckets: [
    {
      bucket_start: '2026-10-04',
      bucket_end: '2026-10-04',
      partial: false,
      registered: 0
    },
    {
      bucket_start: '2026-10-05',
      bucket_end: '2026-10-05',
      partial: false,
      registered: 0
    },
    {
      bucket_start: '2026-10-06',
      bucket_end: '2026-10-06',
      partial: false,
      registered: 11
    }
  ]
}

/** `Appointments report generated.` */
export const appointmentsReportFixture: AppointmentsReport = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  filters: {
    from: '2026-10-05',
    to: '2026-10-08',
    granularity: 'day',
    doctor: null,
    department: null
  },
  summary: {
    total: 14,
    booked: 5,
    checked_in: 3,
    in_progress: 1,
    completed: 3,
    cancelled: 1,
    no_show: 1,
    no_show_rate_percent: '25.0'
  },
  buckets: [
    {
      bucket_start: '2026-10-05',
      bucket_end: '2026-10-05',
      partial: false,
      total: 5,
      booked: 0,
      checked_in: 0,
      in_progress: 0,
      completed: 3,
      cancelled: 1,
      no_show: 1
    },
    {
      bucket_start: '2026-10-06',
      bucket_end: '2026-10-06',
      partial: false,
      total: 5,
      booked: 1,
      checked_in: 3,
      in_progress: 1,
      completed: 0,
      cancelled: 0,
      no_show: 0
    },
    {
      bucket_start: '2026-10-07',
      bucket_end: '2026-10-07',
      partial: false,
      total: 2,
      booked: 2,
      checked_in: 0,
      in_progress: 0,
      completed: 0,
      cancelled: 0,
      no_show: 0
    },
    {
      bucket_start: '2026-10-08',
      bucket_end: '2026-10-08',
      partial: false,
      total: 2,
      booked: 2,
      checked_in: 0,
      in_progress: 0,
      completed: 0,
      cancelled: 0,
      no_show: 0
    }
  ],
  by_doctor: [
    {
      doctor_id: '5b0c1c7e-2f0a-4d0e-9c53-0a1d6f3b9a11',
      doctor_name: 'Priya Sharma',
      department_id: 'c2a4e0f1-7b55-4a0c-8f0e-3d2b1a9e6c01',
      department_name: 'Cardiology',
      total: 6,
      completed: 2,
      cancelled: 0,
      no_show: 0
    },
    {
      doctor_id: '8e1f4a22-91c3-4b6d-a7f2-5c0d9e8b7a12',
      doctor_name: 'Arjun Nair',
      department_id: 'd3b5f1a2-8c66-4b1d-9a1f-4e3c2b0f7d02',
      department_name: 'Orthopaedics',
      total: 4,
      completed: 0,
      cancelled: 0,
      no_show: 1
    },
    {
      doctor_id: 'a94d2b33-0e7f-4c8a-b1d5-6e2f0a9c8b13',
      doctor_name: 'Meera Krishnan',
      department_id: 'e4c6a2b3-9d77-4c2e-ab20-5f4d3c1a8e03',
      department_name: 'Paediatrics',
      total: 3,
      completed: 1,
      cancelled: 0,
      no_show: 0
    },
    {
      doctor_id: 'bb5e3c44-1f80-4d9b-c2e6-7f3a1b0d9c14',
      doctor_name: 'Vikram Desai',
      department_id: null,
      department_name: null,
      total: 1,
      completed: 0,
      cancelled: 1,
      no_show: 0
    }
  ],
  by_department: [
    {
      department_id: 'c2a4e0f1-7b55-4a0c-8f0e-3d2b1a9e6c01',
      department_name: 'Cardiology',
      total: 6,
      completed: 2,
      cancelled: 0,
      no_show: 0
    },
    {
      department_id: 'd3b5f1a2-8c66-4b1d-9a1f-4e3c2b0f7d02',
      department_name: 'Orthopaedics',
      total: 4,
      completed: 0,
      cancelled: 0,
      no_show: 1
    },
    {
      department_id: 'e4c6a2b3-9d77-4c2e-ab20-5f4d3c1a8e03',
      department_name: 'Paediatrics',
      total: 3,
      completed: 1,
      cancelled: 0,
      no_show: 0
    },
    {
      department_id: null,
      department_name: null,
      total: 1,
      completed: 0,
      cancelled: 1,
      no_show: 0
    }
  ]
}

/** `Revenue report generated.` */
export const revenueReportFixture: RevenueReport = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  filters: {
    from: '2026-10-05',
    to: '2026-10-06',
    granularity: 'day'
  },
  summary: {
    invoice_count: 5,
    invoiced_amount: '4350.00',
    payment_count: 5,
    collected_amount: '2800.00',
    refund_count: 2,
    refunded_amount: '950.00',
    net_collected_amount: '1850.00'
  },
  by_method: [
    {
      method: 'cash',
      payment_count: 1,
      collected_amount: '500.00',
      refund_count: 0,
      refunded_amount: '0.00',
      net_collected_amount: '500.00'
    },
    {
      method: 'card',
      payment_count: 2,
      collected_amount: '1100.00',
      refund_count: 1,
      refunded_amount: '350.00',
      net_collected_amount: '750.00'
    },
    {
      method: 'upi',
      payment_count: 2,
      collected_amount: '1200.00',
      refund_count: 1,
      refunded_amount: '600.00',
      net_collected_amount: '600.00'
    },
    {
      method: 'bank_transfer',
      payment_count: 0,
      collected_amount: '0.00',
      refund_count: 0,
      refunded_amount: '0.00',
      net_collected_amount: '0.00'
    },
    {
      method: 'insurance',
      payment_count: 0,
      collected_amount: '0.00',
      refund_count: 0,
      refunded_amount: '0.00',
      net_collected_amount: '0.00'
    }
  ],
  buckets: [
    {
      bucket_start: '2026-10-05',
      bucket_end: '2026-10-05',
      partial: false,
      invoice_count: 3,
      invoiced_amount: '2950.00',
      payment_count: 3,
      collected_amount: '1400.00',
      refund_count: 0,
      refunded_amount: '0.00',
      net_collected_amount: '1400.00'
    },
    {
      bucket_start: '2026-10-06',
      bucket_end: '2026-10-06',
      partial: false,
      invoice_count: 2,
      invoiced_amount: '1400.00',
      payment_count: 2,
      collected_amount: '1400.00',
      refund_count: 2,
      refunded_amount: '950.00',
      net_collected_amount: '450.00'
    }
  ]
}

/** `Outstanding report generated.` */
export const outstandingReportFixture: OutstandingReport = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  filters: {
    as_of_date: '2026-10-06'
  },
  summary: {
    invoice_count: 2,
    outstanding_amount: '1550.00',
    issued_count: 1,
    partially_paid_count: 1
  },
  ageing: [
    {
      bucket: '0_30',
      min_days: 0,
      max_days: 30,
      invoice_count: 2,
      outstanding_amount: '1550.00'
    },
    {
      bucket: '31_60',
      min_days: 31,
      max_days: 60,
      invoice_count: 0,
      outstanding_amount: '0.00'
    },
    {
      bucket: '61_90',
      min_days: 61,
      max_days: 90,
      invoice_count: 0,
      outstanding_amount: '0.00'
    },
    {
      bucket: 'over_90',
      min_days: 91,
      max_days: null,
      invoice_count: 0,
      outstanding_amount: '0.00'
    }
  ],
  invoices: [
    {
      invoice_id: '0f6a7b11-3c2d-4e5f-8a90-1b2c3d4e5f03',
      invoice_number: 'INV-2026-000003',
      issued_at: '2026-10-05T05:10:00Z',
      issued_date: '2026-10-05',
      age_days: 1,
      patient_id: '71c2d3e4-5f60-4a1b-9c8d-7e6f5a4b3c02',
      patient_name: 'Thomas George',
      patient_mrn: 'MRN-000002',
      status: 'partially_paid',
      total: '750.00',
      amount_paid: '300.00',
      balance_due: '450.00'
    },
    {
      invoice_id: '1a7b8c22-4d3e-4f60-9ba1-2c3d4e5f6a04',
      invoice_number: 'INV-2026-000004',
      issued_at: '2026-10-05T06:40:00Z',
      issued_date: '2026-10-05',
      age_days: 1,
      patient_id: '82d3e4f5-6071-4b2c-ad9e-8f706b5c4d03',
      patient_name: 'Ishaan Kulkarni',
      patient_mrn: 'MRN-000003',
      status: 'issued',
      total: '1100.00',
      amount_paid: '0.00',
      balance_due: '1100.00'
    }
  ],
  invoices_total: 2,
  invoices_truncated: false
}

/** `Admin dashboard loaded.` */
export const adminDashboardFixture: AdminDashboard = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  appointments_today: {
    date: '2026-10-06',
    total: 5,
    booked: 1,
    checked_in: 3,
    in_progress: 1,
    completed: 0,
    cancelled: 0,
    no_show: 0,
    in_clinic: 4
  },
  revenue_this_week: {
    from: '2026-10-05',
    to: '2026-10-06',
    invoice_count: 5,
    invoiced_amount: '4350.00',
    payment_count: 5,
    collected_amount: '2800.00',
    refund_count: 2,
    refunded_amount: '950.00',
    net_collected_amount: '1850.00'
  },
  patient_registrations_this_month: {
    from: '2026-10-01',
    to: '2026-10-06',
    registered: 11,
    active_total: 11
  }
}

/** `Doctor dashboard loaded.` */
export const doctorDashboardFixture: DoctorDashboard = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  doctor: {
    id: '5b0c1c7e-2f0a-4d0e-9c53-0a1d6f3b9a11',
    name: 'Priya Sharma'
  },
  schedule_today: {
    date: '2026-10-06',
    total: 2,
    booked: 0,
    checked_in: 1,
    in_progress: 1,
    completed: 0,
    cancelled: 0,
    no_show: 0,
    to_see: 1,
    appointments: [
      {
        appointment_id: '3c9d0e44-6f5a-4b71-8cd2-4e5f6a7b8c06',
        scheduled_start: '2026-10-06T08:00:00Z',
        scheduled_end: '2026-10-06T08:30:00Z',
        status: 'in_progress',
        type: 'new',
        patient_id: '93e4f506-7182-4c3d-bea0-90817c6d5e04',
        patient_name: 'Ananya Rao',
        patient_mrn: 'MRN-000006',
        checked_in_at: '2026-10-06T07:50:00Z'
      },
      {
        appointment_id: '4dae1f55-706b-4c82-9de3-5f607b8c9d07',
        scheduled_start: '2026-10-06T08:30:00Z',
        scheduled_end: '2026-10-06T09:00:00Z',
        status: 'checked_in',
        type: 'follow_up',
        patient_id: 'a4f50617-8293-4d4e-cfb1-a1928d7e6f05',
        patient_name: 'Harish Pillai',
        patient_mrn: 'MRN-000007',
        checked_in_at: '2026-10-06T08:20:00Z'
      }
    ]
  },
  my_patients: {
    count: 5
  },
  this_week: {
    from: '2026-10-05',
    to: '2026-10-11',
    total: 6,
    booked: 2,
    checked_in: 1,
    in_progress: 1,
    completed: 2,
    cancelled: 0,
    no_show: 0
  }
}

/** `Reception dashboard loaded.` */
export const receptionDashboardFixture: ReceptionDashboard = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  schedule_today: {
    date: '2026-10-06',
    total: 5,
    booked: 1,
    checked_in: 3,
    in_progress: 1,
    completed: 0,
    cancelled: 0,
    no_show: 0,
    in_clinic: 4
  },
  walk_in_queue: {
    waiting: 2,
    not_arrived: 0,
    in_consultation: 0,
    longest_wait_minutes: 42
  },
  no_show_alerts: {
    marked_today: 0,
    at_risk: 1,
    at_risk_appointments: [
      {
        appointment_id: '6fc03177-928d-4ea4-bf05-718293a4b509',
        scheduled_start: '2026-10-06T08:15:00Z',
        minutes_late: 17,
        patient_id: 'b5061728-93a4-4e5f-d0c2-b2a39e8f7006',
        patient_name: 'Sunita Verma',
        patient_mrn: 'MRN-000010',
        doctor_id: '8e1f4a22-91c3-4b6d-a7f2-5c0d9e8b7a12',
        doctor_name: 'Arjun Nair'
      }
    ]
  }
}

/** `Billing dashboard loaded.` */
export const billingDashboardFixture: BillingDashboard = {
  meta: {
    hospital_name: 'Demo Hospital',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    today: '2026-10-06',
    generated_at: '2026-10-06T08:32:11Z'
  },
  unpaid_invoices: {
    invoice_count: 2,
    outstanding_amount: '1550.00',
    issued_count: 1,
    partially_paid_count: 1
  },
  revenue: {
    today: {
      from: '2026-10-06',
      to: '2026-10-06',
      invoice_count: 2,
      invoiced_amount: '1400.00',
      payment_count: 2,
      collected_amount: '1400.00',
      refund_count: 2,
      refunded_amount: '950.00',
      net_collected_amount: '450.00'
    },
    this_week: {
      from: '2026-10-05',
      to: '2026-10-06',
      invoice_count: 5,
      invoiced_amount: '4350.00',
      payment_count: 5,
      collected_amount: '2800.00',
      refund_count: 2,
      refunded_amount: '950.00',
      net_collected_amount: '1850.00'
    },
    this_month: {
      from: '2026-10-01',
      to: '2026-10-06',
      invoice_count: 5,
      invoiced_amount: '4350.00',
      payment_count: 5,
      collected_amount: '2800.00',
      refund_count: 2,
      refunded_amount: '950.00',
      net_collected_amount: '1850.00'
    }
  },
  discounts_pending_approval: {
    invoice_count: 1,
    discount_amount: '300.00'
  }
}
