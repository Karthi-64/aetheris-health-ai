/**
 * Token drift check (docs/modules/15-patient-app.md §25.3).
 *
 * In V1 the Atheris design tokens exist twice: in the Hospital app's
 * `frontend/src/index.css` and in `@atheris/design-tokens`. This compares the
 * `@theme` and `.dark` blocks of the two files and fails on any difference, so
 * a token change in one place cannot silently diverge. It only reads files.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'

const root = path.resolve(import.meta.dirname, '..')
const SOURCES = {
  hospital: path.resolve(root, '..', 'frontend', 'src', 'index.css'),
  patient: path.resolve(root, 'packages', 'design-tokens', 'tokens.css'),
}

/** The top-level block that starts with `opener`, through its closing brace. */
function block(css, opener, file) {
  const lines = css.split('\n')
  const start = lines.findIndex((line) => line === opener)
  const end = lines.findIndex((line, i) => i > start && line === '}')
  if (start === -1 || end === -1) throw new Error(`No "${opener}" block in ${file}`)
  return lines.slice(start, end + 1).join('\n')
}

const css = Object.fromEntries(
  Object.entries(SOURCES).map(([name, file]) => [name, readFileSync(file, 'utf8')]),
)

const drifted = ['@theme {', '.dark {'].filter(
  (opener) => block(css.hospital, opener, SOURCES.hospital) !== block(css.patient, opener, SOURCES.patient),
)

if (drifted.length > 0) {
  process.stderr.write(
    `Design tokens have drifted between frontend/src/index.css and @atheris/design-tokens: ${drifted.join(', ')}\n`,
  )
  process.exit(1)
}
process.stdout.write('Design tokens match frontend/src/index.css.\n')
