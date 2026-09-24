/**
 * Clear chat's two surfaces: the action in the bot chat header and the notice
 * that stands where an archived chat's composer would be.
 *
 * Both are mounted app-wide (titlebar + composer strip) and resolve to "not
 * mine" by returning null, which is how a plugin claims a session it owns
 * without core knowing anything about it.
 */

import { Button, Codicon, host, Tip, useValue } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { isCanonicalChatOnScreen } from './canonical-chat'
import {
  archivedBotChatFor,
  clearBotChat,
  clearChatBlockMessage,
  clearChatBlockReason,
  turnRunningInChat
} from './clear-chat'
import type { ClearChatBlock, ClearChatTarget } from './clear-chat'
import { $lastRoster } from './data'
import { useBots } from './i18n'
import { displayName } from './labels'
import type { RosterRow } from './types'

/** How often the async half of the in-flight probe re-runs while the action is
 *  on screen. The card probe shells out to the bot's kanban CLI, so this is a
 *  compromise: fresh enough that a card landing in the chat flips the button,
 *  slow enough not to spawn a process every few seconds. A turn start/stop
 *  does not wait for it — that half is synchronous off the app's turn flags. */
const PROBE_INTERVAL_MS = 20_000

/** The bot whose canonical chat is the session on screen, if it is one. */
function canonicalBotFor(roster: readonly RosterRow[], storedId: string): null | RosterRow {
  if (!storedId || !Array.isArray(roster)) {
    return null
  }

  return roster.find(bot => isCanonicalChatOnScreen(bot, storedId)) ?? null
}

function errText(error: unknown): string {
  const message = (error as { message?: unknown })?.message

  return typeof message === 'string' ? message : String(error ?? '')
}

/** The action in the bot chat header. Renders only while a bot's canonical
 *  chat is the session on screen — for every other session it returns null, so
 *  the slot stays empty in Sessions mode and in every other profile's chat. */
export function ClearChatAction() {
  const b = useBots()
  const roster: RosterRow[] = useValue($lastRoster)
  const focused = String(useValue(host.state.focusedStoredSessionId) || '')
  // Runtime id of the FOCUSED chat — the id space every session RPC takes.
  // `activeSessionId` is the PRIMARY's runtime, which is a different chat
  // whenever the bot chat is open in a secondary tile.
  const runtimeId = String(useValue(host.state.focusedSessionId) || '')
  // Subscribed so a turn starting or finishing re-renders the button — that
  // half of the probe must not wait for the next interval.
  useValue(host.state.busy)

  const [block, setBlock] = useState<ClearChatBlock | null>(null)
  const [running, setRunning] = useState(false)

  const bot = canonicalBotFor(roster, focused)
  const rowRef = useRef<RosterRow | null>(bot)
  rowRef.current = bot

  const target: ClearChatTarget = {
    resolvedSessionId: bot?.canonical_session?.resolved_id,
    runtimeSessionId: runtimeId,
    storedSessionId: bot?.canonical_session?.id ?? focused
  }

  // Probe on mount and on every identity change (a different chat, a different
  // runtime for the same chat), then on the interval. Deps are primitives: the
  // roster hands out fresh row objects on every refresh, and keying the effect
  // on the row itself would re-probe in a loop.
  const probeKey = bot ? `${bot.name}\u0000${target.storedSessionId}\u0000${runtimeId}` : ''

  useEffect(() => {
    if (!probeKey) {
      return
    }

    let cancelled = false

    const probe = async () => {
      const row = rowRef.current

      if (!row) {
        return
      }

      const next = await clearChatBlockReason(row, {
        resolvedSessionId: row.canonical_session?.resolved_id,
        runtimeSessionId: runtimeId,
        storedSessionId: row.canonical_session?.id ?? focused
      })

      if (!cancelled) {
        setBlock(next)
      }
    }

    void probe()

    const timer = setInterval(() => void probe(), PROBE_INTERVAL_MS)

    return () => {
      cancelled = true
      clearInterval(timer)
      setBlock(null)
    }
  }, [probeKey, focused, runtimeId])

  if (!bot) {
    return null
  }

  // The turn check is synchronous and reactive; the async probe lags behind it
  // by design (it is the expensive half).
  const liveTurn = turnRunningInChat(target)
  const blocked: ClearChatBlock | null = liveTurn ? { code: 'turn', detail: '' } : block
  const reason = clearChatBlockMessage(blocked)

  const onClick = async () => {
    const row = rowRef.current

    if (!row || running || blocked) {
      return
    }

    setRunning(true)

    try {
      // Re-check before acting: the interval's verdict can be a whole tick old,
      // and this is the last moment the four states can be observed.
      const fresh = await clearChatBlockReason(row, {
        resolvedSessionId: row.canonical_session?.resolved_id,
        runtimeSessionId: runtimeId,
        storedSessionId: row.canonical_session?.id ?? focused
      })

      if (fresh) {
        setBlock(fresh)
        host.notify({ kind: 'warning', message: clearChatBlockMessage(fresh) || b.clearChat.failedDetail })

        return
      }

      const name = displayName(row)

      await clearBotChat(row, {
        resolvedSessionId: row.canonical_session?.resolved_id,
        runtimeSessionId: runtimeId,
        storedSessionId: row.canonical_session?.id ?? focused
      })

      host.notify({ kind: 'success', title: b.clearChat.action, message: b.clearChat.cleared(name) })
    } catch (error) {
      host.notify({
        kind: 'error',
        message: errText(error) || b.clearChat.failedDetail,
        title: b.clearChat.failed
      })
    } finally {
      setRunning(false)
    }
  }

  return (
    <Tip label={reason || b.clearChat.action}>
      <span className="inline-flex">
        <Button
          aria-label={b.clearChat.action}
          className="shrink-0 text-(--ui-text-tertiary) hover:text-foreground"
          disabled={running || Boolean(blocked)}
          onClick={() => void onClick()}
          size="icon-titlebar"
          variant="ghost"
        >
          <Codicon aria-hidden name={running ? 'sync' : 'clear-all'} spinning={running} />
        </Button>
      </span>
    </Tip>
  )
}

/** Stands where an archived bot chat's composer would be: the chat can be read,
 *  never continued. The send path refuses the same chat (see the plugin's
 *  composer middleware), so this is the visible half of that promise. */
export function ArchivedChatBanner() {
  const b = useBots()
  const focused = String(useValue(host.state.focusedStoredSessionId) || '')
  const archived = archivedBotChatFor(focused)

  if (!archived) {
    return null
  }

  return (
    <div
      className="mx-1 mb-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs leading-snug text-amber-700 dark:text-amber-200"
      data-slot="bot_chat_archived"
    >
      {b.clearChat.archivedNotice}
    </div>
  )
}
