import { lazy, Suspense } from 'react'
import { createBrowserRouter, Outlet } from 'react-router-dom'
import { RequireAuth } from '@/components/auth/RequireAuth'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { RouteFallback } from '@/components/layout/RouteFallback'
import { privacyDoc, termsDoc, hipaaDoc } from '@/content/legal'

// Route-level code splitting: each page (and its heavy deps like Recharts)
// lands in its own chunk, loaded on demand behind the Suspense boundary below.
const DashboardLayout = lazy(() => import('@/layouts/DashboardLayout'))
const LandingPage = lazy(() => import('@/pages/LandingPage'))
const LoginPage = lazy(() => import('@/pages/LoginPage'))
const ForgotPasswordPage = lazy(() => import('@/pages/auth/ForgotPasswordPage'))
const ResetPasswordPage = lazy(() => import('@/pages/auth/ResetPasswordPage'))
const ContactPage = lazy(() => import('@/pages/ContactPage'))
const LegalPage = lazy(() => import('@/pages/LegalPage'))
const PricingPage = lazy(() => import('@/pages/PricingPage'))
const NotFoundPage = lazy(() => import('@/pages/NotFoundPage'))
const DashboardPage = lazy(() => import('@/pages/DashboardPage'))
const PatientsPage = lazy(() => import('@/pages/patients/PatientsPage'))
const PatientDetailPage = lazy(() => import('@/pages/patients/PatientDetailPage'))
const DoctorsPage = lazy(() => import('@/pages/doctors/DoctorsPage'))
const DoctorDetailPage = lazy(() => import('@/pages/doctors/DoctorDetailPage'))
const AppointmentsPage = lazy(() => import('@/pages/appointments/AppointmentsPage'))
const BillingPage = lazy(() => import('@/pages/billing/BillingPage'))
const ReportsPage = lazy(() => import('@/pages/reports/ReportsPage'))
const UsersPage = lazy(() => import('@/pages/users/UsersPage'))
const SettingsPage = lazy(() => import('@/pages/settings/SettingsPage'))
const ProfilePage = lazy(() => import('@/pages/settings/ProfilePage'))

export const router = createBrowserRouter([
  {
    // Single Suspense boundary for every lazily-loaded route.
    element: (
      <Suspense fallback={<RouteFallback />}>
        <Outlet />
      </Suspense>
    ),
    children: [
      // Public marketing pages
      { path: '/', element: <LandingPage /> },
      { path: '/pricing', element: <PricingPage /> },
      { path: '/contact', element: <ContactPage /> },
      { path: '/privacy', element: <LegalPage doc={privacyDoc} /> },
      { path: '/terms', element: <LegalPage doc={termsDoc} /> },
      { path: '/hipaa', element: <LegalPage doc={hipaaDoc} /> },

      // Auth pages (public)
      { path: '/login', element: <LoginPage /> },
      { path: '/forgot-password', element: <ForgotPasswordPage /> },
      { path: '/reset-password', element: <ResetPasswordPage /> },

      // Authenticated app — shared enterprise shell
      {
        element: (
          <RequireAuth>
            <DashboardLayout />
          </RequireAuth>
        ),
        children: [
          { path: '/dashboard', element: <DashboardPage /> },
          {
            path: '/patients',
            element: (
              <RequirePermission permission="patient.read">
                <PatientsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/patients/:patientId',
            element: (
              <RequirePermission permission="patient.read">
                <PatientDetailPage />
              </RequirePermission>
            ),
          },
          {
            path: '/doctors',
            element: (
              <RequirePermission permission="doctor.read">
                <DoctorsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/doctors/:doctorId',
            element: (
              <RequirePermission permission="doctor.read">
                <DoctorDetailPage />
              </RequirePermission>
            ),
          },
          {
            path: '/appointments',
            element: (
              <RequirePermission permission="appointment.read">
                <AppointmentsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/billing',
            element: (
              <RequirePermission permission="billing.read">
                <BillingPage />
              </RequirePermission>
            ),
          },
          {
            path: '/reports',
            element: (
              <RequirePermission permission="report.read">
                <ReportsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/users',
            element: (
              <RequirePermission permission="user.read">
                <UsersPage />
              </RequirePermission>
            ),
          },
          {
            path: '/settings',
            element: (
              <RequirePermission permission="settings.read">
                <SettingsPage />
              </RequirePermission>
            ),
          },
          {
            // Own profile (module spec 02 §12). Deliberately NOT behind
            // RequirePermission: it acts only on the caller's own account, so
            // every authenticated user reaches it — including one with no
            // roles at all.
            path: '/settings/profile',
            element: <ProfilePage />,
          },
        ],
      },

      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
