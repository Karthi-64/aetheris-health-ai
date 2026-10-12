import { AppProviders } from '@/AppProviders'
import { createAppRouter } from '@/router'

const router = createAppRouter()

export default function App() {
  return <AppProviders router={router} />
}
