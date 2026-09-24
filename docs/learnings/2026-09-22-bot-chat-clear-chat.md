# Bot Mode "Clear chat": the core gates a rename has to clear

Clear chat archives a bot's canonical `Bot Chat` and mints a fresh one. The
bot chat is identified by NAME, so "start clean" is a rename + a mint — and
four core gates decide whether that is even legal. Each was found by reading
`hermes_state_titles.py`, `hermes_state_sessions.py` and the plugin's
`canonical-chat.ts`, not by trial and error in the UI:

1. `_set_session_title` REFUSES to rename a HIDDEN row whose title is exactly
   `Bot Chat` ("its name is its identity"). Unhide first, then rename. The
   unhide is also what makes the archived row visible in Sessions.
2. `title` is UNIQUE per profile. The rename is what frees the slot the fresh
   chat needs; minting first would be rejected as "already in use". A
   same-minute collision is the realistic conflict, so the archive title
   carries minutes and retries with seconds on that error only.
3. `createCanonicalChat` FAILS CLOSED when the registry lookup returns zero
   rows while the roster still names a `canonical_session` (the guard against
   forking a forever-chat after a mid-restart empty lookup). The roster is
   stale at exactly that moment, so a forced refresh must land BEFORE the mint
   — `primeRoster()`'s 5 s `staleTime` is not enough, hence `refreshRoster()`.
4. The hide sweep re-hides every VISIBLE row carrying a plumbing title
   (`Bot Chat`, `Agent Inbox`, `Group: `). An archived row that kept the
   canonical name would be pushed straight back out of Sessions, so the
   archive title must be neither. An id-based skip is the backstop for the
   lineage case where the durable root still carries the plumbing title while
   the renamed tip carries the stamp.
5. `session.title` is session-scoped (`@_with_db(session_scoped=True)`) and
   writes `session["session_key"]`, which compression re-anchors onto the
   continuation row (`_sync_session_key_after_compress`). So the live row is
   the lineage TIP, while `get_session_by_title("Bot Chat")` can still return
   the hidden ROOT — and the rename can only ever land on the tip. Writing the
   canonical name onto the tip first is the sanctioned fix: core's conflict
   branch transfers it off the hidden compression ancestor ("uniqueness +
   lineage kept") and nulls the ancestor's title. Verified against a real
   state.db: root title `Bot Chat` → tip claims → root `None`, tip `Bot Chat`.
   Without this step a compressed bot chat (the common case) can never be
   archived, because the row on screen does not hold the name.

Also load-bearing: the `/new` → `/compact` guard reads the roster's
`canonical_session`, so the roster must be refreshed AGAIN after the mint —
otherwise the guard points at the archived row for a whole poll cycle.

Clear chat never submits a slash command: it calls `session.title` /
`session.set_hidden` directly. Submitting `/new` would hit the guard and fork
a scratch session instead of minting the canonical replacement.

In-flight work blocks the action because a completion is addressed to a FIXED
session key recorded when its card/child/process was created, and renaming does
not move it. The kanban half has no RPC (`kanban_notify_subs` is not exposed
and the REST router is namespace-scoped to the kanban plugin), so the probe
shells out through `cli.exec` to `hermes -p <bot> kanban notify-list --json`
plus `kanban show <id> --json`.

## Review round 2: three ways the safety gate can lie

1. A probe that FAILS OPEN is worse than no probe. Every async probe
   (`subagent.list`, `process.list`, the kanban CLI, an unreadable card status)
   first returned "clear" on failure, so the action could archive a chat a
   completion was still addressed to — the exact failure the gate exists to
   prevent. Each probe now returns a three-way verdict: block / `'unknown'` /
   verified clear, and only verified clear passes. A confirmed busy state still
   wins over an `'unknown'` one, so the disabled reason names the real work.
2. "The refresh ran" is not "the refresh landed". `refreshRoster()` swallowed
   its failure, so the post-mint step could publish a STALE cached row and
   report success while `canonical_session` still named the archived chat. It
   now returns the snapshot (or null) and Clear chat verifies the post-mint
   snapshot resolves to the minted id before returning — one retry, then a
   stated failure. The same snapshot feeds the mint, so a failed refresh cannot
   smuggle the retired pointer past `createCanonicalChat`'s fail-closed guard.
3. `host.state.busy` is the FOCUSED tile's flag, not this chat's. Reading it
   before `busyBySession[runtime]` disabled the action because a sibling session
   was mid-turn. The per-session flag is authoritative now (including an
   explicit `false`); the global one counts only when the target IS focused.
