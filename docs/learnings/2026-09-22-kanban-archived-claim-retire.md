# Archived task must not retire an unaccepted report (2026-09-22)

## Symptom
Reviewer reproduced it in an isolated board: an archived task with a pending report,
submit refused -> subscription count 0, no retry on the next tick.

## Cause
`tui_gateway/session_notifications.py::_kb_poll_board` removed the notify sub the
moment the claimed task row looked archived — before the submit. A refused/raised
submit then calls `_notif_rewind_claims` -> `force_rewind_notify_cursor`, which finds
no row ("rewind moved nothing"), and the next tick has no subscription to claim from.
The claim's cursor pair is the only record of the owed report, so deleting the row
while that claim is unaccepted is silent loss.

Also worth knowing: `kanban_db.archive_task` does NOT touch `kanban_notify_subs`
(only `delete_archived_task` does), so the poller is the sole remover — the bug was
entirely in the poller's ordering, not in the archive path.

## Fix
- Claim record gains `archived: True` when the row is archived and a claim was made;
  the sub is left in place.
- Removal happens only for `record is None` (nothing owed) or from the new
  `_notif_retire_archived_claims(claims)`, called by `_notif_poll_kanban_scoped`
  right after `_notif_submit` reports the report accepted (the refusal branch
  returns before it).
- A refusal/raise therefore rewinds a row that still exists; the next tick re-claims.

## Verification
- 4 new tests in `TestArchivedTaskReturnPath` (poller file): refusal + raise retry on
  the next tick with the route kept, split-claim variant, and the no-pending-report
  poll retiring the route at once.
- Mutation check: restoring the old unconditional removal makes exactly the three
  retry/keep tests fail with `kanban notification rewind moved nothing` and
  `_sub_rows() == []` — the reviewer's repro. The no-pending-report test still passes
  (that path is unchanged), which is the expected signature.
- 4 suites green: 75 passed (71 baseline + 4 new); `ruff check` clean.

## Gotcha for future tests
`_cursor(tid)` reads `_sub_rows(tid)[0]`, so after the accepted submit retires the
route it raises IndexError — capture the cursor inside the submit callback when a
test needs both the advanced cursor and the removal.
