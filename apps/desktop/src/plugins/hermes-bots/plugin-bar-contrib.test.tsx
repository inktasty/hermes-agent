/**
 * The bot-mode plugin's bar-area contributions must be REGISTERED as render
 * items, and this asserts it through the registry the way a slot reads it.
 *
 * The two surfaces are different fields on a contribution:
 *
 *   render  — a component, drawn by a bar area's slot (titlebar, composer strip)
 *   data    — a declarative payload an ENGINE consumes (palette commands,
 *             composer middleware, layout presets, chat-empty states)
 *
 * Both new bot-mode registrations originally nested their component under
 * `data.render`. The registry stored them, `getArea` returned them, the slot
 * skipped every one (it reads the top-level `render`), and so the Clear chat
 * button and the archived-chat banner never appeared in a real app — while the
 * component tests stayed green, because they mounted the component directly and
 * never went through the registry. This test goes through the registry.
 */

import type * as HermesSdk from '@hermes/plugin-sdk'
import type { PluginContext } from '@hermes/plugin-sdk'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import type { Contribution } from '@/contrib/types'

import type * as DataModule from './data'
import type * as RoutingModule from './routing'

const mocks = vi.hoisted(() => ({
  botChatOwnsWorkspace: vi.fn(() => false),
  paneVisibility: vi.fn(),
  sessionOwnsWorkspace: vi.fn(() => false),
  setWorkspaceScope: vi.fn(),
  undismissPane: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const original = await importOriginal<typeof HermesSdk>()

  return {
    ...original,
    host: {
      ...original.host,
      onEvent: undefined,
      paneVisibility: mocks.paneVisibility,
      setWorkspaceScope: mocks.setWorkspaceScope,
      undismissPane: mocks.undismissPane
    }
  }
})

// Boundaries this test does not exercise: clocks, sockets, storage sweeps and
// the panes' own render trees.
vi.mock('./avatar', () => ({ startFaceClock: vi.fn(), stopFaceClock: vi.fn() }))
vi.mock('./relay', () => ({ startBotRelay: vi.fn(), stopBotRelay: vi.fn() }))
vi.mock('./session-sweep', () => ({ startHideSweepScheduler: vi.fn() }))
vi.mock('./canonical-chat', () => ({ openBotCanonicalChat: vi.fn() }))
vi.mock('./chat-empty', () => ({ BotChatEmpty: () => null }))
vi.mock('./hygiene', () => ({ annotateOrphanedGroupChatMembers: () => ({ changed: false, rooms: {} }) }))
vi.mock('./cron', () => ({ bindProfileSync: () => () => undefined, RoutinesPane: () => null }))
vi.mock('./roster-pane', () => ({
  botChatOwnsWorkspace: mocks.botChatOwnsWorkspace,
  BotsPane: () => null,
  releaseStaleOpenBotChat: vi.fn(),
  selectedRosterBot: () => null,
  sessionOwnsWorkspace: mocks.sessionOwnsWorkspace
}))
vi.mock('./group-chat', async () => {
  const { atom: nanoAtom } = await import('nanostores')

  return {
    $groupChats: nanoAtom({}),
    $groupChatWorkspace: nanoAtom(null),
    assignLegacyThreads: (log: unknown[]) => log,
    handleSessionsGatewayTransition: vi.fn(),
    pullGroupChatServerState: async () => false,
    scheduleGroupChatServerSync: vi.fn(),
    setGroupChatSyncDisposed: vi.fn(),
    stopGroupChatServerSync: vi.fn(),
    sweepGroupChatMembersForRemovedConnection: vi.fn(),
    updateGroupChat: vi.fn()
  }
})
vi.mock('./data', async importOriginal => {
  const original = await importOriginal<typeof DataModule>()

  return { ...original, migrateBotMeta: async () => undefined }
})
vi.mock('./routing', async importOriginal => {
  const original = await importOriginal<typeof RoutingModule>()

  return { ...original, setBotsWorkspaceOwner: vi.fn() }
})

const plugin = (await import('./plugin')).default
const { COMPOSER_AREAS, TITLEBAR_AREAS } = await import('@hermes/plugin-sdk')

/** A `PluginContext` that registers into the REAL registry, so the assertions
 *  below read exactly what a slot reads at runtime. */
function forwardingContext() {
  const disposers: (() => void)[] = []

  const ctx = {
    i18n: { register: () => () => undefined, t: (key: string) => key },
    onDispose: (fn: () => void) => disposers.push(fn),
    register: (contribution: Contribution) => {
      const dispose = registry.register(contribution)

      disposers.push(dispose)

      return dispose
    },
    storage: { get: async () => undefined, set: async () => undefined }
  }

  return {
    ctx: ctx as unknown as PluginContext,
    dispose: () => disposers.forEach(fn => fn())
  }
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0))

/** Nanostore stand-ins for the SDK's per-pane visibility stores. */
function paneStores() {
  const stores = new Map<string, ReturnType<typeof atom<boolean>>>()

  mocks.paneVisibility.mockImplementation((id: string) => {
    if (!stores.has(id)) {
      stores.set(id, atom(false))
    }

    return stores.get(id)
  })
}

describe('bot-mode bar contributions', () => {
  let harness: ReturnType<typeof forwardingContext> | null = null

  beforeEach(() => {
    paneStores()
  })

  afterEach(() => {
    harness?.dispose()
    harness = null
  })

  it('registers the Clear chat button as a render item the titlebar slot draws', async () => {
    harness = forwardingContext()
    plugin.register(harness.ctx)
    await settle()

    const entry = registry.getArea(TITLEBAR_AREAS.right).find(c => c.id === 'clear-chat-action')

    expect(entry).toBeDefined()
    // A slot draws `render`; it never looks inside `data`.
    expect(typeof entry?.render).toBe('function')
    expect(entry?.data).toBeUndefined()
  })

  it('registers the archived-chat banner as a render item the composer strip draws', async () => {
    harness = forwardingContext()
    plugin.register(harness.ctx)
    await settle()

    const entry = registry.getArea(COMPOSER_AREAS.top).find(c => c.id === 'archived-chat-banner')

    expect(entry).toBeDefined()
    expect(typeof entry?.render).toBe('function')
    expect(entry?.data).toBeUndefined()
  })
})
