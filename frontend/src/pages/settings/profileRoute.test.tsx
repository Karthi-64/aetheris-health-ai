import { describe, it, expect } from 'vitest'
import { isValidElement, type ReactElement } from 'react'
import { router } from '@/router'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { RequireAuth } from '@/components/auth/RequireAuth'

/**
 * Route-shape assertions against the real router config (not a mirror of it).
 *
 * The own-profile page is the one authenticated screen that must NOT sit
 * behind a `user.*` permission: module spec §4 rule 6 says a user with no
 * roles can still log in, and §3 grants "anyone authenticated" the right to
 * view and update their own profile.
 */

interface RouteNode {
  path?: string
  element?: unknown
  children?: RouteNode[]
}

function findRoute(nodes: RouteNode[], path: string, guarded: unknown[] = []): {
  node: RouteNode
  guards: unknown[]
} | null {
  for (const node of nodes) {
    const guards = [...guarded]
    if (isValidElement(node.element)) {
      guards.push((node.element as ReactElement).type)
    }
    if (node.path === path) return { node, guards }
    if (node.children) {
      const hit = findRoute(node.children, path, guards)
      if (hit) return hit
    }
  }
  return null
}

describe('/settings/profile route', () => {
  it('exists and is nested inside the RequireAuth shell', () => {
    const hit = findRoute(router.routes as RouteNode[], '/settings/profile')
    expect(hit).not.toBeNull()
    expect(hit!.guards).toContain(RequireAuth)
  })

  it('is not gated on any permission — a role-less user must reach it', () => {
    const hit = findRoute(router.routes as RouteNode[], '/settings/profile')!
    const elementType = isValidElement(hit.node.element)
      ? (hit.node.element as ReactElement).type
      : null
    expect(elementType).not.toBe(RequirePermission)
    expect(hit.guards).not.toContain(RequirePermission)
  })

  it('still gates the admin directory on user.read, for contrast', () => {
    const hit = findRoute(router.routes as RouteNode[], '/users')!
    const elementType = (hit.node.element as ReactElement).type
    expect(elementType).toBe(RequirePermission)
  })
})
