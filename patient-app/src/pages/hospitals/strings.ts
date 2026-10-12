import type { Gender } from '@/api/links'

/** User-facing strings of the link and self-registration screens. */
export const linkStrings = {
  title: 'Link a hospital',
  heading: 'Link your hospital record',
  intro: 'Enter the code your hospital gave you and your date of birth. We match them with the mobile number you signed in with.',
  hospitalCodeLabel: 'Hospital code',
  hospitalCodeHint: 'The code your hospital gave you.',
  hospitalCodeRequired: 'Enter the hospital code.',
  dobLabel: 'Date of birth',
  dobRequired: 'Enter your date of birth.',
  dobInvalid: 'Enter a real date.',
  dobFuture: 'Your date of birth cannot be in the future.',
  mrnLabel: 'Medical record number (MRN)',
  mrnHint: 'It is printed on your hospital card, bills and prescriptions.',
  mrnRequired: 'Enter your MRN.',
  mrnNeeded: 'We need your MRN to find your record. Please add it below.',
  // What the tick means, and nothing more: there is no terms-of-service or
  // privacy text yet, so neither tick may claim to accept one.
  linkConsent: 'I agree to connect my app account to my record at this hospital.',
  consentRequired: 'Please tick the box to continue.',
  submit: 'Find my record',
  submitting: 'Checking…',

  // One neutral message for "no record", "wrong date of birth", "wrong MRN" and "unknown hospital".
  notFound: 'We could not find a record with these details.',
  notFoundHelp: 'Check the hospital code and your date of birth and try again.',
  registerOffer: 'New to this hospital? You can register as a new patient instead.',
  registerCta: 'Register as a new patient',
  contactHospital: 'Please contact the hospital.',
  alreadyLinkedElsewhere: 'Your account is already linked to a record at this hospital.',
  invalid: 'Some of these details are not valid. Check them and try again.',
  // A consent version this build no longer matches; a reload fetches the current app.
  reload: 'Please reload and try again.',
  policiesPending: 'We cannot complete this in the app right now. Please try again later.',
  busy: 'Too many attempts. Please wait a few minutes and try again.',
  failed: 'Something went wrong. Check your connection and try again.',

  registerHeading: 'Register as a new patient',
  registerIntro: 'Check your details before we create your record. It will use the mobile number you signed in with.',
  confirmHospitalCode: 'Hospital code',
  confirmDob: 'Date of birth',
  changeDetails: 'These are not right — go back',
  firstNameLabel: 'First name',
  firstNameRequired: 'Enter your first name.',
  lastNameLabel: 'Last name',
  lastNameRequired: 'Enter your last name.',
  nameTooLong: 'This is too long.',
  genderLabel: 'Gender',
  genderRequired: 'Choose an option.',
  genderPlaceholder: 'Choose…',
  registerConsent:
    'I agree to give this hospital my name, date of birth, gender and mobile number to register me as a patient, and to connect my app account to the new record.',
  registerSubmit: 'Create my record',
  registerSubmitting: 'Creating…',
  registerConflict:
    'We could not register you because a record may already exist for you at this hospital. Go back and link it instead, or contact the hospital.',
  registerNotFound:
    'We could not register you with these details. Check the hospital code, or contact the hospital.',

  linkedTitle: 'Your record is linked',
  linkedBody: 'You can now see this hospital on your home screen.',
  alreadyLinkedTitle: 'This record is already linked',
  alreadyLinkedBody: 'Your account was already linked to this record. Nothing has changed.',
  registeredTitle: 'You are registered',
  registeredBody: 'Your new record has been created and linked to your account.',
  goHome: 'Go to home',
} as const

export const genderLabels: Record<Gender, string> = {
  female: 'Female',
  male: 'Male',
  other: 'Other',
  unspecified: 'Prefer not to say',
}

/** User-facing strings of hospital discovery: the list and one hospital's page. */
export const hospitalStrings = {
  listTitle: 'Find a hospital',
  listHeading: 'Find a hospital',
  listIntro: 'Search the hospitals that are available in this app.',
  searchFormLabel: 'Find a hospital',
  searchLabel: 'Search by hospital name',
  clearSearch: 'Clear search',
  cityLabel: 'City',
  allCities: 'All cities',

  resultsHeading: 'Hospitals',
  loading: 'Loading hospitals…',
  // The position in the results, exactly as the server reported it.
  count: (total: number) => (total === 1 ? '1 hospital' : `${total} hospitals`),
  range: (from: number, to: number, total: number) => `Showing ${from}–${to} of ${total} hospitals`,
  paginationLabel: 'Pages of hospitals',
  pageOf: (page: number, pages: number) => `Page ${page} of ${pages}`,
  previous: 'Previous',
  next: 'Next',

  // Two different kinds of nothing: none exist, or none match.
  noneStatus: 'No hospitals to show',
  noneTitle: 'No hospitals to show yet',
  noneBody: 'No hospital is available in the app right now. Please check again later.',
  noMatchStatus: 'No hospitals match',
  noMatchTitle: 'No hospitals match your search',
  noMatchBody: 'Check the spelling, or clear the search and the city to see every hospital.',
  clearFilters: 'Clear search and city',
  pastEndStatus: 'Nothing on this page',
  pastEndTitle: 'There is nothing on this page',
  pastEndBody: (total: number) =>
    total === 1 ? 'There is 1 hospital in all, on the first page.' : `There are ${total} hospitals in all, on earlier pages.`,
  firstPage: 'Go to the first page',

  listFailed: 'We could not load the hospitals. Please try again.',
  hospitalFailed: 'We could not load this hospital. Please try again.',
  offlineTitle: 'No connection',
  offlineBody: 'We could not reach the server. Check your internet connection and try again.',
  policiesPending: 'We cannot show this in the app right now. Please try again later.',
  busy: 'Too many requests. Please wait a moment and try again.',
  retry: 'Try again',

  linked: 'Linked',
  // Paid placement, whenever it exists, is always said out loud.
  sponsored: 'Sponsored',

  detailTitle: 'Hospital',
  loadingHospital: 'Loading hospital…',
  allHospitals: 'All hospitals',
  logoAlt: (name: string) => `${name} logo`,
  addressLabel: 'Address',
  phoneLabel: 'Phone',
  timezoneLabel: 'Time zone',
  linkedNote: 'Your record at this hospital is linked to your account.',
  viewDoctors: 'View Doctors',
  linkRecord: 'Link my record',

  // One neutral message for "no such hospital" and "not available in the app".
  notFoundTitle: 'This hospital is not available',
  notFoundBody: 'It may not be available in the app, or the link you followed may be wrong.',
  browseHospitals: 'Browse hospitals',
} as const
