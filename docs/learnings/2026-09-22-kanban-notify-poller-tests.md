# Kanban notify poller tests: patch the OUTER primitive, and the ping is per event id

Test-side notes for the return-path rework (`docs/learnings/2026-09-22-kanban-notify-claim-cursor.md`
covers the production side). Four traps cost time here:

1. Patching a module-global helper recurses. `claim_unseen_events_for_sub` and
   `claim_unseen_events_for_sub_bounded` both call the module-global `unseen_events_for_sub`
   INTERNALLY. Monkeypatching that read to simulate "another consumer took the range" re-enters
   the patch from inside the claim's own `BEGIN IMMEDIATE` and dies with
   `write_txn: already inside a transaction`. Patch the OUTER primitive
   (`claim_unseen_events_for_sub_bounded`) and delegate to the real one after the competing
   claim; a re-entrancy flag on the read works too, but the outer patch is clearer.
2. `rewind_notify_cursor` is no longer on the poller's path (`_notif_rewind_claims` uses
   `force_rewind_notify_cursor`), so stubbing the guarded one proves nothing — that stub now
   silently does nothing. Stub nothing: advance the row with a REAL second claim and let the
   forced rewind do its work. Mutation proof: restoring the guarded rewind fails exactly the two
   concurrent-advance tests ("kanban notification rewind moved nothing").
3. The busy-session ping is deduped per EVENT ID (`last_ping_event_id`, checkpointed with
   `record_notify_ping`), not per poll, so the deterministic test is two direct
   `_notif_poll_kanban` calls with `running=True` and exactly one `_emit`. No threads, no sleeps.
   Mutation proof: a no-op `record_notify_ping` makes exactly that test fail with 2 pings.
4. Style gate: `ruff check` is enforced (the `[tool.ruff.lint]` rules only); `ruff format` is
   NOT — these test files are not format-clean even at HEAD, so running it rewrites the file.

Run the poller tests from the repo root (or `tests/tui_gateway/`) so `tests/conftest.py` applies:
the autouse fixtures sandbox `HERMES_HOME` and the kanban board. A probe run outside them writes
to the real `~/.hermes` board.
