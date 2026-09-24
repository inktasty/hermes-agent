# A reviewer on a decomposed review card could not return findings (2026-09-22)

## Symptom
Reviewer on `t_c21d6d98` (a review child the auto-decomposer created):
`kanban_request_changes` -> `could not request changes for t_c21d6d98: active run
was not claimed from review`. The reviewer's only remaining option was a block,
which loses the routing of the finding.

## Cause
`kanban_db_graph.decompose_triage_task` creates every child as an ordinary card
(`status='todo'` -> `recompute_ready` -> `ready`), so a review child is claimed by
`claim_task` (no `source_status` on the `claimed` event) and dispatched in the
ready lane. `kanban_db.request_changes` gates the verdict on
`claimed_payload["source_status"] == "review"`, so the reviewer's run was refused.
It also had no `review_requested` event, so the implementer-provenance gate would
have refused the next step anyway.

## Fix
The decomposer is the only component that knows a child verifies another child's
work, so it now says so and the role is durable:
- `kanban_decompose._SYSTEM_PROMPT` / `_clean_children`: optional per-child
  `"role": "review"` (anything else/missing = implementation work).
- `kanban_db_graph._review_child_implementer`: the single distinct assignee of
  the child's non-review parents (all non-review siblings when it declares no
  parents); ambiguous or absent -> `None`.
- `_insert_decomposed_child`: records `{"role": "review", "implementer": ...}` on
  the child's `created` event.
- `kanban_db.request_changes`: a run that was not claimed from `review` is
  accepted only when that marker is present, and the implementer comes from it.
  Every other ready-lane run keeps the exact refusal.

## Verification
- Repro script on an isolated board: before the fix `(False, 'active run was not
  claimed from review')`; after, `(True, 'coder')` and the card lands
  `ready`/`coder`. Unmarked decomposed child and plain card: still refused.
- Two new tests (`test_kanban_review_lifecycle_complete.py`,
  `test_kanban_decompose_db.py`); the verdict test is red on base, the refusal
  test is green on both (it is the guard).
- E2E through the real `decompose_task` path with a stubbed aux LLM: the marker
  survives parsing, and the ready-lane reviewer's verdict routes to the coder.

## Gotchas
- Ambiguity is refused, not guessed: a review child over two different
  implementer profiles records no implementer and `request_changes` returns
  "review handoff has no valid implementer provenance" (same honesty as
  `test_review_handoff_of_card_assigned_to_its_reviewer_records_no_implementer`).
- A worker-created downstream review card (`kanban_create`, no marker) keeps the
  old behavior; it is ordinary implementation work per `sdlc-review`.
