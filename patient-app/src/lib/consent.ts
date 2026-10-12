/**
 * The version of the hospital consent texts this build shows the patient
 * (`hospital_record_link`, `hospital_registration`). It is sent with every
 * link and registration so the server records exactly what was agreed to, and
 * refuses when it no longer matches the current policy.
 */
export const CONSENT_POLICY_VERSION = '2026-10-draft'
