/**
 * Where a hospital's pages live in the app. The reference comes from the
 * server and is escaped, so it can only ever be one path segment.
 */
export const hospitalPath = (hospitalRef: string) => `/hospitals/${encodeURIComponent(hospitalRef)}`

export const hospitalDoctorsPath = (hospitalRef: string) => `${hospitalPath(hospitalRef)}/doctors`
