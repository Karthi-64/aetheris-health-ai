import type { AppointmentStatus, CancelReasonCode } from '@/api/myAppointments'

/** User-facing strings of the patient's own appointments: the list, one appointment, and cancelling it. */
export const appointmentStrings = {
  listTitle: 'My appointments',
  listHeading: 'My appointments',
  listIntro: 'The appointments you have booked in this app.',
  viewsLabel: 'Appointments to show',
  upcomingTab: 'Upcoming',
  pastTab: 'Past',
  upcomingHeading: 'Upcoming appointments',
  pastHeading: 'Past appointments',

  loading: 'Loading appointments…',
  // The position in the results, exactly as the server reported it.
  count: (total: number) => (total === 1 ? '1 appointment' : `${total} appointments`),
  range: (from: number, to: number, total: number) => `Showing ${from}–${to} of ${total} appointments`,
  paginationLabel: 'Pages of appointments',
  pageOf: (page: number, pages: number) => `Page ${page} of ${pages}`,
  previous: 'Previous',
  next: 'Next',

  noUpcomingStatus: 'No upcoming appointments',
  noUpcomingTitle: 'No upcoming appointments',
  noUpcomingBody: 'When you book an appointment, it will be listed here.',
  findHospital: 'Find a hospital',
  noPastStatus: 'No past appointments',
  noPastTitle: 'No past appointments',
  noPastBody: 'Appointments that are over, cancelled or missed will be listed here.',
  pastEndStatus: 'Nothing on this page',
  pastEndTitle: 'There is nothing on this page',
  pastEndBody: (total: number) =>
    total === 1 ? 'There is 1 appointment in all, on the first page.' : `There are ${total} appointments in all, on earlier pages.`,
  firstPage: 'Go to the first page',

  listFailed: 'We could not load your appointments. Please try again.',
  appointmentFailed: 'We could not load this appointment. Please try again.',

  // On a card and on the appointment's page. The zone is the server's text, shown as it is.
  timeRange: (from: string, to: string) => `${from} – ${to}`,
  timeWithZone: (range: string, timezone: string) => `${range} (${timezone})`,
  viewAppointment: 'View appointment',
  cancelLink: 'Cancel',

  detailTitle: 'Appointment',
  detailHeading: 'Appointment',
  loadingAppointment: 'Loading appointment…',
  backToList: 'My appointments',
  detailsHeading: 'Appointment details',
  statusLabel: 'Status',
  doctorLabel: 'Doctor',
  hospitalLabel: 'Hospital',
  dayLabel: 'Day',
  timeLabel: 'Time',
  reasonLabel: 'Reason for visit',
  referenceLabel: 'Reference',
  timezoneNoteBefore: 'Times are in the hospital’s local time (',
  timezoneNoteAfter: ')',

  // One neutral message for "no such appointment", "not yours" and "not a reference at all".
  notFoundTitle: 'This appointment is not available',
  notFoundBody: 'It may not be one of your appointments, or the link you followed may be wrong.',
  goToList: 'Go to my appointments',

  // Cancelling. Offered only when the server says the appointment can be cancelled.
  cancelAppointment: 'Cancel appointment',
  cancelUntil: (time: string, day: string, timezone: string) => `You can cancel until ${time}, ${day} (${timezone})`,
  notCancellableInApp: 'This appointment can no longer be cancelled in the app. Please contact the hospital.',

  dialogTitle: 'Cancel this appointment?',
  dialogSummary: (doctor: string, day: string, time: string) => `${doctor}, ${day}, ${time}`,
  reasonLegend: 'Why are you cancelling?',
  reasonRequiredHint: 'Choose one to continue.',
  detailsFieldLabel: 'More details (optional)',
  detailsHint: 'Anything else the hospital should know.',
  detailsCount: (count: number, max: number) => `${count} of ${max} characters`,
  detailsTooLong: (max: number) => `Keep the details to ${max} characters or fewer.`,
  // Once a request has gone out, a retry must be the same request.
  detailsLocked: 'Your answers cannot be changed while this request is being tried again.',
  keepAppointment: 'Keep appointment',
  tryAgain: 'Try again',
  cancelling: 'Cancelling your appointment…',

  // Said only after the server has answered with the appointment, cancelled.
  cancelled: 'Appointment cancelled',
  cancelledBody: 'Your appointment has been cancelled.',

  // Refusals. Each is the app's own wording for one answer of the server; the server's text is never shown.
  noLongerCancellable: 'This appointment can no longer be cancelled.',
  tooLate: 'It is too late to cancel this appointment in the app. Please contact the hospital.',
  cancelOfflineTitle: 'No connection',
  cancelOfflineBody: 'We could not reach the server. Your appointment has not been confirmed as cancelled. Check your internet connection and try again.',
  cancelFailedTitle: 'We could not cancel the appointment',
  cancelFailedBody: 'Something went wrong on our side. Your appointment has not been confirmed as cancelled. Try again.',
  cancelUnconfirmedTitle: 'We could not confirm the cancellation',
  cancelUnconfirmedBody: 'Try again — it is safe to ask more than once.',
  cancelInvalidTitle: 'We could not send this request',
  cancelInvalidBody: 'Check your answers and try again.',
} as const

/** What each real state of an appointment is called. Nothing else is ever shown as a status. */
export const statusLabels: Record<AppointmentStatus, string> = {
  booked: 'Booked',
  checked_in: 'Checked in',
  in_progress: 'In progress',
  completed: 'Completed',
  cancelled: 'Cancelled',
  no_show: 'Missed',
}

/** The reasons offered for cancelling, in the order they are shown. */
export const cancelReasons: { code: CancelReasonCode; label: string }[] = [
  { code: 'schedule_conflict', label: 'I have a schedule conflict' },
  { code: 'feeling_better', label: 'I am feeling better' },
  { code: 'booked_by_mistake', label: 'I booked by mistake' },
  { code: 'other', label: 'Another reason' },
]
