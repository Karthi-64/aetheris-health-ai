// @vitest-environment node
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { ESLint } from 'eslint'
import { beforeAll, describe, expect, it } from 'vitest'
import { allSourceFiles, appRoot as root } from './sourceFiles.ts'

/**
 * The Patient App must not import anything that lives under the Hospital
 * app's `frontend/` (docs/modules/15-patient-app.md §25.5). The rule lives in
 * `eslint.config.js`; these tests run the REAL config against deliberately
 * failing fixtures, so weakening the rule fails the suite, and then check that
 * no import in the tree leaves `patient-app/` at all.
 */

let eslint: ESLint

/** The rule ids the project's ESLint config reports for `code` as if it were at `file`. */
async function violations(code: string, file: string): Promise<(string | null)[]> {
  const [result] = await eslint.lintText(code, { filePath: path.join(root, file) })
  return result.messages.filter((m) => m.severity === 2).map((m) => m.ruleId)
}

beforeAll(() => {
  eslint = new ESLint({ cwd: root })
})

describe('lint rule: no imports from the Hospital app', () => {
  it.each([
    ['a relative import from src', `import { ApiError } from '../../../frontend/src/api/types'\nexport const x = ApiError\n`, 'src/pages/home/fixture.ts'],
    ['a shallow climb', `import { cn } from '../frontend/src/lib/utils'\nexport const x = cn\n`, 'fixture.ts'],
    ['a re-export', `export * from '../../frontend/src/lib/apiErrors'\n`, 'src/lib/fixture.ts'],
    ['a type-only import', `import type { AuthUser } from '../../frontend/src/store/auth-store'\nexport type X = AuthUser\n`, 'src/store/fixture.ts'],
    ['an absolute path', `import { api } from '/Users/someone/repo/frontend/src/lib/api'\nexport const x = api\n`, 'src/api/fixture.ts'],
    ['a shared package reaching out', `import { cn } from '../../../../frontend/src/lib/utils'\nexport const x = cn\n`, 'packages/ui/src/fixture.ts'],
  ])('refuses %s', async (_case, code, file) => {
    expect(await violations(code, file)).toContain('no-restricted-imports')
  })

  it('refuses a dynamic import() too', async () => {
    const code = `export const load = () => import('../../frontend/src/pages/auth/LoginPage')\n`
    expect(await violations(code, 'src/pages/fixture.ts')).toContain('no-restricted-syntax')
  })

  it('allows the app’s own modules, the shared packages and libraries', async () => {
    const code = [
      `import { useState } from 'react'`,
      `import { ApiError } from '@atheris/api-core'`,
      `import { Button } from '@atheris/ui'`,
      `import { http } from '@/api/client'`,
      `import { authStrings } from './strings'`,
      `export const used = [useState, ApiError, Button, http, authStrings]`,
      ``,
    ].join('\n')
    expect(await violations(code, 'src/pages/auth/fixture.ts')).toEqual([])
  })

  it('keeps the shared packages liftable: they cannot import from the app', async () => {
    const viaAlias = `import { http } from '@/api/client'\nexport const x = http\n`
    const viaPath = `import { http } from '../../../src/api/client'\nexport const x = http\n`
    expect(await violations(viaAlias, 'packages/ui/src/fixture.ts')).toContain('no-restricted-imports')
    expect(await violations(viaPath, 'packages/api-core/src/fixture.ts')).toContain('no-restricted-imports')
  })

  it('also refuses web storage in app code', async () => {
    expect(await violations(`localStorage.setItem('t', 'x')\n`, 'src/store/fixture.ts')).toContain(
      'no-restricted-globals',
    )
    expect(await violations(`window.sessionStorage.setItem('t', 'x')\n`, 'src/store/fixture.ts')).toContain(
      'no-restricted-properties',
    )
  })
})

describe('the source tree', () => {
  // This file is left out: its fixtures are the forbidden imports themselves.
  const files = allSourceFiles().filter((file) => file !== import.meta.filename)
  const imports = files.flatMap((file) =>
    [...readFileSync(file, 'utf8').matchAll(/(?:from|import)\s*\(?\s*['"]([^'"]+)['"]/g)].map((match) => ({
      file: path.relative(root, file),
      specifier: match[1],
    })),
  )

  it('has imports to check', () => {
    expect(files.length).toBeGreaterThan(30)
    expect(imports.length).toBeGreaterThan(100)
  })

  it('has no import that resolves outside patient-app/, so none can reach ../frontend', () => {
    const escaping = imports.filter(({ file, specifier }) => {
      if (/(^|\/)frontend(\/|$)/.test(specifier)) return true
      if (!specifier.startsWith('.')) return false
      const target = path.resolve(root, path.dirname(file), specifier)
      return path.relative(root, target).startsWith('..')
    })
    expect(escaping).toEqual([])
  })

  it('imports only the app itself, @atheris/* packages and declared dependencies', () => {
    const manifest = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8')) as {
      dependencies: Record<string, string>
      devDependencies: Record<string, string>
    }
    const declared = new Set([...Object.keys(manifest.dependencies), ...Object.keys(manifest.devDependencies)])
    const packageOf = (specifier: string) =>
      specifier.startsWith('@') ? specifier.split('/').slice(0, 2).join('/') : specifier.split('/')[0]

    const undeclared = imports
      .map(({ specifier }) => specifier)
      .filter((s) => !s.startsWith('.') && !s.startsWith('@/') && !s.startsWith('node:'))
      .map(packageOf)
      .filter((name) => !declared.has(name))

    expect([...new Set(undeclared)]).toEqual([])
  })
})
