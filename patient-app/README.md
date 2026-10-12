# Patient App

The patient-facing web application of Aetheris Health AI. A separate Vite
workspace from the Hospital app in `../frontend/`: its own lockfile, its own
build, nothing shared at build time.

Specification: [`docs/modules/15-patient-app.md`](../docs/modules/15-patient-app.md).

## What it does today

| Area | Route |
|---|---|
| Phone OTP sign-in | `/login`, `/verify-otp` |
| Home | `/` |
| Link a hospital record, or register | `/link-patient` |
| Hospital discovery and detail | `/hospitals`, `/hospitals/:hospitalRef` |
| Doctor discovery and profile | `/hospitals/:hospitalRef/doctors`, `…/doctors/:doctorRef` |
| Availability | `…/doctors/:doctorRef/availability` |
| Booking | `…/doctors/:doctorRef/book` |
| My appointments, detail, self-cancellation | `/appointments`, `/appointments/:appointmentRef` |

Not built yet: prescriptions, lab results, consent screens, rescheduling,
payments, notifications.

## Running it

Requires Node.js 22.19 or newer and a running backend (`make up` from the
repository root).

```bash
npm ci
npm run dev        # http://localhost:5174
```

The dev server proxies `/api` to the backend, so the refresh cookie stays
first-party. The port is fixed: the backend trusts only the origins listed in
`PATIENT_APP_ORIGINS` (default `http://localhost:5174`).

Configuration is optional; see [`.env.example`](.env.example).

To sign in locally the backend must run with `SMS_PROVIDER=dev`
(development only), which writes the one-time code to the backend log
instead of sending an SMS.

## Commands

| Command | What it does |
|---|---|
| `npm run dev` | Development server |
| `npm test` | Test suite (Vitest + Testing Library) |
| `npm run lint` | ESLint, including the import-boundary rule |
| `npm run typecheck` | TypeScript project build, no emit |
| `npm run check:tokens` | Fails if the design tokens drift from `frontend/src/index.css` |
| `npm run build` | Type check and production build into `dist/` |

From the repository root the same are available as `make patient-dev`,
`make patient-test`, `make patient-lint` and `make patient-build`. CI runs
lint, the token check, the build and the tests on every push and pull request
to `develop`.

## Layout

```
patient-app/
├── src/
│   ├── api/          # One module per backend area; responses are allow-listed here
│   ├── pages/        # Route components, each with its strings and tests
│   ├── components/   # Shared components
│   ├── layouts/      # Signed-in shell and navigation
│   ├── routes/       # Route guards
│   └── test/         # Fake API and fixtures
├── packages/
│   ├── design-tokens/  # Tokens, kept identical to the Hospital app's
│   ├── ui/             # Base components
│   └── api-core/       # HTTP client, session and refresh handling
└── scripts/            # check-token-drift
```

## Rules that are easy to break

- **No web storage.** The access token lives in memory only; the refresh
  token is an HTTP-only cookie. A test fails if anything is written to
  `localStorage` or `sessionStorage`.
- **The server decides.** Whether an appointment can be cancelled, which slots
  exist, and what "today" is all come from API responses. Times are shown in
  the hospital's timezone, never the device's.
- **Server text is text.** Nothing from a response is rendered as HTML.
- **References are validated before they are sent**, and responses are
  reduced to the documented fields before any page sees them.
- **Do not import from `../frontend/`.** The two apps share nothing at build
  time; an ESLint rule enforces it.
