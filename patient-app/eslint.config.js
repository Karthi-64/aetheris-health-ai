import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

/**
 * Boundary (docs/modules/15-patient-app.md §25.5): nothing in the Patient App
 * may import code that lives under the Hospital app's `frontend/` directory.
 * A specifier is refused when any of its path segments is `frontend`, which
 * covers every relative climb (`../frontend/…`, `../../frontend/…`) and any
 * absolute path into it.
 */
const HOSPITAL_APP_IMPORT = '(^|/)frontend(/|$)'
const HOSPITAL_APP_MESSAGE =
  'The Patient App must not import from the Hospital app (frontend/). Copy what you need into patient-app/packages instead.'

/** The shared packages must stay liftable: nothing in them reaches into the app. */
const APP_SOURCE_IMPORT = '^@/|(^|/)\\.\\./\\.\\./\\.\\./src(/|$)'

/** The access token lives in memory; nothing else may be kept in the browser. */
const WEB_STORAGE = ['localStorage', 'sessionStorage', 'indexedDB']
const WEB_STORAGE_MESSAGE = 'The Patient App keeps nothing in web storage (docs/modules/15-patient-app.md §5.5).'

const dynamicImport = (pattern, message) => ({
  selector: `ImportExpression > Literal[value=/${pattern.replaceAll('/', '\\/')}/]`,
  message,
})

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'coverage'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Project rule: no `any` (see frontend/CLAUDE.md).
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'no-console': 'error',
      'no-restricted-imports': [
        'error',
        { patterns: [{ regex: HOSPITAL_APP_IMPORT, message: HOSPITAL_APP_MESSAGE }] },
      ],
      'no-restricted-syntax': ['error', dynamicImport(HOSPITAL_APP_IMPORT, HOSPITAL_APP_MESSAGE)],
      'no-restricted-globals': [
        'error',
        ...WEB_STORAGE.map((name) => ({ name, message: WEB_STORAGE_MESSAGE })),
      ],
      'no-restricted-properties': [
        'error',
        ...['window', 'globalThis', 'self'].flatMap((object) =>
          WEB_STORAGE.map((property) => ({ object, property, message: WEB_STORAGE_MESSAGE })),
        ),
      ],
    },
  },
  {
    files: ['packages/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            { regex: HOSPITAL_APP_IMPORT, message: HOSPITAL_APP_MESSAGE },
            {
              regex: APP_SOURCE_IMPORT,
              message: 'A shared package must not import from patient-app/src.',
            },
          ],
        },
      ],
    },
  },
  // Route config + shadcn ui primitives legitimately co-export non-components
  // (the route table; cva variant helpers) — Fast Refresh doesn't apply here.
  {
    files: ['src/router.tsx', 'packages/ui/src/**/*.{ts,tsx}'],
    rules: { 'react-refresh/only-export-components': 'off' },
  },
  // Test files may use jsdom/vitest globals, and the storage tests have to
  // name the storage objects to prove nothing was written to them.
  {
    files: ['**/*.{test,spec}.{ts,tsx}', 'src/test/**'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: {
      'no-restricted-globals': 'off',
      'no-restricted-properties': 'off',
      'react-refresh/only-export-components': 'off',
    },
  },
  // Config/tooling files run in Node.
  {
    files: ['*.{js,ts,cjs,mjs}', 'vite.config.ts', 'scripts/**/*.mjs'],
    languageOptions: { globals: globals.node },
  },
)
