import { lazy, Suspense } from 'react'
import { Navigate, createBrowserRouter, Outlet } from 'react-router-dom'
import { RequireAuth } from '@/components/auth/RequireAuth'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { RouteFallback } from '@/components/layout/RouteFallback'
import RouteErrorPage from '@/pages/RouteErrorPage'
import { privacyDoc, termsDoc, securityDoc } from '@/content/legal'

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
const InvoiceDetailPage = lazy(() => import('@/pages/billing/InvoiceDetailPage'))
const ReportsPage = lazy(() => import('@/pages/reports/ReportsPage'))
const PatientsReportPage = lazy(() => import('@/pages/reports/PatientsReportPage'))
const AppointmentsReportPage = lazy(() => import('@/pages/reports/AppointmentsReportPage'))
const RevenueReportPage = lazy(() => import('@/pages/reports/RevenueReportPage'))
const OutstandingReportPage = lazy(() => import('@/pages/reports/OutstandingReportPage'))
const LabOrdersPage = lazy(() => import('@/pages/laboratory/LabOrdersPage'))
const LabOrderDetailPage = lazy(() => import('@/pages/laboratory/LabOrderDetailPage'))
const LabCatalogPage = lazy(() => import('@/pages/laboratory/LabCatalogPage'))
const PharmacyIndexPage = lazy(() => import('@/pages/pharmacy/PharmacyIndexPage'))
const PrescriptionsPage = lazy(() => import('@/pages/pharmacy/PrescriptionsPage'))
const PrescriptionDetailPage = lazy(() => import('@/pages/pharmacy/PrescriptionDetailPage'))
const MedicinesPage = lazy(() => import('@/pages/pharmacy/MedicinesPage'))
const MedicineStockPage = lazy(() => import('@/pages/pharmacy/MedicineStockPage'))
const PurchaseOrdersPage = lazy(() => import('@/pages/pharmacy/PurchaseOrdersPage'))
const PurchaseOrderDetailPage = lazy(() => import('@/pages/pharmacy/PurchaseOrderDetailPage'))
const VendorsPage = lazy(() => import('@/pages/pharmacy/VendorsPage'))
const InventoryIndexPage = lazy(() => import('@/pages/inventory/InventoryIndexPage'))
const StockPage = lazy(() => import('@/pages/inventory/StockPage'))
const MovementsPage = lazy(() => import('@/pages/inventory/MovementsPage'))
const ItemsPage = lazy(() => import('@/pages/inventory/ItemsPage'))
const LocationsPage = lazy(() => import('@/pages/inventory/LocationsPage'))
const InventoryOrdersPage = lazy(() => import('@/pages/inventory/InventoryOrdersPage'))
const InventoryOrderDetailPage = lazy(() => import('@/pages/inventory/InventoryOrderDetailPage'))
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
    // Catches a page that throws while rendering and a lazy chunk that fails
    // to load. Imported eagerly: it must render when chunks cannot be fetched.
    errorElement: <RouteErrorPage />,
    children: [
      // Public marketing pages
      { path: '/', element: <LandingPage /> },
      { path: '/pricing', element: <PricingPage /> },
      { path: '/contact', element: <ContactPage /> },
      { path: '/privacy', element: <LegalPage doc={privacyDoc} /> },
      { path: '/terms', element: <LegalPage doc={termsDoc} /> },
      { path: '/security', element: <LegalPage doc={securityDoc} /> },
      // The page's old address, kept so existing links still land somewhere true.
      { path: '/hipaa', element: <Navigate to="/security" replace /> },

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
            // Either invoice read code opens Billing; the server narrows a
            // doctor's list to their own visits (docs/18-API_CONTRACTS.md §6.11).
            path: '/billing',
            element: (
              <RequirePermission group="invoice.read">
                <BillingPage />
              </RequirePermission>
            ),
          },
          {
            path: '/billing/:invoiceId',
            element: (
              <RequirePermission group="invoice.read">
                <InvoiceDetailPage />
              </RequirePermission>
            ),
          },
          {
            path: '/laboratory',
            element: (
              <RequirePermission permission="lab.order.read">
                <LabOrdersPage />
              </RequirePermission>
            ),
          },
          // Orders are listed on the worklist; a typed or shortened address lands there.
          { path: '/laboratory/orders', element: <Navigate to="/laboratory" replace /> },
          {
            path: '/laboratory/orders/:orderId',
            element: (
              <RequirePermission permission="lab.order.read">
                <LabOrderDetailPage />
              </RequirePermission>
            ),
          },
          {
            // Reading the catalog is its own code: a nurse can read orders
            // but not the catalog (docs/18-API_CONTRACTS.md §8.2).
            path: '/laboratory/catalog',
            element: (
              <RequirePermission permission="lab.test.read">
                <LabCatalogPage />
              </RequirePermission>
            ),
          },
          {
            // Any pharmacy read code opens the module; the index sends each user to
            // the first screen their permissions cover (docs/18-API_CONTRACTS.md §9.1).
            path: '/pharmacy',
            element: (
              <RequirePermission group="pharmacy.medicine.read">
                <PharmacyIndexPage />
              </RequirePermission>
            ),
          },
          {
            path: '/pharmacy/prescriptions',
            element: (
              <RequirePermission permission="pharmacy.prescription.read">
                <PrescriptionsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/pharmacy/prescriptions/:prescriptionId',
            element: (
              <RequirePermission permission="pharmacy.prescription.read">
                <PrescriptionDetailPage />
              </RequirePermission>
            ),
          },
          {
            path: '/pharmacy/medicines',
            element: (
              <RequirePermission permission="pharmacy.medicine.read">
                <MedicinesPage />
              </RequirePermission>
            ),
          },
          {
            // Stock is its own code: a doctor reads the catalog but not the batches.
            path: '/pharmacy/medicines/:medicineId',
            element: (
              <RequirePermission permission="pharmacy.batch.read">
                <MedicineStockPage />
              </RequirePermission>
            ),
          },
          {
            path: '/pharmacy/purchase-orders',
            element: (
              <RequirePermission permission="pharmacy.po.read">
                <PurchaseOrdersPage />
              </RequirePermission>
            ),
          },
          {
            path: '/pharmacy/purchase-orders/:orderId',
            element: (
              <RequirePermission permission="pharmacy.po.read">
                <PurchaseOrderDetailPage />
              </RequirePermission>
            ),
          },
          {
            path: '/pharmacy/vendors',
            element: (
              <RequirePermission permission="pharmacy.vendor.read">
                <VendorsPage />
              </RequirePermission>
            ),
          },
          {
            // The low-stock notification links here (docs/18-API_CONTRACTS.md §10.6).
            path: '/inventory',
            element: (
              <RequirePermission group="inventory.stock.read">
                <InventoryIndexPage />
              </RequirePermission>
            ),
          },
          {
            path: '/inventory/stock',
            element: (
              <RequirePermission permission="inventory.stock.read">
                <StockPage />
              </RequirePermission>
            ),
          },
          {
            path: '/inventory/movements',
            element: (
              <RequirePermission permission="inventory.stock.read">
                <MovementsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/inventory/items',
            element: (
              <RequirePermission permission="inventory.item.read">
                <ItemsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/inventory/locations',
            element: (
              <RequirePermission permission="inventory.location.read">
                <LocationsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/inventory/purchase-orders',
            element: (
              <RequirePermission permission="inventory.po.read">
                <InventoryOrdersPage />
              </RequirePermission>
            ),
          },
          {
            path: '/inventory/purchase-orders/:orderId',
            element: (
              <RequirePermission permission="inventory.po.read">
                <InventoryOrderDetailPage />
              </RequirePermission>
            ),
          },
          {
            // Either report code opens Reports: an administrator reads all four,
            // billing staff the two financial ones
            // (docs/modules/10-reports-dashboard.md §10). Doctors and receptionists hold a
            // dashboard code only and are sent home.
            path: '/reports',
            element: (
              <RequirePermission group="report.admin.read">
                <ReportsPage />
              </RequirePermission>
            ),
          },
          {
            path: '/reports/patients',
            element: (
              <RequirePermission permission="report.admin.read">
                <PatientsReportPage />
              </RequirePermission>
            ),
          },
          {
            path: '/reports/appointments',
            element: (
              <RequirePermission permission="report.admin.read">
                <AppointmentsReportPage />
              </RequirePermission>
            ),
          },
          {
            path: '/reports/revenue',
            element: (
              <RequirePermission group="report.admin.read">
                <RevenueReportPage />
              </RequirePermission>
            ),
          },
          {
            path: '/reports/outstanding',
            element: (
              <RequirePermission group="report.admin.read">
                <OutstandingReportPage />
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
