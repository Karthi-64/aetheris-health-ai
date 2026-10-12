/**
 * @atheris/ui — shadcn primitives seeded by copying from the Hospital app's
 * `frontend/src/components/ui/` (docs/modules/15-patient-app.md §25.3). Only
 * the primitives the Patient App uses are here; nothing in this package
 * imports from outside it.
 */
export { Alert, alertVariants } from './alert'
export { Button, buttonVariants } from './button'
export {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from './card'
export { EmptyState } from './empty-state'
export { Skeleton } from './skeleton'
export { cn } from './lib/utils'
