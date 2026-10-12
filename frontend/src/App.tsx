import { ThemeProvider } from 'next-themes'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { Toaster } from '@/components/ui/sonner'
import { SessionRestorer } from '@/components/auth/SessionRestorer'
import { queryClient } from '@/lib/query-client'
import { router } from '@/router'

export default function App() {
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <QueryClientProvider client={queryClient}>
        <SessionRestorer>
          <RouterProvider router={router} />
        </SessionRestorer>
        {/* Offset to sit below the top bar: a toast in the corner itself covers
            the notification bell, which then can't be clicked until it clears. */}
        <Toaster
          richColors
          position="top-right"
          offset={{ top: 92, right: 24 }}
          mobileOffset={{ top: 84 }}
        />
      </QueryClientProvider>
    </ThemeProvider>
  )
}
