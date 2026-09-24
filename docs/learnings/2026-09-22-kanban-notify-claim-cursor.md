# Kanban notify cursor: a claim's new_cursor is max(id of the rows it RETURNED)

`hermes_cli/kanban_db_notify.py` is the durable queue for a task's terminal events,
and `last_event_id` on the subscription row is the only record of what a consumer has
taken. Four things a caller must not miss:

1. `unseen_events_for_sub` / `claim_unseen_events_for_sub` report
   `new_cursor = max(cursor, max(id of the returned rows))`, so a KIND-NARROWED claim
   also advances past unclaimed events of other kinds sitting below that id. With
   warnings suppressed the TUI poller wants one presentation category per turn (a muted
   diagnostic turn must never carry a result), so a plain kind-narrowed claim is unsafe:
   `completed(1), crashed(2), completed(3)` claims `completed` and retires the crash.
2. The fix is cap-and-claim in ONE transaction, not claim-then-rewind:
   `claim_unseen_events_for_sub_bounded(..., kinds=..., max_event_id=end)` — built on
   `unseen_events_for_sub(..., max_event_id=...)`, still a single `BEGIN IMMEDIATE` and
   a single CAS. A two-step bound is a second cursor move over an already-committed
   claim; it can raise or lose its CAS, and it leaves a window in which another consumer
   moves the row between the claim and the bound. Atomic, the returned cursor is always
   the value the row holds, so it is always the value a later rewind CASes against.
3. `rewind_notify_cursor` is CAS-guarded on the claim's own value and therefore DECLINES
   once another consumer advanced the row — correct for a notifier that must not clobber
   newer progress, wrong for a claim whose events were never sent: the advance retires
   them behind a cursor no later tick reads past. `force_rewind_notify_cursor` reads the
   row's current cursor inside the same transaction and CASes against that, so the
   undelivered range `(old_cursor, claimed_cursor]` is claimable again. Re-delivering a
   range a concurrent consumer claimed in between is the accepted price: at-least-once
   over silent loss. The poller uses the forced one; the gateway notifier keeps the
   guarded one.
4. Invariant to hold at every call site: the cursor never advances past an event the
   session has not accepted. After a refused or raised submit, every unaccepted event
   must still be claimable on the next tick. The in-memory buffer is gone; the unclaimed
   cursor is the buffer.

Regression shape that has teeth: advance the row past the claim (a real second claim on
the same subscription row, not a stubbed `False`), then refuse/raise the submit, and
assert the cursor returns to its pre-claim value and the next tick re-delivers the
report. With the guarded rewind that test fails with the cursor stuck at the
concurrent consumer's value.

Test-side trap: `monkeypatch.undo()` in this repo also reverts the root
`tests/conftest.py` autouse fixtures (`_hermetic_environment`, `_kanban_write_guard`)
because they share the function-scoped `monkeypatch` — restore only your own `setattr`
mid-test, or the test silently runs against a different kanban DB.
