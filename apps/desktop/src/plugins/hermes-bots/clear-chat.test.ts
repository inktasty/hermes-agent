/**
 * Clear chat — archiving a bot's canonical forever-chat and minting a fresh
 * one.
 *
 * The order of writes is the feature, so these tests pin the SEQUENCE, not
 * just the end state:
 *
 *   read the live chat's title (identity) → unhide → rename → roster refresh →
 *   mint → roster refresh → open
 *
 * Each step is load-bearing: core refuses to rename a HIDDEN canonical row, the
 * rename is what frees the UNIQUE(title) slot the fresh chat needs, the mint
 * fails closed while the roster still names a retired `canonical_session`, and
 * the `/new` → `/compact` guard reads the roster's `canonical_session` — so the
 * window between the rename and the second refresh must not be observable.
 */

import type * as HermesSdk from '@hermes/plugin-sdk'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RosterRow } from './types'

const { fetchQuery, notify, openSession, order, overrides, request, stateAtoms } = vi.hoisted(() => {
  const makeAtom = <T>(initial: T) => {
    let value = initial

    return {
      get: () => value,
      listen: () => () => undefined,
      set: (next: T) => {
        value = next
      }
    }
  }

  return {
    fetchQuery: vi.fn(async (): Promise<unknown> => undefined),
    notify: vi.fn(),
    // Typed args: the assertions read `openSession.mock.calls[0][0]`, and an
    // untyped zero-arg mock types its calls as the empty tuple.
    openSession: vi.fn(async (_sessionId: string, _options?: Record<string, unknown>) => undefined),
    order: [] as string[],
    // Read through a Proxy below so a test can make a host verb *absent*.
    overrides: {} as Record<string, unknown>,
    request: vi.fn(),
    stateAtoms: {
      activeSessionId: makeAtom<null | string>('rt-1'),
      busy: makeAtom(false),
      busyBySession: makeAtom<Record<string, boolean>>({}),
      connectionId: makeAtom('local'),
      focusedSessionId: makeAtom<null | string>('rt-1'),
      focusedStoredSessionId: makeAtom<null | string>('stored-1'),
      profile: makeAtom('default')
    }
  }
})

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()

  overrides.notify = notify
  overrides.openSession = openSession
  overrides.request = request
  overrides.state = { ...sdk.host.state, ...stateAtoms }

  return {
    ...sdk,
    host: new Proxy(sdk.host, {
      get: (target, prop) => (prop in overrides ? overrides[prop as string] : Reflect.get(target, prop))
    }),
    queryClient: { fetchQuery }
  }
})

interface RpcCall {
  method: string
  params: Record<string, unknown>
}

let storageSets: Array<{ key: string; value: unknown }> = []

/** How many roster refreshes the current test has answered. */
let rosterFetches = 0

/** A `profiles.list` answer carrying the one bot row: `canonical_session` is
 *  the id the server resolved by NAME, or null when no "Bot Chat" row exists
 *  (which is the state right after the rename and before the mint). */
function rosterSnapshot(canonicalId: null | string) {
  return {
    fetchedAt: Date.now(),
    profiles: [
      {
        canonical_session: canonicalId ? { id: canonicalId, resolved_id: canonicalId } : null,
        name: 'vera'
      }
    ]
  }
}

async function loadModules() {
  vi.resetModules()

  storageSets = []

  const [clearChat, shared] = await Promise.all([import('./clear-chat'), import('./shared')])

  shared.setPluginCtx({
    storage: {
      get: () => null,
      remove: () => undefined,
      set: (key: string, value: unknown) => {
        storageSets.push({ key, value })
      }
    }
  } as unknown as Parameters<typeof shared.setPluginCtx>[0])

  return clearChat
}

/** The canonical-chat fixture: a local bot whose registry row is the chat on
 *  screen (durable id `stored-1`, no compression lineage). */
function botRow(): RosterRow {
  return {
    canonical_session: { id: 'stored-1', resolved_id: 'stored-1' },
    name: 'vera'
  }
}

/** RPC stub for the whole Clear chat flow, recording a readable trace. */
function stubRpc(handlers: Record<string, (params: Record<string, unknown>) => unknown> = {}) {
  request.mockImplementation(async (method: string, params: Record<string, unknown>) => {
    const call: RpcCall = { method, params }

    const custom = handlers[method]

    if (custom) {
      order.push(`${method}:${String(params?.title ?? '')}`)

      return custom(params)
    }

    if (method === 'session.title') {
      order.push(params?.title ? `title:${String(params.session_id)}:${String(params.title)}` : `title-read:${String(params.session_id)}`)

      return params?.title
        ? { session_key: String(params.session_id) === 'rt-1' ? 'stored-1' : 'stored-2', title: params.title }
        : { session_key: 'stored-1', title: 'Bot Chat' }
    }

    if (method === 'session.set_hidden') {
      order.push(`unhide:${String(params.session_id)}`)

      return { hidden: params.hidden }
    }

    if (method === 'session.list') {
      order.push('registry')

      return { sessions: [] }
    }

    if (method === 'session.create') {
      order.push('create')

      return { session_id: 'rt-2', stored_session_id: 'stored-2' }
    }

    if (method === 'cli.exec') {
      const argv = (params?.argv as string[]) || []

      order.push(`cli:${argv.join(' ')}`)

      return { blocked: false, code: 0, output: '[]' }
    }

    order.push(method)

    return {}
  })
}

// Pay the graph's cold transform once, up front — `loadModules` re-imports on
// every test, and charging that cost to whichever test runs first times out
// under a loaded runner.
beforeAll(async () => {
  await loadModules()
}, 60_000)

beforeEach(() => {
  vi.clearAllMocks()
  order.length = 0
  rosterFetches = 0
  stateAtoms.activeSessionId.set('rt-1')
  stateAtoms.busy.set(false)
  stateAtoms.busyBySession.set({})
  stateAtoms.connectionId.set('local')
  stateAtoms.focusedSessionId.set('rt-1')
  stateAtoms.focusedStoredSessionId.set('stored-1')
  stateAtoms.profile.set('default')
  stubRpc()

  // The roster refresh is load-bearing, not decoration: Clear chat VERIFIES
  // that the post-mint snapshot resolves `canonical_session` to the fresh chat,
  // so the default mock answers like a real roster — no "Bot Chat" row before
  // the mint (the rename retired it), the new id from the second refresh on.
  fetchQuery.mockImplementation(async () => {
    rosterFetches += 1
    order.push('roster')

    return rosterSnapshot(rosterFetches === 1 ? null : 'stored-2')
  })
})

describe('clearBotChat', () => {
  it('unhides, renames, refreshes, mints, refreshes and opens — in that order', async () => {
    const clearChat = await loadModules()

    const result = await clearChat.clearBotChat(botRow(), {
      resolvedSessionId: 'stored-1',
      runtimeSessionId: 'rt-1',
      storedSessionId: 'stored-1'
    })

    const trace = [...order]

    // The rename target is derived from the row's own name + a timestamp, so
    // only its prefix is stable.
    const renamed = trace[2]

    expect(renamed.startsWith('title:rt-1:Bot Chat · Vera — archived ')).toBe(true)

    expect(trace.slice(0, 2)).toEqual(['title-read:rt-1', 'unhide:rt-1'])
    // Two registry lookups: the open path's own check, then createCanonicalChat's
    // (it re-consults the registry before minting).
    expect(trace.slice(3)).toEqual([
      'roster',
      'registry',
      'registry',
      'create',
      'title:rt-2:Bot Chat',
      'roster'
    ])

    expect(openSession.mock.calls[0][0]).toBe('stored-2')
    expect(result).toEqual({ archivedId: 'stored-1', openedId: 'stored-2', registryId: 'stored-2' })
  })

  it('unhides before renaming — a hidden canonical row refuses the rename', async () => {
    const clearChat = await loadModules()

    await clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })

    const unhide = order.findIndex(entry => entry.startsWith('unhide:'))
    const rename = order.findIndex(entry => entry.startsWith('title:rt-1:'))

    expect(unhide).toBeGreaterThanOrEqual(0)
    expect(rename).toBeGreaterThan(unhide)
  })

  it('records the archived row so its composer stays suppressed', async () => {
    const clearChat = await loadModules()

    await clearChat.clearBotChat(botRow(), {
      resolvedSessionId: 'stored-1',
      runtimeSessionId: 'rt-1',
      storedSessionId: 'stored-1'
    })

    expect(clearChat.isArchivedBotChat('stored-1')).toBe(true)
    expect(clearChat.archivedBotChatFor('stored-1')?.botName).toBe('vera')
    expect(clearChat.isArchivedBotChat('some-other-chat')).toBe(false)

    const write = storageSets.find(entry => entry.key === 'archived-bot-chats-v1')

    expect(write).toBeTruthy()
    expect(Object.keys(write?.value as Record<string, unknown>)).toEqual(['stored-1'])
  })

  it('never submits a prompt — the reset path is the RPCs, not /new', async () => {
    const clearChat = await loadModules()

    await clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })

    expect(request.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(false)
    expect(request.mock.calls.some(([, params]) => String(params?.text || '').includes('/new'))).toBe(false)
  })

  it('refuses when the runtime session is not the canonical chat', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'session.title': () => ({ session_key: 'stored-9', title: 'Something else' })
    })

    await expect(
      clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })
    ).rejects.toThrow(/Reopen the chat and try again/i)

    // The read, then the claim the stub answers with a foreign title — nothing
    // was written: no unhide, no rename, no mint.
    expect(order).toEqual(['session.title:', 'session.title:Bot Chat'])
    expect(clearChat.isArchivedBotChat('stored-1')).toBe(false)
  })

  it('retries the rename with seconds when the minute stamp is taken', async () => {
    const clearChat = await loadModules()

    let attempts = 0
    const attempted: string[] = []

    stubRpc({
      'session.title': (params: Record<string, unknown>) => {
        if (!params?.title) {
          return { session_key: 'stored-1', title: 'Bot Chat' }
        }

        // Only the canonical row's rename is under test — the mint's own eager
        // title write on the fresh runtime also lands on this RPC.
        if (String(params.session_id) !== 'rt-1') {
          return { session_key: 'stored-2', title: params.title }
        }

        attempts += 1
        attempted.push(String(params.title))

        if (attempts === 1) {
          throw new Error('Title "Bot Chat · Vera" is already in use')
        }

        return { session_key: 'stored-1', title: params.title }
      }
    })

    await clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })

    expect(attempts).toBe(2)
    expect(attempted[0]).toMatch(/archived \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
    expect(attempted[1]).toMatch(/archived \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  })

  it('claims the canonical name onto the live tip before renaming it', async () => {
    // Compressed lineage: the durable root holds "Bot Chat" while the row on
    // screen is the tip, carrying an auto-title. The rename can only land on
    // the live row, so the name has to move onto it first — core transfers it
    // off the hidden ancestor and frees the root's title in the same write.
    const clearChat = await loadModules()

    let claimed: null | string = null

    stubRpc({
      'session.title': (params: Record<string, unknown>) => {
        if (!params?.title) {
          return { session_key: 'stored-tip', title: 'Fixing the parser' }
        }

        // The mint's own eager write on the fresh runtime also carries the
        // canonical name — only the live row's claim is under test here.
        if (params.title === 'Bot Chat' && String(params.session_id) === 'rt-1') {
          claimed = String(params.session_id)

          return { title: 'Bot Chat' }
        }

        return { session_key: String(params.session_id), title: params.title }
      }
    })

    await clearChat.clearBotChat(botRow(), {
      resolvedSessionId: 'stored-tip',
      runtimeSessionId: 'rt-1',
      storedSessionId: 'stored-root'
    })

    expect(claimed).toBe('rt-1')
    expect(order.slice(0, 3)).toEqual(['session.title:', 'session.title:Bot Chat', 'unhide:rt-1'])
    expect(order[3].startsWith('session.title:Bot Chat · Vera — archived ')).toBe(true)
    // The claim is what let the rename free the name, so the fresh chat could
    // take it.
    expect(order.some(entry => entry === 'create')).toBe(true)
    expect(clearChat.isArchivedBotChat('stored-tip')).toBe(true)
    expect(clearChat.isArchivedBotChat('stored-root')).toBe(true)
  })

  it('refuses when a row outside this lineage holds the canonical name', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'session.title': (params: Record<string, unknown>) => {
        if (!params?.title) {
          return { session_key: 'stored-tip', title: 'Fixing the parser' }
        }

        // The claim is rejected (or answered with the foreign title) — either
        // way this chat is not the canonical one.
        throw new Error("Title 'Bot Chat' is already in use by session stored-other")
      }
    })

    await expect(
      clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })
    ).rejects.toThrow(/Reopen the chat and try again/i)

    expect(order.some(entry => entry.startsWith('unhide:'))).toBe(false)
    expect(order.some(entry => entry === 'create')).toBe(false)
  })

  it('refuses when the claim is answered with a different title', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'session.title': (params: Record<string, unknown>) => {
        if (!params?.title) {
          return { session_key: 'stored-tip', title: 'Fixing the parser' }
        }

        // Compare-and-swap lost: the RPC reports the row's CURRENT title.
        return { title: 'Fixing the parser' }
      }
    })

    await expect(
      clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })
    ).rejects.toThrow(/Reopen the chat and try again/i)

    expect(order.some(entry => entry === 'create')).toBe(false)
  })

  it('does not swallow a rename failure that is not a title conflict', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'session.title': (params: Record<string, unknown>) => {
        if (!params?.title) {
          return { session_key: 'stored-1', title: 'Bot Chat' }
        }

        throw new Error('RPC unavailable')
      }
    })

    await expect(
      clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })
    ).rejects.toThrow(/RPC unavailable/)

    expect(order.some(entry => entry === 'create')).toBe(false)
  })

  it('rides out one dropped roster refresh before confirming the new chat', async () => {
    const clearChat = await loadModules()

    // Pre-mint: no canonical row (the rename retired it). Post-mint: the first
    // refresh drops, the retry answers with the fresh chat.
    fetchQuery.mockImplementation(async () => {
      rosterFetches += 1

      if (rosterFetches === 1 || rosterFetches === 2) {
        return rosterFetches === 1 ? rosterSnapshot(null) : null
      }

      return rosterSnapshot('stored-2')
    })

    const result = await clearChat.clearBotChat(botRow(), {
      runtimeSessionId: 'rt-1',
      storedSessionId: 'stored-1'
    })

    expect(result.registryId).toBe('stored-2')
    expect(rosterFetches).toBe(3)
  })

  it('fails instead of reporting success when the roster still names the archived chat', async () => {
    const clearChat = await loadModules()

    // The pre-mint read is clean; every post-mint read still resolves
    // `canonical_session` to the row that was just archived. Reporting success
    // here would leave the `/new` → `/compact` guard pointing at it.
    fetchQuery.mockImplementation(async () => {
      rosterFetches += 1

      return rosterSnapshot(rosterFetches === 1 ? null : 'stored-1')
    })

    await expect(
      clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })
    ).rejects.toThrow(/could not confirm the new/i)

    // The archive and the mint really did happen — only the confirmation
    // failed, and the action says so instead of claiming a clean clear.
    expect(order.some(entry => entry === 'create')).toBe(true)
    expect(clearChat.isArchivedBotChat('stored-1')).toBe(true)
  })

  it('fails when the roster refresh itself fails after the mint', async () => {
    const clearChat = await loadModules()

    fetchQuery.mockRejectedValue(new Error('offline'))

    await expect(
      clearChat.clearBotChat(botRow(), { runtimeSessionId: 'rt-1', storedSessionId: 'stored-1' })
    ).rejects.toThrow(/could not confirm the new/i)

    // A failed refresh must not smuggle a stale pointer into the mint either:
    // the mint ran from the row with the retired `canonical_session` cleared.
    expect(order.some(entry => entry === 'create')).toBe(true)
  })
})

describe('archived chat bookkeeping', () => {
  it('hydrates persisted rows and drops junk', async () => {
    const clearChat = await loadModules()

    clearChat.hydrateArchivedBotChats({
      good: { archivedAt: 123, botKey: 'local::vera', botName: 'vera', title: 'Bot Chat · vera — archived x' },
      junk: 'not an entry',
      nullish: null
    })

    expect(clearChat.isArchivedBotChat('good')).toBe(true)
    expect(clearChat.archivedBotChatFor('good')?.botName).toBe('vera')
    expect(clearChat.isArchivedBotChat('junk')).toBe(false)
    expect(clearChat.isArchivedBotChat('nullish')).toBe(false)
    expect(clearChat.isArchivedBotChat(null)).toBe(false)
  })

  it('ignores a malformed hydrate payload instead of clearing a real archive', async () => {
    const clearChat = await loadModules()

    clearChat.hydrateArchivedBotChats({ good: { archivedAt: 1, botKey: 'k', botName: 'vera', title: 't' } })
    clearChat.hydrateArchivedBotChats('garbage')

    expect(clearChat.isArchivedBotChat('good')).toBe(true)
  })

  it('titles an archived row out of the hide sweep', async () => {
    const clearChat = await loadModules()

    const title = clearChat.archivedBotChatTitle(botRow(), new Date('2026-09-22T19:55:00'))

    expect(title).toBe('Bot Chat · Vera — archived 2026-09-22 19:55')
    // The sweep re-hides VISIBLE rows carrying a Bot Mode plumbing title.
    expect(title).not.toBe('Bot Chat')
    expect(title).not.toBe('Agent Inbox')
    expect(title.startsWith('Group: ')).toBe(false)
  })
})

describe('in-flight work blocks the action', () => {
  it('reports a running turn from the app flags, without an RPC', async () => {
    const clearChat = await loadModules()

    stateAtoms.busy.set(true)

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toEqual({
      code: 'turn',
      detail: ''
    })
    expect(request).not.toHaveBeenCalled()
  })

  it('reports a running turn when only this session is busy', async () => {
    const clearChat = await loadModules()

    stateAtoms.busyBySession.set({ 'rt-1': true })

    expect((await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' }))?.code).toBe('turn')
  })

  it('does not block when the busy turn belongs to a different chat', async () => {
    const clearChat = await loadModules()

    // `host.state.busy` follows the FOCUSED tile — a sibling session running a
    // turn must not disable this bot's action.
    stateAtoms.busy.set(true)
    stateAtoms.busyBySession.set({ 'rt-9': true })
    stateAtoms.focusedSessionId.set('rt-9')

    stubRpc({
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => ({ subagents: [] })
    })

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toBeNull()
  })

  it('does not block when this chat’s own flag says idle', async () => {
    const clearChat = await loadModules()

    // An explicit per-session `false` is authoritative: the global flag is the
    // focused tile's, and this chat is not mid-turn.
    stateAtoms.busy.set(true)
    stateAtoms.busyBySession.set({ 'rt-1': false })

    stubRpc({
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => ({ subagents: [] })
    })

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toBeNull()
  })

  it('reports a live background child', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'subagent.list': () => ({ subagents: [{ status: 'done' }, { status: 'running' }] })
    })

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toEqual({
      code: 'subagent',
      detail: '1'
    })
  })

  it('reports a handed-off background process', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'process.list': () => ({ processes: [{ status: 'exited' }, { status: 'running' }] }),
      'subagent.list': () => ({ subagents: [] })
    })

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toEqual({
      code: 'process',
      detail: '1'
    })
  })

  it('reports a non-terminal card subscribed to this chat', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'cli.exec': (params: Record<string, unknown>) => {
        const argv = (params?.argv as string[]) || []

        if (argv[3] === 'notify-list') {
          return {
            blocked: false,
            code: 0,
            output: JSON.stringify([
              { chat_id: 'stored-1', platform: 'tui', task_id: 't_open' },
              { chat_id: 'other-chat', platform: 'tui', task_id: 't_other' },
              { chat_id: 'stored-1', platform: 'telegram', task_id: 't_telegram' }
            ])
          }
        }

        return { blocked: false, code: 0, output: JSON.stringify({ task: { id: argv[4], status: 'running' } }) }
      },
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => ({ subagents: [] })
    })

    expect(
      await clearChat.clearChatBlockReason(botRow(), {
        resolvedSessionId: 'stored-1',
        runtimeSessionId: 'rt-1',
        storedSessionId: 'stored-1'
      })
    ).toEqual({ code: 'card', detail: '1' })
  })

  it('lets a card that has landed through', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'cli.exec': (params: Record<string, unknown>) => {
        const argv = (params?.argv as string[]) || []

        if (argv[3] === 'notify-list') {
          return {
            blocked: false,
            code: 0,
            output: JSON.stringify([{ chat_id: 'stored-1', platform: 'tui', task_id: 't_done' }])
          }
        }

        return { blocked: false, code: 0, output: JSON.stringify({ task: { status: 'done' } }) }
      },
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => ({ subagents: [] })
    })

    expect(
      await clearChat.clearChatBlockReason(botRow(), {
        resolvedSessionId: 'stored-1',
        runtimeSessionId: 'rt-1',
        storedSessionId: 'stored-1'
      })
    ).toBeNull()
  })

  it('holds the action as unverified when the kanban CLI cannot be read', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'cli.exec': () => ({ blocked: true, code: 0, output: '' }),
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => ({ subagents: [] })
    })

    expect(
      await clearChat.clearChatBlockReason(botRow(), {
        resolvedSessionId: 'stored-1',
        runtimeSessionId: 'rt-1',
        storedSessionId: 'stored-1'
      })
    ).toEqual({ code: 'unknown', detail: '' })
  })

  it('holds the action as unverified when a subscribed card’s status cannot be read', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'cli.exec': (params: Record<string, unknown>) => {
        const argv = (params?.argv as string[]) || []

        if (argv[3] === 'notify-list') {
          return {
            blocked: false,
            code: 0,
            output: JSON.stringify([{ chat_id: 'stored-1', platform: 'tui', task_id: 't_open' }])
          }
        }

        // `kanban show` fails: the card IS addressed to this chat, so an
        // unreadable status is unconfirmed work, never a landed one.
        return { blocked: false, code: 1, output: '' }
      },
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => ({ subagents: [] })
    })

    expect(
      await clearChat.clearChatBlockReason(botRow(), {
        resolvedSessionId: 'stored-1',
        runtimeSessionId: 'rt-1',
        storedSessionId: 'stored-1'
      })
    ).toEqual({ code: 'unknown', detail: '' })
  })

  it('holds the action as unverified when the child probe cannot be read', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => {
        throw new Error('subagent.list unavailable')
      }
    })

    const block = await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })

    expect(block).toEqual({ code: 'unknown', detail: '' })
    expect(clearChat.clearChatBlockMessage(block)).toMatch(/check this bot for work in flight/i)
  })

  it('holds the action as unverified when the process probe cannot be read', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'process.list': () => {
        throw new Error('process.list unavailable')
      },
      'subagent.list': () => ({ subagents: [] })
    })

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toEqual({
      code: 'unknown',
      detail: ''
    })
  })

  it('holds the action as unverified when the subagent probe answers malformed', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'process.list': () => ({ processes: [] }),
      'subagent.list': () => ({})
    })

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toEqual({
      code: 'unknown',
      detail: ''
    })
  })

  it('names the work in flight even when another probe is unreadable', async () => {
    const clearChat = await loadModules()

    stubRpc({
      'process.list': () => ({ processes: [{ status: 'running' }] }),
      'subagent.list': () => {
        throw new Error('subagent.list unavailable')
      }
    })

    expect(await clearChat.clearChatBlockReason(botRow(), { runtimeSessionId: 'rt-1' })).toEqual({
      code: 'process',
      detail: '1'
    })
  })

  it('states the reason in the active locale', async () => {
    const clearChat = await loadModules()

    expect(clearChat.clearChatBlockMessage({ code: 'turn', detail: '' })).toMatch(/turn is running/i)
    expect(clearChat.clearChatBlockMessage({ code: 'subagent', detail: '2' })).toContain('2')
    expect(clearChat.clearChatBlockMessage({ code: 'process', detail: '3' })).toContain('3')
    expect(clearChat.clearChatBlockMessage({ code: 'card', detail: '4' })).toContain('4')
    expect(clearChat.clearChatBlockMessage({ code: 'unknown', detail: '' })).toMatch(
      /check this bot for work in flight/i
    )
    expect(clearChat.clearChatBlockMessage(null)).toBeNull()
  })
})
