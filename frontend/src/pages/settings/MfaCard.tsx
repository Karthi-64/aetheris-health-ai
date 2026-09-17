import { useState } from 'react'
import { toast } from 'sonner'
import { KeyRound, Loader2 } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/api/types'
import { useConfirmMfa, useDisableMfa, useEnrollMfa } from '@/api/profile'

type Stage = 'idle' | 'verifying-password' | 'confirming'

/**
 * MFA settings (module spec §12). Enrollment is the backend's two-step flow:
 * `POST /auth/mfa/enroll` returns a TOTP secret once, then
 * `POST /auth/mfa/confirm` proves the authenticator is working before MFA is
 * actually switched on. The secret is rendered as text rather than a QR image
 * so the page needs no extra dependency; authenticator apps accept manual
 * entry.
 */
export function MfaCard({ enabled, loading }: { enabled: boolean; loading?: boolean }) {
  const [stage, setStage] = useState<Stage>('idle')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [secret, setSecret] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const enroll = useEnrollMfa()
  const confirm = useConfirmMfa()
  const disable = useDisableMfa()

  function resetFlow() {
    setStage('idle')
    setPassword('')
    setCode('')
    setSecret(null)
    setError(null)
  }

  async function startEnrollment() {
    setError(null)
    try {
      const result = await enroll.mutateAsync(password)
      setSecret(result.secret)
      setPassword('')
      setStage('confirming')
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? 'That password is not correct.'
          : 'Could not start MFA enrollment. Please try again.',
      )
    }
  }

  async function finishEnrollment() {
    if (!secret) return
    setError(null)
    try {
      await confirm.mutateAsync({ secret, code })
      toast.success('Two-factor authentication is on')
      resetFlow()
    } catch {
      setError('That code was not accepted. Check your authenticator and try again.')
    }
  }

  async function turnOff() {
    setError(null)
    try {
      await disable.mutateAsync({ password, code })
      toast.success('Two-factor authentication is off')
      resetFlow()
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? 'Check your password and the 6-digit code.'
          : 'Could not disable MFA. Please try again.',
      )
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="text-secondary size-5" /> Two-factor authentication
          {loading ? null : (
            <Badge variant={enabled ? 'success' : 'neutral'}>{enabled ? 'On' : 'Off'}</Badge>
          )}
        </CardTitle>
        <CardDescription>
          An authenticator app generates a 6-digit code each time you sign in.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <Skeleton className="h-10 w-48" />
        ) : (
          <>
            {error && <Alert variant="error">{error}</Alert>}

            {stage === 'idle' && (
              <Button
                variant="secondary"
                className="rounded-full"
                onClick={() => setStage('verifying-password')}
              >
                {enabled ? 'Turn off' : 'Set up'}
              </Button>
            )}

            {stage === 'verifying-password' && (
              <div className="space-y-4">
                <Field
                  label="Password"
                  required
                  hint="Confirm it is you before changing a security setting."
                >
                  {(props) => (
                    <Input
                      {...props}
                      type="password"
                      autoComplete="current-password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                    />
                  )}
                </Field>

                {enabled && (
                  <Field label="Current 6-digit code" required>
                    {(props) => (
                      <Input
                        {...props}
                        inputMode="numeric"
                        maxLength={6}
                        value={code}
                        onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                      />
                    )}
                  </Field>
                )}

                <div className="flex justify-end gap-2">
                  <Button type="button" variant="ghost" onClick={resetFlow}>
                    Cancel
                  </Button>
                  <Button
                    className="rounded-full"
                    disabled={!password || (enabled && code.length !== 6) || enroll.isPending || disable.isPending}
                    onClick={enabled ? turnOff : startEnrollment}
                  >
                    {(enroll.isPending || disable.isPending) && (
                      <Loader2 className="size-4 animate-spin" />
                    )}
                    {enabled ? 'Turn off MFA' : 'Continue'}
                  </Button>
                </div>
              </div>
            )}

            {stage === 'confirming' && secret && (
              <div className="space-y-4">
                <Alert variant="info" title="Add this key to your authenticator">
                  <code className="font-mono text-sm tracking-widest break-all">{secret}</code>
                  <p className="mt-2 text-xs">
                    It is shown once. Enter the 6-digit code it generates to finish.
                  </p>
                </Alert>

                <Field label="6-digit code" required>
                  {(props) => (
                    <Input
                      {...props}
                      inputMode="numeric"
                      maxLength={6}
                      value={code}
                      onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                    />
                  )}
                </Field>

                <div className="flex justify-end gap-2">
                  <Button type="button" variant="ghost" onClick={resetFlow}>
                    Cancel
                  </Button>
                  <Button
                    className="rounded-full"
                    disabled={code.length !== 6 || confirm.isPending}
                    onClick={finishEnrollment}
                  >
                    {confirm.isPending && <Loader2 className="size-4 animate-spin" />}
                    Turn on MFA
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
