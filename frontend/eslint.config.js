import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

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
    },
  },
  // Route config + shadcn ui primitives legitimately co-export non-components
  // (the router object; cva variant helpers) — Fast Refresh doesn't apply here.
  {
    files: ['src/router.tsx', 'src/components/ui/**/*.{ts,tsx}', 'src/pages/**/columns.tsx'],
    rules: { 'react-refresh/only-export-components': 'off' },
  },
  // Test files may use jsdom/vitest globals.
  {
    files: ['**/*.{test,spec}.{ts,tsx}', 'src/test/**'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
  // Config/tooling files run in Node.
  {
    files: ['*.{js,ts,cjs,mjs}', 'vite.config.ts'],
    languageOptions: { globals: globals.node },
  },
)
