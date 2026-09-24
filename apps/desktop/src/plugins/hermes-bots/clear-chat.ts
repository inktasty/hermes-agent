/**
 * Clear chat — archive a bot's canonical forever-chat and mint a fresh one.
 *
 * The canonical Bot Chat is identified by NAME (the profile's row titled
 * exactly "Bot Chat"), so "starting clean" is two writes: rename the current
 * registry row so it becomes an ordinary visible session, then mint a new
 * `Bot Chat`. Nothing here touches the bot's profile, memory, skills or
 * prompt — the conversation is the only thing that moves.
 *
 * Order is load-bearing, and each step exists for a verified reason:
 *
 *  1. `session.title` READ first, so the rename provably lands on the
 *     canonical chat and not on whatever runtime id the shell handed us. If
 *     the live row is a compression TIP and the canonical name still sits on
 *     its hidden ancestor, claim the name onto the tip (core transfers it) —
 *     the rename has to land on the row that holds the name.
 *  2. UNHIDE, then rename. Core refuses to rename a HIDDEN row titled
 *     "Bot Chat" ("its name is its identity") — hidden is the discriminator,
 *     so clearing the flag is what makes the rename legal. The same write is
 *     what makes the archived chat visible in Sessions.
 *  3. Rename frees the UNIQUE(title) slot the fresh chat needs.
 *  4. Roster refresh BEFORE minting: `createCanonicalChat` fails closed when
 *     the registry lookup is empty while the roster still names a
 *     `canonical_session` (the guard that stops a mid-restart lookup from
 *     forking a forever-chat). The roster is stale at exactly that moment.
 *  5. Mint + open the fresh chat (no kickoff turn — it opens empty).
 *  6. Roster refresh AFTER minting, VERIFIED: the roster's `canonical_session`
 *     must resolve to the NEW chat before the action reports success. The
 *     `/new` → `/compact` guard reads that value, so the window between the
 *     two writes must not be observable, and an unconfirmed roster is reported
 *     as a failure rather than a green toast.
 *
 * Clear chat never submits a slash command: it calls the session RPCs
 * directly, which is how it bypasses the `/new`/`/reset` → `/compact`
 * rewrite in plugin.tsx. A `/new` here would fork the relationship into a
 * scratch session instead of minting the canonical replacement.
 *
 * In-flight work blocks the action (see `clearChatBlockReason`). A completion
 * is addressed to a FIXED session key recorded when its card/child/process
 * was created, and renaming does not move it, so a reset while work is
 * running would route every pending outcome into the row the bot has stopped
 * reading. Four states hold it: a turn in this chat, a live background child,
 * a handed-off background process, and a non-terminal card whose
 * subscription names this chat.
 */

import { atom, host } from '@hermes/plugin-sdk'

import { CANONICAL_CHAT_TITLE, openBotCanonicalChat } from './canonical-chat'
import { $lastRoster, botOwner, botRosterKey, botSelectionKey, refreshRoster } from './data'
import type { RosterSnapshot } from './data'
import { botsText } from './i18n'
import { displayName } from './labels'
import { requestForBot } from './routing'
import { getPluginCtx } from './shared'
import type { RosterRow } from './types'

// ── archived bot chats ───────────────────────────────────────────────────────

/** One archived bot chat: the row Clear chat renamed, plus the bot it belonged
 *  to. Persisted, because the composer suppression it drives must survive a
 *  window reload — the row itself is an ordinary visible session with no flag
 *  of its own (core is deliberately untouched). */
export interface ArchivedBotChat {
  archivedAt: number
  /** The bot's roster key (`connectionId::profile`). */
  botKey: string
  botName: string
  title: string
}

const ARCHIVED_BOT_CHATS_KEY = 'archived-bot-chats-v1'

export const $archivedBotChats = atom<Readonly<Record<string, ArchivedBotChat>>>({})

function writeArchivedBotChats(next: Record<string, ArchivedBotChat>) {
  $archivedBotChats.set(next)

  try {
    const storage = getPluginCtx()?.storage

    void Promise.resolve(storage?.set?.(ARCHIVED_BOT_CHATS_KEY, next)).catch(() => undefined)
  } catch {
    /* storage unavailable — the suppression lasts for this window */
  }
}

/** Hydrate the persisted archive map (plugin load). Malformed entries are
 *  dropped rather than trusted — the map gates a composer, so a junk row would
 *  silently freeze an unrelated session. */
export function hydrateArchivedBotChats(stored: unknown): void {
  if (!stored || typeof stored !== 'object' || Array.isArray(stored)) {
    return
  }

  const next: Record<string, ArchivedBotChat> = {}

  for (const [id, value] of Object.entries(stored as Record<string, unknown>)) {
    const entry = value as ArchivedBotChat | null

    if (!id || !entry || typeof entry !== 'object') {
      continue
    }

    next[id] = {
      archivedAt: Number(entry.archivedAt) || 0,
      botKey: String(entry.botKey || ''),
      botName: String(entry.botName || ''),
      title: String(entry.title || '')
    }
  }

  $archivedBotChats.set(next)
}

export function archivedBotChatFor(storedSessionId: null | string | undefined): ArchivedBotChat | null {
  const id = String(storedSessionId || '').trim()

  return id ? $archivedBotChats.get()[id] || null : null
}

/** True when this stored session is a bot chat Clear chat archived. Read by the
 *  composer's send path (and its banner): an archived bot chat can be read,
 *  never continued. */
export function isArchivedBotChat(storedSessionId: null | string | undefined): boolean {
  return archivedBotChatFor(storedSessionId) !== null
}

function rememberArchivedBotChat(storedIds: Array<null | string | undefined>, bot: RosterRow, title: string) {
  const ids = [...new Set(storedIds.map(id => String(id || '').trim()).filter(Boolean))]

  if (!ids.length) {
    return
  }

  const { name } = botOwner(bot)
  const next = { ...$archivedBotChats.get() }

  const entry: ArchivedBotChat = {
    archivedAt: Date.now(),
    botKey: botRosterKey(bot),
    botName: name,
    title
  }

  // Every id that resolves to the archived conversation: the live session key
  // plus the durable registry id and its lineage tip. The Sessions list opens
  // the durable id, the composer's send path may be looking at the tip, and
  // the sweep sees whichever the REST list returns — all of them are the same
  // archived chat.
  for (const id of ids) {
    next[id] = entry
  }

  writeArchivedBotChats(next)
}

// ── the archived row's title ─────────────────────────────────────────────────

/** `YYYY-MM-DD HH:MM` in local time. Seconds are only used when the minute
 *  stamp collides with a row that already holds the title. */
function titleStamp(at: Date, withSeconds = false): string {
  const pad = (value: number) => String(value).padStart(2, '0')

  const date = `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`
  const time = `${pad(at.getHours())}:${pad(at.getMinutes())}${withSeconds ? `:${pad(at.getSeconds())}` : ''}`

  return `${date} ${time}`
}

/** The archived row's title: the bot's name + the archive stamp.
 *
 *  Deliberately NOT `Bot Chat` (nor "Agent Inbox" / a "Group: " prefix): the
 *  hide sweep re-hides every VISIBLE row carrying a Bot Mode plumbing title,
 *  so a renamed row that kept the canonical name would be pushed straight back
 *  out of Sessions. */
export function archivedBotChatTitle(bot: RosterRow, at: Date = new Date(), withSeconds = false): string {
  const name = displayName(bot)

  return botsText().clearChat.archivedRowTitle(name, titleStamp(at, withSeconds))
}

// ── in-flight work ───────────────────────────────────────────────────────────

/** Which of the four in-flight states holds the action — plus `unknown`, the
 *  state where a probe could not be read at all. `unknown` is a block too: the
 *  gate is "every relevant probe confirmed clear", never "nothing objected". */
export type ClearChatBlockCode = 'card' | 'process' | 'subagent' | 'turn' | 'unknown'

export interface ClearChatBlock {
  code: ClearChatBlockCode
  /** A count, as a string (the message interpolator's argument). Empty for
   *  `turn` and `unknown`, which have no count to report. */
  detail: string
}

/** One probe's verdict. `null` is a VERIFIED clear; `'unknown'` means the probe
 *  could not be read, so the action must stay off. The distinction is the whole
 *  safety gate: an unreadable probe that read as "clear" would let Clear chat
 *  archive a chat a completion is still addressed to, and the completion would
 *  land in a row the bot has stopped reading (spec §4C). */
type InFlightVerdict = ClearChatBlock | 'unknown' | null

const UNKNOWN_BLOCK: ClearChatBlock = { code: 'unknown', detail: '' }

/** What the action needs to know about the chat on screen. */
export interface ClearChatTarget {
  /** Live runtime id of the canonical chat (the id every session RPC takes). */
  runtimeSessionId?: null | string
  /** Durable registry id (`canonical_session.id`). */
  storedSessionId?: null | string
  /** Compression-lineage tip (`canonical_session.resolved_id`). */
  resolvedSessionId?: null | string
}

/** The statuses a card can rest in and still have a completion coming. */
const KANBAN_TERMINAL_STATUSES = new Set(['archived', 'done'])

/** Live subagent states (see `subagent.list`'s snapshot fields). */
const LIVE_SUBAGENT_STATUSES = new Set(['queued', 'running'])

/** `cli.exec` runs the kanban CLI on the bot's OWN backend, which is the only
 *  door a plugin has to the board: the kanban REST router is namespace-scoped
 *  to the kanban plugin and no RPC exposes `kanban_notify_subs`. `-p <bot>`
 *  pins the profile explicitly so a backend serving several profiles reads the
 *  bot's own board.
 *
 *  The result is a discriminated verdict, not a nullable value: an unreadable
 *  board (blocked exec, non-zero exit, empty output, non-JSON output) must be
 *  distinguishable from a board that answered "no such subscription", because
 *  only the second one is evidence of clear. */
type CliJson = { ok: true; value: unknown } | { ok: false }

async function kanbanJson(bot: RosterRow, argv: string[]): Promise<CliJson> {
  const { name } = botOwner(bot)

  try {
    const result = await requestForBot<{ blocked?: boolean; code?: number; output?: string }>(
      bot,
      'cli.exec',
      { argv: ['-p', name, 'kanban', ...argv], timeout: 30 },
      { timeoutMs: 40_000 }
    )

    if (result?.blocked || result?.code !== 0) {
      return { ok: false }
    }

    const text = String(result?.output || '').trim()

    if (!text) {
      return { ok: false }
    }

    // A malformed payload throws into the catch below — also unreadable.
    return { ok: true, value: JSON.parse(text) }
  } catch {
    return { ok: false }
  }
}

interface KanbanSubRow {
  chat_id?: unknown
  platform?: unknown
  task_id?: unknown
}

/** A non-terminal card whose subscription names this chat. Matched on the chat
 *  ids the subscription could have recorded: the durable registry id, the
 *  lineage tip, and the live runtime id (a session created but not yet
 *  persisted records its runtime id as the key).
 *
 *  A subscription that exists but whose card status cannot be read is
 *  `'unknown'`, never clear: the card is addressed to this chat, and an
 *  unreadable status is exactly the state that would strand its completion. */
async function probeSubscribedCards(bot: RosterRow, keys: ReadonlySet<string>): Promise<InFlightVerdict> {
  if (!keys.size) {
    return null
  }

  const subs = await kanbanJson(bot, ['notify-list', '--json'])

  if (!subs.ok || !Array.isArray(subs.value)) {
    return 'unknown'
  }

  const mine = (subs.value as KanbanSubRow[]).filter(
    sub => String(sub?.platform || '').toLowerCase() === 'tui' && keys.has(String(sub?.chat_id || ''))
  )

  if (!mine.length) {
    return null
  }

  const open: string[] = []
  let unreadable = 0

  for (const sub of mine) {
    const taskId = String(sub?.task_id || '').trim()

    if (!taskId) {
      unreadable += 1

      continue
    }

    const shown = await kanbanJson(bot, ['show', taskId, '--json'])

    const status = shown.ok
      ? String((shown.value as { task?: { status?: unknown } } | null)?.task?.status || '')
          .trim()
          .toLowerCase()
      : ''

    if (!status) {
      unreadable += 1

      continue
    }

    if (!KANBAN_TERMINAL_STATUSES.has(status)) {
      open.push(taskId)
    }
  }

  if (open.length) {
    return { code: 'card', detail: String(open.length) }
  }

  return unreadable ? 'unknown' : null
}

/** A live background child (delegation) on this chat's session. */
async function probeSubagents(bot: RosterRow, runtimeSessionId: string): Promise<InFlightVerdict> {
  if (!runtimeSessionId) {
    // No runtime id to ask about: this is "cannot verify", not "none".
    return 'unknown'
  }

  try {
    const result = await requestForBot<{ subagents?: Array<{ status?: unknown }> }>(bot, 'subagent.list', {
      session_id: runtimeSessionId
    })

    if (!Array.isArray(result?.subagents)) {
      return 'unknown'
    }

    const live = result.subagents.filter(sub =>
      LIVE_SUBAGENT_STATUSES.has(String(sub?.status || '').trim().toLowerCase())
    )

    return live.length ? { code: 'subagent', detail: String(live.length) } : null
  } catch {
    return 'unknown'
  }
}

/** A handed-off background process (`terminal(background=true)`) on this
 *  chat's session — the registry `process.list` reports, same as the
 *  composer's status stack. */
async function probeProcesses(bot: RosterRow, runtimeSessionId: string): Promise<InFlightVerdict> {
  if (!runtimeSessionId) {
    return 'unknown'
  }

  try {
    const result = await requestForBot<{ processes?: Array<{ status?: unknown }> }>(bot, 'process.list', {
      session_id: runtimeSessionId
    })

    if (!Array.isArray(result?.processes)) {
      return 'unknown'
    }

    const running = result.processes.filter(
      proc => String(proc?.status || '').trim().toLowerCase() !== 'exited'
    )

    return running.length ? { code: 'process', detail: String(running.length) } : null
  } catch {
    return 'unknown'
  }
}

/** A turn running in this chat — the synchronous half of the probe, read
 *  straight off the app's turn flags so the button reacts to a send without
 *  waiting for an RPC.
 *
 *  The per-session flag is authoritative and is consulted FIRST, including when
 *  it says `false`: `host.state.busy` follows the FOCUSED tile, so treating it
 *  as this chat's turn would disable this bot's action because some other chat
 *  is running. The global flag only counts as this chat's turn when this chat
 *  IS the focused one. */
export function turnRunningInChat(target: ClearChatTarget = {}): boolean {
  try {
    const runtime = String(target.runtimeSessionId || '').trim()

    if (!runtime) {
      return false
    }

    const busyBySession = host.state.busyBySession?.get?.() || {}

    if (Object.prototype.hasOwnProperty.call(busyBySession, runtime)) {
      return Boolean(busyBySession[runtime])
    }

    const focused = String(host.state.focusedSessionId?.get?.() || '').trim()

    return Boolean(host.state.busy?.get?.()) && focused === runtime
  } catch {
    return false
  }
}

/** The full in-flight query: the synchronous turn check, then the three async
 *  states. Returns null only when EVERY relevant probe confirmed clear; an
 *  unreadable probe returns the `unknown` block, which holds the action with
 *  its own stated reason. A confirmed busy state always wins over an
 *  unreadable one, because it names the actual work holding the chat. */
export async function clearChatBlockReason(
  bot: RosterRow,
  target: ClearChatTarget = {}
): Promise<ClearChatBlock | null> {
  if (turnRunningInChat(target)) {
    return { code: 'turn', detail: '' }
  }

  const runtime = String(target.runtimeSessionId || '').trim()

  let unverified = false

  const subagent = await probeSubagents(bot, runtime)

  if (subagent && subagent !== 'unknown') {
    return subagent
  }

  unverified = unverified || subagent === 'unknown'

  const process = await probeProcesses(bot, runtime)

  if (process && process !== 'unknown') {
    return process
  }

  unverified = unverified || process === 'unknown'

  const keys = new Set(
    [target.storedSessionId, target.resolvedSessionId, target.runtimeSessionId]
      .map(id => String(id || '').trim())
      .filter(Boolean)
  )

  const card = await probeSubscribedCards(bot, keys)

  if (card && card !== 'unknown') {
    return card
  }

  unverified = unverified || card === 'unknown'

  // Nothing confirmed work in flight, but something could not be read:
  // unverified is not clear, so the action stays off with its reason stated.
  return unverified ? UNKNOWN_BLOCK : null
}

/** The user-facing reason a block holds the action, in the active locale. */
export function clearChatBlockMessage(block: ClearChatBlock | null | undefined): string | null {
  if (!block) {
    return null
  }

  const text = botsText().clearChat
  const count = block.detail || '1'

  switch (block.code) {
    case 'subagent':
      return text.blockedSubagent(count)

    case 'process':
      return text.blockedProcess(count)

    case 'card':
      return text.blockedCard(count)

    case 'unknown':
      // The probe itself failed: say that, rather than implying work in flight.
      return text.blockedUnverified

    default:
      return text.blockedTurn
  }
}

// ── the action ───────────────────────────────────────────────────────────────

export interface ClearChatResult {
  /** The renamed (archived) row's durable id. */
  archivedId: string
  /** The fresh canonical chat's durable registry id. */
  registryId: string
  /** The fresh canonical chat's opened (lineage tip) id. */
  openedId: string
}

/** The bot's canonical ids as the FRESH roster snapshot reports them: the
 *  registry row id and its compression-lineage tip. Empty when the snapshot
 *  carries no row for this bot — which is "unresolved", never "resolves
 *  elsewhere", so callers must not read an empty list as confirmation. */
function canonicalIdsFromRoster(snapshot: RosterSnapshot | null, bot: RosterRow): string[] {
  const profiles = Array.isArray(snapshot?.profiles) ? snapshot.profiles : []
  const key = botSelectionKey(bot)
  const fresh = key ? profiles.find(row => botSelectionKey(row) === key) : undefined

  return [fresh?.canonical_session?.id, fresh?.canonical_session?.resolved_id]
    .map(id => String(id || '').trim())
    .filter(Boolean)
}

/** The bot's roster row as the FRESH snapshot reads — falling back to the
 *  caller's row with the (now retired) `canonical_session` cleared, which is
 *  the truth we just established: the rename freed the registry, so the mint
 *  must not be handed a stale pointer. (A stale pointer is not cosmetic:
 *  `createCanonicalChat` fails closed when the registry lookup comes back empty
 *  while the row still names a `canonical_session`.) */
function rosterRowForMint(bot: RosterRow, snapshot: RosterSnapshot | null): RosterRow {
  const profiles = Array.isArray(snapshot?.profiles) ? snapshot.profiles : []
  const key = botSelectionKey(bot)
  const fresh = key ? profiles.find(row => botSelectionKey(row) === key) : undefined

  return fresh || { ...bot, canonical_session: null }
}

/** Archive the bot's canonical chat and mint + open a fresh one.
 *
 *  Caller contract: the canonical chat is on screen and live (that is where
 *  the action renders), and the in-flight query has already come back clean.
 *  Throws with a user-facing message when the chat cannot be archived. */
export async function clearBotChat(bot: RosterRow, target: ClearChatTarget = {}): Promise<ClearChatResult> {
  const text = botsText().clearChat
  const runtime = String(target.runtimeSessionId || '').trim()

  if (!runtime) {
    throw new Error(text.failedDetail)
  }

  // 1. Identity check + name claim. The rename must land on the canonical
  //    chat: a stale runtime id would rename an unrelated conversation.
  const read = await requestForBot<{ session_key?: string; title?: string }>(bot, 'session.title', {
    session_id: runtime
  })

  const storedId = String(read?.session_key || target.storedSessionId || '').trim()
  let liveTitle = String(read?.title || '').trim()

  if (liveTitle !== CANONICAL_CHAT_TITLE) {
    // The live session is a compression TIP and the canonical name still sits
    // on its hidden ancestor: core leaves the ancestor's title alone when a
    // rotation forks the row, and the gateway re-anchors `session_key` onto the
    // continuation (see `_sync_session_key_after_compress`), so the row on
    // screen is the tip, not the registry row. Claim the name onto the tip
    // FIRST — core's own transfer rule frees the ancestor's title when the tip
    // claims it ("uniqueness + lineage kept") — because the rename below has to
    // land on the row that actually holds the name. Verified, not assumed: the
    // RPC answers with the row's CURRENT title when its compare-and-swap loses,
    // and it raises "already in use" when a row outside this lineage holds the
    // name. Both read as "not this chat" and refuse.
    try {
      const claimed = await requestForBot<{ title?: string }>(bot, 'session.title', {
        session_id: runtime,
        title: CANONICAL_CHAT_TITLE
      })

      liveTitle = String(claimed?.title || '').trim()
    } catch {
      liveTitle = ''
    }

    if (liveTitle !== CANONICAL_CHAT_TITLE) {
      throw new Error(text.failedDetail)
    }
  }

  // 2. Unhide first — core refuses to rename a hidden canonical row.
  await requestForBot(bot, 'session.set_hidden', { session_id: runtime, hidden: false })

  // 3. Rename, freeing the UNIQUE(title) slot. A same-minute collision with an
  //    earlier archive is the one realistic conflict, so retry with seconds.
  const at = new Date()
  let title = archivedBotChatTitle(bot, at)

  try {
    await requestForBot(bot, 'session.title', { session_id: runtime, title })
  } catch (error) {
    const message = String((error as { message?: unknown })?.message || '')

    if (!/already in use/i.test(message)) {
      throw error
    }

    title = archivedBotChatTitle(bot, at, true)
    await requestForBot(bot, 'session.title', { session_id: runtime, title })
  }

  // 4. The archived row is now an ordinary visible session — remember it so
  //    its composer stays suppressed (the plugin's half of "read, never
  //    continued"; core's session list is untouched). Every id that resolves
  //    to the conversation is recorded, because the durable registry id, its
  //    lineage tip and the live session key can all differ.
  rememberArchivedBotChat([storedId, target.storedSessionId, target.resolvedSessionId], bot, title)

  // 5. Refresh the roster before minting: `createCanonicalChat` fails closed
  //    on an empty registry lookup while the roster still names a
  //    canonical_session, and the roster is stale until this refresh lands.
  //    The snapshot is used directly rather than re-read from the cache, so a
  //    FAILED refresh (null) cannot smuggle the stale pointer into the mint.
  const preMint = await refreshRoster()

  const opened = await openBotCanonicalChat(rosterRowForMint(bot, preMint))

  if (!opened) {
    throw new Error(text.failedDetail)
  }

  // 6. Refresh again and VERIFY that the roster's `canonical_session` resolves
  //    to the NEW chat — the `/new` → `/compact` guard reads that value, so
  //    this is load-bearing, not defensive. A refresh that failed, or a
  //    snapshot that still names the archived row, is NOT proof: reporting
  //    success there would leave the guard pointing at the archived chat. One
  //    retry rides out a single dropped refresh; after that the action reports
  //    what actually happened instead of a green toast.
  let confirmed = await refreshRoster()
  let resolvedIds = canonicalIdsFromRoster(confirmed, bot)

  if (!resolvedIds.includes(String(opened.registryId))) {
    confirmed = await refreshRoster()
    resolvedIds = canonicalIdsFromRoster(confirmed, bot)

    if (!resolvedIds.includes(String(opened.registryId))) {
      throw new Error(text.unconfirmed)
    }
  }

  publishFreshCanonicalSession(bot, confirmed)

  return {
    archivedId: storedId,
    openedId: String(opened.openedId),
    registryId: String(opened.registryId)
  }
}

/** Write the VERIFIED fresh row's `canonical_session` into the shared roster
 *  atom without waiting for the Bots pane's publish effect (which only re-runs
 *  when its own snapshot's `sources` subtree changes). The `/new` guard and the
 *  roster previews read this atom, so it is written from the snapshot the
 *  post-mint verification just confirmed — never from a cache read that could
 *  still be the stale row. */
function publishFreshCanonicalSession(bot: RosterRow, snapshot: RosterSnapshot | null) {
  const profiles = Array.isArray(snapshot?.profiles) ? snapshot.profiles : []
  const key = botSelectionKey(bot)
  const fresh = key ? profiles.find(row => botSelectionKey(row) === key) : undefined

  if (!fresh) {
    return
  }

  const current = botSelectionKey(fresh)
  const roster = $lastRoster.get()

  if (!current || !roster.some(row => botSelectionKey(row) === current)) {
    return
  }

  $lastRoster.set(roster.map(row => (botSelectionKey(row) === current ? fresh : row)))
}
