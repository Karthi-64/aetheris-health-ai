/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import path from 'node:path'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The Patient App is its own Vite application (docs/modules/15-patient-app.md
// §25): its own port, its own build, nothing shared with `frontend/` at build
// time.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    port: 5174,
    // The backend only trusts this origin for the patient cookie endpoints
    // (PATIENT_APP_ORIGINS), so never drift to another port silently.
    strictPort: true,
    proxy: {
      // Same-origin API in dev: keeps the refresh cookie first-party.
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        // The Origin header is left as the browser sent it; the backend checks
        // it against its configured patient origins.
        changeOrigin: false,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
