import { FileClock, Hospital, Settings as SettingsIcon } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { EmptyState } from '@/components/ui/empty-state'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { usePermissions } from '@/hooks/usePermissions'
import { HospitalSettingsTab } from './HospitalSettingsTab'
import { AuditLogTab } from './AuditLogTab'

/**
 * Settings shell (module 14 §12, module 12 §12).
 *
 * Tabs are permission-driven: the Hospital tab needs `settings.read` and the
 * Audit tab `audit.read`, so a caller who somehow reaches `/settings` without
 * them sees an explanation rather than a dead screen. The route itself is
 * already gated on `settings.read` (router.tsx).
 */
export default function SettingsPage() {
  const { can } = usePermissions()
  const showHospital = can('settings.read')
  const showAudit = can('audit.read')

  return (
    <div className="w-full">
      <PageHeader
        title="Settings"
        subtitle="Hospital configuration, departments and the security audit trail."
      />

      {!showHospital && !showAudit ? (
        <EmptyState
          icon={SettingsIcon}
          title="No access to settings"
          description="Your permission set does not include hospital settings or the audit trail."
        />
      ) : showAudit && !showHospital ? (
        // Reached only via a direct URL — show the one tab they may use.
        <AuditLogTab />
      ) : (
        <Tabs defaultValue="hospital">
          <TabsList>
            {showHospital && (
              <TabsTrigger value="hospital">
                <Hospital className="size-4" /> Hospital
              </TabsTrigger>
            )}
            {showAudit && (
              <TabsTrigger value="audit">
                <FileClock className="size-4" /> Audit log
              </TabsTrigger>
            )}
          </TabsList>

          {showHospital && (
            <TabsContent value="hospital">
              <HospitalSettingsTab />
            </TabsContent>
          )}
          {showAudit && (
            <TabsContent value="audit">
              <AuditLogTab />
            </TabsContent>
          )}
        </Tabs>
      )}
    </div>
  )
}
