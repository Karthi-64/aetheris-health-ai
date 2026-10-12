// @vitest-environment node
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { appRoot, shippedSourceFiles } from './sourceFiles.ts'

/**
 * The access token is memory-only (docs/modules/15-patient-app.md §5.5). The
 * behavioural proof is `src/test/webStorage.test.tsx`; this is the static one:
 * no shipped file so much as names a browser storage API, a script-readable
 * cookie, or Zustand's `persist` middleware.
 */
describe('shipped source', () => {
  it('never names web storage, IndexedDB, document.cookie or a persist middleware', () => {
    const shipped = shippedSourceFiles()
    expect(shipped.length).toBeGreaterThan(30)

    const withoutComments = (code: string) => code.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')
    const offenders = shipped.filter((file) =>
      /\b(localStorage|sessionStorage|indexedDB|document\.cookie)\b|zustand\/middleware/.test(
        withoutComments(readFileSync(file, 'utf8')),
      ),
    )

    expect(offenders.map((file) => path.relative(appRoot, file))).toEqual([])
  })
})
