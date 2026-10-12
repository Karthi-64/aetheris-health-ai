/**
 * What a hospital's page hands to the link page through router state: that
 * hospital's code, so the patient does not have to type it. It is the public
 * reference and nothing else — no name, no id, nothing about the patient.
 */
export interface LinkPatientState {
  hospitalCode: string
}

/** The longest code the link form accepts. */
const MAX_CODE_LENGTH = 100

export const linkStateFor = (hospitalRef: string): LinkPatientState => ({ hospitalCode: hospitalRef })

/**
 * The code to pre-fill, or `''`. Router state survives a reload and can be
 * anything, so only a string the form would accept is used.
 */
export function hospitalCodeFrom(state: unknown): string {
  const code = (state as Partial<LinkPatientState> | null)?.hospitalCode
  if (typeof code !== 'string') return ''
  const trimmed = code.trim()
  return trimmed.length <= MAX_CODE_LENGTH ? trimmed : ''
}
