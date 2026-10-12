import { Link } from 'react-router-dom'
import { usePermissions } from '@/hooks/usePermissions'

/** A patient's name, linking to their record for a user who may open it. */
export function PatientName({ id, name }: { id: string; name: string }) {
  const { can } = usePermissions()
  if (!can('patient.read')) return <span className="font-semibold">{name}</span>
  return (
    <Link
      to={`/patients/${id}`}
      className="hover:text-secondary font-semibold transition-colors hover:underline"
    >
      {name}
    </Link>
  )
}
