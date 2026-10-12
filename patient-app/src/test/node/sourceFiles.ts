import { readdirSync, statSync } from 'node:fs'
import path from 'node:path'

/** The Patient App's root directory (`patient-app/`). */
export const appRoot = path.resolve(import.meta.dirname, '..', '..', '..')

const isTest = (file: string) => /\.test\.(ts|tsx)$/.test(file) || file.includes(`${path.sep}test${path.sep}`)

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const file = path.join(dir, name)
    if (name === 'node_modules') return []
    if (statSync(file).isDirectory()) return walk(file)
    return /\.(ts|tsx)$/.test(name) ? [file] : []
  })
}

/** Every TypeScript file of the app and its shared packages. */
export const allSourceFiles = (): string[] =>
  [path.join(appRoot, 'src'), path.join(appRoot, 'packages')].flatMap(walk)

/** The files that ship: everything except tests and test helpers. */
export const shippedSourceFiles = (): string[] => allSourceFiles().filter((file) => !isTest(file))
