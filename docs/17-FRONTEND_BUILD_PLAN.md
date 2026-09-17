# Aetheris Health AI — Frontend Build Plan & Gap Analysis

> **Status:** proposal for team review · **Owner:** frontend (Jaikanth) · **Date:** 2026-08-09
>
> This document reconciles the current frontend prototype with the official
> **Frontend Product Specification (v1.0, Parts 1–12)** and the existing project
> docs ([15-SPRINT_PLAN](15-SPRINT_PLAN.md), [09-PROJECT_STRUCTURE](09-PROJECT_STRUCTURE.md),
> [04-TECH_STACK](04-TECH_STACK.md), [docs/modules/](modules/)). It records the
> design decision, the gap between what exists and what the spec requires, and a
> phased plan mapped onto the agreed sprints.

---

## 1. Context

The current `frontend/` was built from a **Google Stitch "Clinical Glass" design**
(glassmorphism + soft neomorphism, deep-navy `#0A2540` + electric-cyan `#00D4FF`,
Manrope/Public Sans, Material Symbols). It shipped a marketing site + 3 app
screens + auth.

The **Frontend Product Specification** describes a different target: a desktop-first,
information-dense **enterprise Hospital Management System** with RBAC across 7 roles
and ~10 functional modules. Spec Part 11 mandates a **flat** design language
(`#2563EB`, Inter, Lucide, *"avoid gradients and glassmorphism"*).

## 2. Design decision (product call)

**We keep the Clinical Glass aesthetic** and implement the spec's *functionality*
inside it. This is an explicit, team-approved override of Spec Part 11 §1–2
(flat / no-glass). Consequences:

- Design tokens, glass/neo components, navy+cyan palette, and Manrope type **stay**.
- Spec references to `#2563EB` / flat surfaces are treated as **superseded** by the
  Clinical Glass system for this build.

### Open reconciliation items (need a quick ruling)

| Item | Spec / docs say | We have | Recommendation |
| --- | --- | --- | --- |
| **Icons** | Lucide (Part 11 §14, 04-TECH_STACK) | Material Symbols | **Migrate to Lucide** — pairs with shadcn, independent of the glass look. Low risk. |
| **Body font** | Inter (Part 1, 11) | Manrope + Public Sans | Keep Manrope (part of the glass identity) — confirm. |
| **Self-signup** | None — admins create staff (Part 2B, Settings §5) | `/signup` page | **Drop** (decided). |
| **Dark mode** | Light + Dark (Part 2C §12) | Light only | Add in Phase 0 shell. |
| **Priority** | Desktop-first, mobile = v2 (Part 11 §16) | Mobile-optimized already | Harmless bonus; keep. |

## 3. Current state → decisions (Keep / Adapt / Drop)

| Built | Decision |
| --- | --- |
| Stack (React 19, TS, Vite, Tailwind v4, shadcn, Router, Zustand, TanStack Query, RHF+Zod, Axios, Recharts, Sonner) | **Keep** — matches 04-TECH_STACK |
| Clinical Glass design tokens + glass/neo components | **Keep / extend** into the full component library |
| Login + `RequireAuth` guards | **Keep / extend** (add forgot/reset, session, RBAC) |
| App sidebar | **Adapt** → collapsible sidebar with all modules + a top bar |
| Dashboard (single, generic) | **Adapt** → role-based dashboards |
| Records screen | **Adapt** → real Patients module |
| Landing + Contact + Privacy/Terms/HIPAA | **Adapt** landing to Spec 2A (Book Demo, module cards, workflow, FAQ); keep legal/contact |
| **Diagnostics** screen | **Drop** (not in spec; concept folds into Doctor consultation later) |
| **How-it-Works / Technology** marketing pages | **Drop** (not in Spec 2A) |
| **Signup** page | **Drop** (no staff self-signup) |

## 4. Structural alignment (refactor to 09-PROJECT_STRUCTURE)

Current layout is flat (`src/pages/*.tsx`, `src/router.tsx`, `src/lib/*`). The agreed
structure is module-foldered. **Phase 0 refactors to it:**

```
src/
  app/         App.tsx · AppRoutes.tsx · providers.tsx
  layouts/     PublicLayout · AuthLayout · DashboardLayout
  pages/       auth/ dashboard/ patients/ doctors/ appointments/ billing/
               laboratory/ pharmacy/ inventory/ reports/ settings/ ai/
  components/  ui/ forms/ tables/ charts/ layout/ patient/ appointment/ billing/ ai/
  hooks/       useAuth · usePermissions · usePagination · useDebounce · useAiStream
  api/         client.ts · auth.ts · patients.ts · doctors.ts · appointments.ts · …
  services/    tokenStore · notificationService · errorReporter
  store/       Zustand stores
```

> Note the spec docx covers Parts 1–12, but `docs/modules/` also lists **Laboratory,
> Pharmacy, Inventory** — the true scope is larger than the 12 docx parts. Plan
> accommodates them as later modules.

## 5. Module gap matrix

| Module | Spec | Module doc | Current | Action |
| --- | --- | --- | --- | --- |
| Public / Landing | 2A | — | Built (off-structure) | Restructure to 2A |
| Auth (login/forgot/reset/session) | 2B/2C | 01-authentication | Login + guards only | Add forgot/reset/session/first-login |
| App shell (topbar, search, notifications, breadcrumbs, profile, copilot entry) | 2C | 11-notifications | Sidebar only | **Build** |
| RBAC (7 roles, permissions) | 1, 2B, 10 | 02-user-management | Server-issued permission codes; nav + routes gated | **Built** (Sprint 2) |
| Dashboards (role-based) | 3 | 10-reports-dashboard | 1 generic | **Build** role variants |
| Patients | 4 | 03-patient-management | Records stub | **Build** |
| Doctors / Departments | 5 | 04-doctor-management | — | **Build** |
| Appointments | 6 | 05-appointment-management | — | **Build** |
| Billing | 7 | 06-billing | — | **Build** |
| Reports & Analytics | 8 | 10-reports-dashboard | — | **Build** |
| AI Copilot (side panel) | 9 | 13-ai-assistant | — | **Build** |
| Users & Roles (admin) | 10 | 02-user-management | `/users`: directory, invite, role assign/remove, deactivate | **Built** (Sprint 2) |
| Own profile | 10 | 02-user-management | `/settings/profile`: name/phone, password, MFA | **Built** (Sprint 2) |
| Settings / Admin | 10 | 14-hospital-settings | — | **Build** |
| Laboratory / Pharmacy / Inventory | — | 07/08/09 | — | Later modules |
| Design System / Component Library | 11 | — | Partial (glass) | **Extend** |

## 6. Component library to build (Phase 0, spec Part 11)

`DataTable` (search · sort · filters · pagination · column visibility · export · row actions),
`KpiCard`, `Drawer`, `Modal/Dialog`, form kit (`Input` `Textarea` `Select` `DatePicker`
`TimePicker` `Checkbox` `Radio` `Toggle` `FileUpload` with inline validation),
`Tabs`, `Breadcrumbs`, `EmptyState`, `Skeleton`, `Alert`, chart wrappers
(`LineChart` `BarChart` `PieChart` `AreaChart`), `CommandSearch`, `NotificationDrawer`,
`UserMenu` — all in the Clinical Glass style, all reused across modules.

## 7. Phased execution (mapped to 15-SPRINT_PLAN)

| Phase | Maps to | Contents |
| --- | --- | --- |
| **0 — Foundation** | Sprint 0/1 | Refactor to project structure · component library · app shell (topbar+sidebar+copilot scaffold+breadcrumbs) · RBAC · dark mode · Landing→2A · **drop off-spec extras** |
| **1 — Auth complete** | Sprint 1 | Forgot/reset password · first-time login · session timeout + expiry warning |
| **2 — Dashboards + Users + Settings** | Sprint 2 | Role-based dashboards · User management · Hospital Settings |
| **3 — Patients** | Sprint 3 | List/register/profile (8 tabs)/admissions/documents/AI summary |
| **4 — Doctors + Appointments** | Sprint 4–5 | Directory · departments · availability calendar · leaves · booking · queue · calendar views · lifecycle |
| **5 — Billing + Reports** | Sprint 6+ | Invoices · payments · refunds · revenue dashboard · report categories · export center |
| **6 — AI Copilot** | Sprint 6+ | Persistent panel · suggested prompts · function-call stubs · history |
| **7 — Lab / Pharmacy / Inventory** | later | Per docs/modules 07–09 |

## 8. Cross-cutting standards (every module)

- **States:** skeleton loaders, meaningful empty states (+ CTA), friendly error + retry (Part 2C, 12).
- **RBAC:** hide unauthorized widgets/actions entirely; enforce on routes and nav.
- **Data:** all screens use **TanStack Query + Axios** clients (`src/api/<module>.ts`) with mock data behind clear `TODO(backend)` seams; never surface raw backend errors.
- **UX:** breadcrumbs on every page, primary action top-right, confirm destructive actions, instant inline validation.
- **A11y:** keyboard nav, visible focus, WCAG contrast, semantic HTML.

## 9. Immediate next step

On approval of this doc, begin **Phase 0** — it unblocks every module by delivering
the shell, component library, RBAC, and structure refactor. The current
`j-jaikanth` branch already carries the reusable foundation.
