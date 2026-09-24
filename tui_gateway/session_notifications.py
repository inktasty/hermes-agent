"""Per-session notification poller: kanban/loop/delegation events routed to the owning session,
desktop UI wiring, HUD surface note. Bodies are rebound onto server.py's globals at install time
(method_ctx.bind_module), so they reference server.py globals bare."""

from __future__ import annotations

import contextlib

from .method_ctx import bind_module


def _notif_locked_sessions(fn, default):
    """Run ``fn(_sessions)`` under ``_sessions_lock``; ``default`` on failure (poller must never crash)."""
    try:
        with _sessions_lock:
            return fn(_sessions)
    except Exception:
        return default


def _notif_current_keys(sid: str, session: dict) -> set:
    return {str(session.get("session_key") or ""), _session_lookup_key(session, fallback=sid)}


def _notif_session_matches(s: dict, keys) -> bool:
    return str(s.get("session_key") or "") in keys or _session_lookup_key(s, fallback="") in keys


def _notif_live_session_matches(keys, exclude: dict | None = None) -> bool:
    """Any non-finalized live session (other than ``exclude``) matches ``keys``; False if the registry can't be read
    (fail open rather than drop the event)."""
    return _notif_locked_sessions(
        lambda ss: any(s is not exclude and not s.get("_finalized") and _notif_session_matches(s, keys)
                       for s in ss.values()),
        False)


def _notif_resolve_event_key(evt_key: str, session: dict | None = None) -> str:
    """Resolve a compression-rotated session key to its continuation tip (or itself). Looked up in
    ``session``'s own store: a named-profile session's lineage lives in ``profiles/<x>/state.db``,
    where the launch handle cannot see it."""
    try:
        with _session_db(session or {}) as db:
            return (db.resolve_resume_session_id(evt_key) if db is not None else evt_key) or evt_key
    except Exception:
        return evt_key


def _notification_event_belongs_elsewhere(sid: str, session: dict, evt: dict) -> bool:
    """True if ``evt`` is owned by a *different* live session. Background completions carry the ``session_key`` of the
    session that started the work; async delegation completions also carry ``origin_ui_session_id`` (the live TUI tab)."""
    evt_ui_sid = str(evt.get("origin_ui_session_id") or "")
    if evt_ui_sid:
        if evt_ui_sid == str(sid or "") and not session.get("_finalized"):
            return False
        if _notif_locked_sessions(lambda ss: evt_ui_sid in ss and not ss[evt_ui_sid].get("_finalized"), False):
            return True
        # Exact UI tab gone: fall through to durable session_key routing so a resumed continuation with the same
        # key/lineage can still claim it.
    evt_key = str(evt.get("session_key") or "")
    if not evt_key:
        return False
    current_keys = _notif_current_keys(sid, session)
    # Compression can rotate AIAgent.session_id while the detached child is still running: map the event's original
    # key to its continuation tip so it reaches the live session instead of becoming an orphan any poller may consume.
    # A live continuation wins over the compressed parent, else a stale parent tab could consume the event first.
    resolved_key = _notif_resolve_event_key(evt_key, session)
    if resolved_key != evt_key:
        if resolved_key in current_keys:
            return False
        if _notif_live_session_matches({resolved_key}):
            return True
    if evt_key in current_keys:
        return False
    if resolved_key == evt_key and _notif_other_profile_session_owns(sid, session, evt):
        return True
    return _notif_live_session_matches({evt_key, resolved_key}, exclude=session)


def _notif_other_profile_session_owns(sid: str, session: dict, evt: dict) -> bool:
    """True when a live session on ANOTHER profile store provably owns ``evt`` (its compression lineage
    resolves there). Every poller drains one process-wide queue, but lineage is looked up in the
    dequeuer's own store; without this, profile B dequeuing an event keyed on profile A's compressed
    parent found no owner anywhere and dropped it for good. Snapshot under the lock, resolve outside it."""
    own_home = str(session.get("profile_home") or "")
    candidates = _notif_locked_sessions(
        lambda ss: [(other_sid, other) for other_sid, other in ss.items()
                    if other is not session and not other.get("_finalized")
                    and str(other.get("profile_home") or "") != own_home],
        [])
    return any(_session_owns_notification_event(other_sid, other, evt) for other_sid, other in candidates)


def _session_owns_notification_event(sid: str, session: dict, evt: dict) -> bool:
    """True iff *this* session PROVABLY owns ``evt`` (UI origin is this live session, or ``session_key`` raw/compression-
    resolved matches) — the fail-closed gate for addressed notifications, without the orphan-adoption fallback."""
    if session.get("_finalized"):
        return False
    if str(evt.get("origin_ui_session_id") or "") == str(sid or ""):
        return True
    evt_key = str(evt.get("session_key") or "")
    current_keys = _notif_current_keys(sid, session)
    return bool(evt_key) and (evt_key in current_keys or _notif_resolve_event_key(evt_key, session) in current_keys)


def _notification_event_requires_owner(evt: dict) -> bool:
    """Whether ``evt`` must be positively claimed before TUI delivery."""
    return evt.get("type") == "async_delegation" or bool(evt.get("origin_ui_session_id") or evt.get("session_key"))


# Extra dedup fields per event type. Completions are terminal (one-shot per process session); watch events are not —
# one process can match patterns many times, so their content is part of the key.
_DEDUP_EXTRA_FIELDS = {
    "watch_match": ("command", "pattern", "output", "suppressed", "message_id"),
    "heartbeat": ("seq",),
    "watch_disabled": ("command", "message", "suppressed"),
    "watch_overflow_": ("command", "message", "suppressed"),  # prefix match
}


def _notification_event_dedup_key(evt: dict) -> tuple:
    """UI-emission identity for a process notification event."""
    evt_type = evt.get("type", "completion")
    if evt_type == "async_delegation":
        # No process session_id: else every completion keys as ("", "async_delegation") and the second is suppressed forever.
        # An early per-task failure notice must not collapse with the batch's final result (nor with a sibling's notice).
        if evt.get("task_failure_notice"):
            task_idx = ((evt.get("results") or [{}])[0] or {}).get("task_index", "")
            return (evt.get("delegation_id", ""), evt_type, "task_failure", task_idx)
        return (evt.get("delegation_id", ""), evt_type)
    extra = _DEDUP_EXTRA_FIELDS.get("watch_overflow_" if evt_type.startswith("watch_overflow_") else evt_type, ())
    return (evt.get("session_id", ""), evt_type, *(evt.get(f, 0 if f == "suppressed" else "") for f in extra))


# Deliverable kinds only: a silent kind (archived/unblocked) has no formatter, and a claim moves the cursor to the
# highest id it returns, so claiming one would only skip an event no turn ever carries.
_KANBAN_NOTIFY_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status")
# kanban, /loop + /heartbeat and the bot mailbox share one idle-poll cadence; probing the lease registry on
# every 0.5s queue timeout cost ~a core at 11 sessions (#108005).
_KANBAN_POLL_SECONDS = _LOOP_POLL_SECONDS = _BOT_DELIVERY_POLL_SECONDS = 5.0


def _notif_release_turn(session: dict) -> None:
    with session["history_lock"]:
        session["running"] = False


def _notif_claim_turn(session: dict) -> bool:
    """Claim the idle session (running=True) under history_lock; False if a turn is live."""
    with _session_turn_admission(session) as admitted:
        if not admitted or session.get("running"):
            return False
        session["running"] = True
        return True


def _notif_log_failure(what: str, exc: BaseException) -> None:
    print(f"[tui_gateway] {what}: {type(exc).__name__}: {exc}", file=sys.stderr)


def _notif_submit(rid: str, sid: str, session: dict, text: str, what: str, **kwargs) -> bool:
    """``_run_prompt_submit`` for a claimed (running=True) turn; releases on failure.

    Returns ``_run_prompt_submit``'s flag: a refusal is a delivery that never happened (it releases the turn
    itself), so callers holding a claim must treat False like a raise and put the claim back.

    The turn-start frame is NOT emitted here: ``_run_prompt_submit`` emits it after the turn is admitted.
    A frame sent before admission is what leaves a refused delivery showing "the agent is working" for
    good, since the client latches busy on that frame and no turn runs to send its end.
    """
    try:
        with _session_profile_runtime_scope(session):
            return bool(_run_prompt_submit(rid, sid, session, text, **kwargs))
    except Exception as exc:
        _notif_log_failure(what, exc)
        _notif_release_turn(session)
        raise


def _notif_loop_status(sid: str, text: str) -> None:
    _emit("status.update", sid, {"kind": "loop", "text": text})


def _notif_slash_loop_tick(rid: str, sid: str, session: dict, mgr, wakeup: str) -> None:
    """Slash-command /loop wakeup: route through the slash pipeline, not the model. No model reply to evaluate, so the
    tick completes immediately — unless the command resolves to a prompt (skill command etc.), which runs as a normal
    turn whose post-turn hook completes the tick."""
    _notif_release_turn(session)
    try:
        parts = wakeup.lstrip()[1:].split(None, 1)
        resp = _methods["command.dispatch"](
            rid, {"name": parts[0] if parts else "", "arg": parts[1] if len(parts) > 1 else "", "session_id": sid})
        payload = (resp or {}).get("result") or {}
        if out := str(payload.get("output") or "").strip():
            _notif_loop_status(sid, out)
        if payload.get("type") == "send" and payload.get("message"):
            if not _notif_claim_turn(session):
                mgr.abandon_tick()
                return
            # Releases the claim on failure: the swallow below would otherwise leave the session busy for good.
            if _notif_submit(rid, sid, session, payload["message"], "loop wakeup send failed") is False:
                # A refusal ran no turn: the tick stays due and the claim goes back.
                _notif_release_turn(session)
                with contextlib.suppress(Exception):
                    mgr.abandon_tick()
            return
    except Exception:
        pass
    decision = mgr.complete_tick("")
    if decision.get("message"):
        _notif_loop_status(sid, decision["message"])


def _notif_gateway_owns_heartbeat(session: dict, session_key: str) -> bool:
    """Whether the gateway's heartbeat poller owns this session's due tick.

    Desktop/TUI can attach to a messaging conversation, but its session-owner poller has no adapter
    route for the reply. Ownership is the gateway's LIVE routing index, not the row's immutable
    ``source``: ``gateway/run_heartbeat_restore.py`` only registers watches for a non-suspended,
    origin-bearing key whose current ``session_id`` is this one, so a row archived by /reset,
    auto-reset or compression rotation belongs to nobody there and must keep firing here. The index
    lives in the gateway's home store (the launch handle for a multiplexed gateway, the profile's
    own store for a per-profile gateway), so both are consulted; no entry is fail-open.
    """
    try:
        with _session_db(session) as db:
            for store in {id(d): d for d in (db, _get_db()) if d is not None}.values():
                if (entry := store.gateway_routing_entry_for_session(session_key)) is not None:
                    return bool(entry.get("origin")) and not entry.get("suspended")
        return False
    except Exception:
        return False


def _maybe_fire_tui_heartbeat_tick(sid: str, session: dict) -> None:
    """Fire a due /heartbeat prompt for an idle TUI/Desktop/dashboard session (#102056, #103044).

    ``/heartbeat`` runs in the slash worker, whose CLI watchdog queues the due prompt into a
    ``_pending_input`` no turn loop ever drains — armed-but-dead. State is durable in SessionDB, so
    the session-owner process drives firing exactly like ``_maybe_fire_tui_loop_tick``: claim the
    idle session first (a racing user prompt wins), then re-enter through ``_run_prompt_submit`` as a
    plain user turn. A dispatch that never starts a turn rewinds the persisted fire so the tick stays
    due instead of being silently consumed.
    """
    try:
        from hermes_cli.heartbeat import HeartbeatManager
    except Exception:
        return
    if not (sid_key := session.get("session_key") or ""):
        return
    mgr = HeartbeatManager(session_id=sid_key)
    if not mgr.is_active() or not mgr.state.is_due() or _notif_gateway_owns_heartbeat(session, sid_key):
        return  # not due, or the gateway poller owns the routed conversation — stays due there
    if not _notif_claim_turn(session):
        return  # busy — the tick coalesces to the next idle poll
    if not (prompt := mgr.due_prompt()):
        _notif_release_turn(session)
        return
    started = False
    try:
        _emit("status.update", sid, {"kind": "heartbeat", "text": f"♥ heartbeat #{mgr.state.fire_count} firing…"})
        started = bool(_run_prompt_submit(f"__heartbeat__{int(time.time() * 1000)}", sid, session, prompt))
    except Exception as exc:
        _notif_log_failure("heartbeat dispatch failed", exc)
    if not started:
        # _run_prompt_submit releases ``running`` itself when it refuses the turn; make it unconditional.
        _notif_release_turn(session)
        with contextlib.suppress(Exception):
            mgr.abandon_fire()


def _loop_route_is_gateway_chat(state) -> bool:
    """A /loop set from a messaging chat carries the gateway's ``route`` (platform + chat_id); its wakeup scanner
    (``gateway/run_goals.py::_loop_wakeup_fire_one``) fires those and skips route-less CLI/TUI loops. Mirror it here
    so a Desktop viewer of the same session never consumes the tick and strands the reply off the chat."""
    route = getattr(state, "route", None) or {}
    return bool(route.get("platform") and route.get("chat_id"))


def _maybe_fire_tui_loop_tick(sid: str, session: dict) -> None:
    """Fire a due /loop wakeup for an idle TUI/Desktop/dashboard session (per-session poller, coarse cadence). Claims
    the session (running=True) before dispatching so a racing user prompt wins; the post-turn hook completes the tick."""
    try:
        from hermes_cli.loops import LoopManager, goal_blocks_loop_tick
    except Exception:
        return
    if not (sid_key := session.get("session_key") or ""):
        return
    mgr = LoopManager(session_id=sid_key)
    if not mgr.is_due() or goal_blocks_loop_tick(sid_key) or _loop_route_is_gateway_chat(mgr.state):
        return  # not due, or the gateway's wakeup scanner owns the routed chat — stays due there
    if not _notif_claim_turn(session):
        return  # busy — stays due, next poll retries
    if not (wakeup := mgr.fire_tick()):
        _notif_release_turn(session)
        return
    rid = f"__loop__{int(time.time() * 1000)}"
    try:
        _notif_loop_status(sid, f"↻ /loop wakeup #{mgr.state.ticks_fired if mgr.state else '?'} firing…")
        if wakeup.lstrip().startswith("/"):
            _notif_slash_loop_tick(rid, sid, session, mgr, wakeup)
        else:
            # No turn-start frame before admission: _run_prompt_submit emits it once the turn is
            # admitted, so a refused wakeup cannot leave the client showing work that never runs.
            if _run_prompt_submit(rid, sid, session, wakeup) is False:
                _notif_release_turn(session)
                with contextlib.suppress(Exception):
                    mgr.abandon_tick()
    except Exception as exc:
        _notif_log_failure("loop wakeup dispatch failed", exc)
        _notif_release_turn(session)
        with contextlib.suppress(Exception):
            mgr.abandon_tick()


def _kb_first_line(value: Any, limit: int) -> str:
    lines = str(value).strip().splitlines()
    return f"\n{lines[0][:limit]}" if lines else ""


def _kb_completed(task, payload: dict, title: str) -> str:
    handoff = (_kb_first_line(payload["summary"], 200) if payload.get("summary")
               else _kb_first_line(task.result, 160) if getattr(task, "result", None) else "")
    return f" done — {title}{handoff}"


def _kb_timed_out(task, payload: dict, title: str) -> str:
    with contextlib.suppress(TypeError, ValueError):
        return f" timed out (max_runtime={int(payload.get('limit_seconds') or 0)}s); will retry"
    return " timed out (max_runtime=0s); will retry"


# kind -> (glyph, suffix after "Kanban <id>"); silent kinds (archived/unblocked) are absent → None.
_KANBAN_EVENT_FORMATTERS = {
    "completed": ("✔", _kb_completed),
    "blocked": ("⏸", lambda t, p, title: " blocked" + (f": {str(p.get('reason'))[:160]}" if p.get("reason") else "")),
    "gave_up": ("✖", lambda t, p, title: " gave up after repeated spawn failures"
                + (f"\n{str(p.get('error'))[:200]}" if p.get("error") else "")),
    "crashed": ("✖", lambda t, p, title: " worker crashed (pid gone); dispatcher will retry"),
    "timed_out": ("⏱", _kb_timed_out),
    "status": ("🔄", lambda t, p, title: f" → {p.get('status') or ''}"),
}


def _format_kanban_event_text(sub: dict, task, ev, board_slug: str) -> Optional[str]:
    """Single-line notification text for one kanban event; wording mirrors gateway/kanban_watchers.py (reads the same
    as on Telegram). None for silent kinds."""
    if (entry := _KANBAN_EVENT_FORMATTERS.get(getattr(ev, "kind", ""))) is None:
        return None
    glyph, fmt = entry
    task_id = sub.get("task_id", "")
    title = (getattr(task, "title", None) or task_id)[:120]
    who = getattr(task, "assignee", None) or ""
    prefix = f"{glyph} " + (f"[{board_slug}] " if board_slug else "") + (f"@{who} " if who else "")
    return f"{prefix}Kanban {task_id}{fmt(task, getattr(ev, 'payload', None) or {}, title)}"


def _kb_board_key(_kb, board_meta) -> tuple[str, str]:
    """(slug, resolved DB identity) — multiple slugs can point at one DB when HERMES_KANBAN_DB pins it."""
    slug = (board_meta or {}).get("slug") or _kb.DEFAULT_BOARD
    db_path = (board_meta or {}).get("db_path")
    try:
        return slug, str(Path(db_path).expanduser().resolve() if db_path else _kb.kanban_db_path(slug).resolve())
    except Exception:
        return slug, f"slug:{slug}"


def _kb_sub_ident(sub: dict) -> dict:
    """The four columns that identify one subscription row."""
    return dict(task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "")


def _kb_event_is_diagnostic(ev) -> bool:
    """Infrastructure attention (a crash, a timeout) vs. a task-status change, per the gateway's classifier."""
    from gateway.kanban_watchers_notifier import diagnostic_event
    return bool(diagnostic_event(ev))


def _kb_claim_for_turn(_kbn, conn, sub_ident: dict, *, split: bool) -> tuple[int, int, list]:
    """Claim the unseen events the turn this poll will deliver carries, as ``(old_cursor, new_cursor, events)``.

    Without the split that is one atomic claim of every deliverable kind. With it the turn carries a single
    presentation category — a diagnostic turn is muted when ``suppress_warning_notifications`` is on, so folding a
    result into one would hide the report — and only the contiguous run of that category is claimed, capped at the
    run's last event id in the same transaction that moves the cursor
    (``claim_unseen_events_for_sub_bounded``).

    The cap is what makes a narrowed claim safe. A kind-filtered claim reports ``max(id of the rows returned)``, so
    the same kinds reappearing after the other category intervened would ride the cursor out of the stream unread;
    the cap stops the claim at the run instead. Because the cap is part of the claim, there is no second cursor
    move to confirm and no window for another consumer to move the row out from under one: ``new_cursor`` is always
    the cursor the row holds afterwards, which is the value every later rewind CASes against. An event the claim
    did not return is never retired, so a refused or raised turn can always put its claim back for the next tick.
    """
    if not split:
        return _kbn.claim_unseen_events_for_sub(conn, kinds=_KANBAN_NOTIFY_KINDS, **sub_ident)
    _cursor, unseen = _kbn.unseen_events_for_sub(conn, kinds=_KANBAN_NOTIFY_KINDS, **sub_ident)
    if not unseen:
        return 0, 0, []
    want_diagnostic = _kb_event_is_diagnostic(unseen[0])
    run: list = []
    for ev in unseen:
        if _kb_event_is_diagnostic(ev) != want_diagnostic:
            break
        run.append(ev)
    # ``unseen`` is id-ordered and the run is its contiguous prefix, so every unseen event at or below the cap is in
    # the run: the kind filter plus the cap returns exactly the run, and nothing above it is retired.
    return _kbn.claim_unseen_events_for_sub_bounded(
        conn, kinds=sorted({ev.kind for ev in run}), max_event_id=int(run[-1].id), **sub_ident)


def _kb_poll_board(sid: str, _kb, slug: str, session_key: str, *, claim: bool, split: bool, ping,
                   out: Optional[list] = None) -> list[dict]:
    """One board's share of a poll: claim + format this session's unseen events, or (``claim=False``) read them
    without advancing anything and render only the status lines that have not pinged yet.

    One poller per live session: the board is not opened writable unless it has a subscription owned by this exact
    session (a failed read-only probe — locked/corrupt DB — falls through so delivery is preserved). ``ping`` is
    None when the caller wants the texts alone; otherwise it renders one line and returns whether it was visible.
    Returned records carry the texts plus the cursor pair the caller needs to rewind a claim nothing delivered; a
    record whose task is already archived additionally carries ``archived``, the flag that entitles the caller —
    once, and only once, the submit is accepted — to retire the subscription.

    A claim is registered in ``out`` the moment its cursor moves — before the task row is read and before any text
    is formatted — because from that instant the only record of the owed report is the claim's own cursor pair.
    Anything raising after it (task lookup, a formatter, a later board) must leave the board's committed claims
    visible to a rewind, so the caller passes one accumulator across every board and rewinds it on the way out.
    """
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    from gateway.warning_notifications import DiagnosticText
    claims: list = [] if out is None else out
    with contextlib.suppress(Exception):
        if _kbn.count_notify_subs(board=slug, platform="tui", chat_id=session_key) == 0:
            return claims
    try:
        conn = _kbc.connect(board=slug)
    except Exception:
        return claims
    with contextlib.closing(conn):
        try:
            subs = _kbn.list_notify_subs(conn)
        except Exception:
            return claims
        for sub in subs:
            if (sub.get("platform") or "").lower() != "tui" or sub.get("chat_id") != session_key:
                continue
            sub_ident = _kb_sub_ident(sub)
            record: Optional[dict] = None
            if claim:
                old_cursor, new_cursor, events = _kb_claim_for_turn(_kbn, conn, sub_ident, split=split)
                if new_cursor > old_cursor:
                    record = {"slug": slug, "sub": sub_ident, "old_cursor": old_cursor,
                              "new_cursor": new_cursor, "texts": []}
                    claims.append(record)
                # The task row is read even when nothing was claimed: the archived check below is what retires a
                # subscription whose only remaining event is the (silent, unclaimed) archive itself.
                task = _kb.get_task(conn, sub["task_id"])
                # Unsubscribe only on archive: ``done`` is reversible in review/controller flows, so keeping the sub
                # lets a later reopen notify the same session. The claimed cursor prevents replay.
                if task and getattr(task, "status", "") == "archived":
                    if record is None:
                        # Nothing owed to a turn, so the archive retires the route right here (this is also what
                        # retires a sub whose only remaining event was the silent, unclaimed archive itself).
                        with contextlib.suppress(Exception):
                            _kbn.remove_notify_sub(conn, **sub_ident)
                    else:
                        # A report is claimed but no turn carries it yet, and the claim's cursor pair is the only
                        # record of it: the row must outlive the submit, or a refusal/raise rewinds a sub that is
                        # already gone and the report is lost with no route left to retry on. Flagged for the
                        # caller, which retires it once the report is accepted.
                        record["archived"] = True
            else:
                old_cursor = new_cursor = 0
                _cursor, events = _kbn.unseen_events_for_sub(conn, kinds=_KANBAN_NOTIFY_KINDS, **sub_ident)
                if not events:
                    continue
                task = _kb.get_task(conn, sub["task_id"])
            texts: list = []
            for ev in events:
                text = _format_kanban_event_text(sub, task, ev, slug)
                if text is None:
                    continue
                wrapped = DiagnosticText(text) if _kb_event_is_diagnostic(ev) else text
                texts.append(wrapped)
                if ping is None or int(ev.id) <= int(sub.get("last_ping_event_id") or 0):
                    continue
                if ping(wrapped, isinstance(wrapped, DiagnosticText)):
                    # Checkpoint the ping apart from the cursor: a busy session re-reads the same unclaimed events
                    # every poll, and the line must not re-fire each time.
                    with contextlib.suppress(Exception):
                        _kbn.record_notify_ping(conn, event_id=int(ev.id), **sub_ident)
            if record is not None:
                if texts:
                    record["texts"] = texts
                else:
                    # Claimed, yet no line to carry (a kind without a formatter slipped in): hand the cursor back
                    # here rather than let a turn that will never run retire the events.
                    claims.remove(record)
                    _notif_rewind_claims([record])
    return claims


def _collect_kanban_claims(sid: str, session: dict, *, claim: bool = True, split: bool = False, ping=None) -> list[dict]:
    """Poll every board for this session's ``platform="tui"`` subscriptions (``kanban_create`` auto-subscribes with
    ``chat_id=HERMES_SESSION_KEY``; no "tui" messaging adapter exists, so this poller is the delivery path). Claiming
    is the same atomic cursor move the gateway notifier makes: exactly-once even if a gateway polls the same DB.

    Every claim this returns is a report still owed to a turn. No other exit leaves a cursor advanced: the boards
    share one accumulator, so a failure in a task lookup, a formatter or a later board rewinds everything committed
    so far — partial results included — before the exception escapes.

    See #59890.
    """
    claims: list = []
    session_key = str(session.get("session_key") or "")
    if not session_key or session.get("_finalized"):
        return claims
    try:
        from hermes_cli import kanban_db as _kb
    except Exception:
        return claims
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        try:
            boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
        except Exception:
            return claims
    # dict keyed by resolved DB identity: first slug per DB wins (a pinned HERMES_KANBAN_DB aliases slugs).
    unique = {}
    for slug, resolved in (_kb_board_key(_kb, board_meta) for board_meta in boards):
        unique.setdefault(resolved, slug)
    try:
        for slug in unique.values():
            _kb_poll_board(sid, _kb, slug, session_key, claim=claim, split=split, ping=ping, out=claims)
    except Exception:
        # A claim is already a durable cursor move: put every one of them back before the exception escapes, or the
        # batch — including the boards that succeeded before the failure — is gone with nothing left to retry.
        _notif_rewind_claims(claims)
        raise
    return claims


def _notif_poll_kanban(sid: str, session: dict) -> None:
    with _session_profile_runtime_scope(session):
        _notif_poll_kanban_scoped(sid, session)


def _notif_rewind_claims(claims: list) -> None:
    """Hand one undelivered batch's claims back: no turn carried them, so the next tick must find them again.

    ``force_rewind_notify_cursor``, not the CAS-guarded ``rewind_notify_cursor``: a concurrent consumer that
    advanced the subscription row past our claim would make the guarded rewind decline, and the events we never
    delivered would stay retired behind the advanced cursor, where no later tick reads them. The forced rewind
    reads the row's current cursor inside one transaction and CASes against that, so the claim's range becomes
    claimable again; re-delivering a range that consumer claimed in between is the accepted price (at-least-once
    over silent loss). A row that is gone or already at ``old_cursor`` leaves nothing to move, and is reported.
    """
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    for claim in claims:
        try:
            conn = _kbc.connect(board=claim["slug"])
        except Exception as exc:
            _notif_log_failure("kanban notification rewind failed", exc)
            continue
        with contextlib.closing(conn):
            try:
                if not _kbn.force_rewind_notify_cursor(conn, claimed_cursor=claim["new_cursor"],
                                                       old_cursor=claim["old_cursor"], **claim["sub"]):
                    print(f"[tui_gateway] kanban notification rewind moved nothing "
                          f"({claim['slug']}/{claim['sub'].get('task_id')})", file=sys.stderr)
            except Exception as exc:
                _notif_log_failure("kanban notification rewind failed", exc)


def _notif_retire_archived_claims(claims: list) -> None:
    """Retire the routes of accepted claims whose task is archived.

    Called only after ``_notif_submit`` reports the report accepted. Archiving must not retire a report the turn
    never delivered: the claim's cursor pair is the only record of it, so the subscription is kept until this
    point — and a refused or raised submit never reaches here, it rewinds the claim and lets the next tick find
    the report again in the row that is still there.
    """
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    for claim in claims:
        if not claim.get("archived"):
            continue
        try:
            conn = _kbc.connect(board=claim["slug"])
        except Exception as exc:
            _notif_log_failure("kanban notification unsubscribe failed", exc)
            continue
        with contextlib.closing(conn):
            with contextlib.suppress(Exception):
                _kbn.remove_notify_sub(conn, **claim["sub"])


def _notif_poll_kanban_scoped(sid: str, session: dict) -> None:
    """One kanban poll: turn first, then claim, then submit — the turn is held across the whole sequence, so no
    prompt interleaves and the events this turn claims are exactly the ones it carries.

    While the session is busy nothing is claimed: the unclaimed cursor IS the buffer, so a report survives a chat
    that closes or a process that dies, and only the status lines that have not pinged yet are rendered. A submit
    that raises or is refused rewinds its claim, so the next tick retries the report instead of dropping it."""
    from gateway.warning_notifications import DiagnosticText, render_notification, warning_notifications_enabled

    def ping(text, diagnostic: bool) -> bool:
        return render_notification(lambda: _emit("status.update", sid, {"kind": "process", "text": text}),
                                   platform="tui", diagnostic=diagnostic)

    if not _notif_claim_turn(session):
        try:
            _collect_kanban_claims(sid, session, claim=False, ping=ping)
        except Exception as exc:
            _notif_log_failure("kanban notification poll failed", exc)
        return
    try:
        claims = _collect_kanban_claims(sid, session, claim=True,
                                        split=not warning_notifications_enabled("tui"), ping=ping)
    except Exception as exc:
        # Collection rewinds the claims it committed before raising, so there is nothing left to hand back.
        _notif_log_failure("kanban notification poll failed", exc)
        claims = []
    texts = [text for claim in claims for text in claim["texts"]]
    if not texts:
        _notif_release_turn(session)
        return
    # One category per turn: a muted (diagnostic) turn must never carry a report the user asked to see.
    diagnostic = all(isinstance(text, DiagnosticText) for text in texts)
    try:
        started = _notif_submit(f"__notif__{int(time.time() * 1000)}", sid, session, "\n".join(texts),
                                "kanban notification dispatch failed",
                                **({"display_metadata": {"notification_category": "diagnostic"}} if diagnostic else {}))
    except Exception:
        _notif_rewind_claims(claims)
        return
    if started is False:
        # A refusal delivered nothing: release the turn (``_run_prompt_submit`` does too) and put the claim back.
        _notif_release_turn(session)
        _notif_rewind_claims(claims)
        return
    # The report is accepted, so an archived task may now retire its route; a refusal above never gets here.
    _notif_retire_archived_claims(claims)


def _notif_dispatch_event(sid: str, session: dict, evt: dict, text: str) -> None:
    """Run the claimed (running=True) agent turn for one notification event."""
    from tools.async_delegation import claim_event_delivery, complete_event_delivery, release_event_delivery
    try:
        claim = claim_event_delivery(evt, "tui-poller")
    except Exception as exc:  # shared ledger busy/unreadable: the durable row stays pending and replays
        _notif_log_failure("notification delivery claim failed", exc)
        claim = None
    if claim is None:
        # Another consumer holds the durable row — a gateway sharing this home claims before it verifies
        # the target. No turn will run, and nothing else clears ``running``: a busy session is exempt
        # from the reaper, keeps its lease, and never reaches its bot mailbox again.
        _notif_release_turn(session)
        return
    kwargs = ({"display_kind": "async_delegation_complete", "display_metadata": _async_delegation_display_metadata(evt)}
              if evt.get("type") == "async_delegation" else {})
    from agent.notification_presentation import diagnostic_process_event
    if diagnostic_process_event(evt):
        kwargs.setdefault("display_metadata", {})["notification_category"] = "diagnostic"
    try:
        started = _notif_submit(f"__notif__{int(time.time() * 1000)}", sid, session, text, "notification poller dispatch failed", **kwargs)
    except Exception:
        release_event_delivery(evt, claim)
        return
    if started is False:
        # A refusal ran no turn: put the durable delivery back so the event replays, and release the
        # claim the caller took for it (``_admit_prompt_turn`` clears the flag; this keeps the
        # contract for any other refusal source).
        release_event_delivery(evt, claim)
        _notif_release_turn(session)
        return
    complete_event_delivery(evt, claim)


def _notif_handle_event(sid, session, evt, emitted, registry, fmt, deferred, completions=None, *, owned=False) -> bool:
    """Route one dequeued event: foreign (another live session owns it) → requeued, or onto ``deferred`` during the
    shutdown drain; unowned (addressed but unprovable — never adopt an orphan) → dropped, except delegation payloads
    deferred for a resume; ours (or ownerless legacy, kept process-global) → status.update once, then an agent turn if
    idle. False = the drain must stop (session busy). ``owned`` skips the ownership gates for events a caller already
    drained through ``_session_owns_notification_event`` (the post-turn safety net), so lineage resolves once."""
    queue = registry.completion_queue
    evt_type, is_delegation = evt.get("type", "completion"), evt.get("type") == "async_delegation"
    if not owned and _notification_event_belongs_elsewhere(sid, session, evt):
        if deferred is not None:
            deferred.append(evt)
        else:  # otherwise a process started in session A surfaces in whichever poller wakes first
            queue.put(evt)
            time.sleep(0.1)
        return True
    if not owned and _notification_event_requires_owner(evt) and not _session_owns_notification_event(sid, session, evt):
        origin, key = str(evt.get("origin_ui_session_id") or ""), str(evt.get("session_key") or "")
        if deferred is None:
            (logger.warning if is_delegation else logger.debug)(
                "Dropping unowned %s notification (origin=%r key=%r) instead of delivering to session %s",
                evt_type, origin, key, sid)
        elif is_delegation:
            deferred.append(evt)
        else:
            logger.debug("Dropping unowned %s notification during shutdown drain (origin=%r key=%r)", evt_type, origin, key)
        return True
    if evt_type == "completion" and registry.is_completion_consumed(evt.get("session_id", "")):
        return True
    text = fmt(evt)
    if not text:
        return True
    # Emit once per dedup key: a re-queued completion would otherwise re-emit every 0.5s while the session is busy,
    # while distinct watch_match events from one process must stay visible.
    dedup_key = _notification_event_dedup_key(evt)
    if dedup_key not in emitted:
        from tools.process_registry_notifications import async_delegation_display_text, process_completion_display_text
        display_text = (async_delegation_display_text(evt) if is_delegation
                        else process_completion_display_text([evt]) if evt_type == "completion" else text)
        from agent.notification_presentation import diagnostic_process_event
        from gateway.warning_notifications import render_notification
        render_notification(lambda: _emit("status.update", sid, {"kind": "process", "text": display_text}),
                            platform="tui", diagnostic=diagnostic_process_event(evt))
        emitted.add(dedup_key)
    if evt_type == "completion" and completions is not None:
        completions.append((evt, text))
        return True
    if not _notif_claim_turn(session):
        queue.put(evt)
        if deferred is not None:
            return False
        time.sleep(0.25)  # back off: the re-queued event keeps the queue non-empty, else this loop spins at 100% CPU
        return True
    _notif_dispatch_event(sid, session, evt, text)
    return True


def _notif_dispatch_completions(sid, session, notifications, registry, deferred):
    from tools.process_registry_notifications import PROCESS_COMPLETE_DISPLAY_KIND, ProcessNotificationBatch
    from tools.async_delegation import claim_event_delivery, complete_event_delivery, release_event_delivery

    if not notifications:
        return
    if not _notif_claim_turn(session):
        for event, _text in notifications:
            (deferred.append if deferred is not None else registry.completion_queue.put)(event)
        if deferred is None:
            time.sleep(0.25)
        return
    claimed: list = []
    try:
        for event, event_text in notifications:
            if (claim := claim_event_delivery(event, "tui-completion-batch")) is not None:
                claimed.append((event, event_text, claim))
        batch = ProcessNotificationBatch(tuple((event, event_text) for event, event_text, _claim in claimed))
        text = batch.render(registry)
    except Exception as exc:
        _notif_log_failure("completion batch preparation failed", exc)
        _notif_release_turn(session)
        for event, _text, claim in claimed:
            release_event_delivery(event, claim)
        return
    if text is None:
        _notif_release_turn(session)
    try:
        if text is not None:
            _notif_submit(f"__notif__{int(time.time() * 1000)}", sid, session, text,
                          "completion batch dispatch failed", display_kind=PROCESS_COMPLETE_DISPLAY_KIND,
                          display_metadata={"display_text": batch.display_text(registry)})
    except Exception:
        for event, _text, claim in claimed:
            release_event_delivery(event, claim)
        return
    for event, _text, claim in claimed:
        complete_event_delivery(event, claim)


def _notif_handle_ready(sid, session, events, emitted, registry, fmt, deferred, *, owned=False):
    """One ready snapshot: ownership and UI emission per event, one turn per completion run."""
    completions = []
    for index, event in enumerate(events):
        if event.get("type", "completion") != "completion":
            _notif_dispatch_completions(sid, session, completions, registry, deferred)
            completions = []
        if not _notif_handle_event(sid, session, event, emitted, registry, fmt, deferred, completions, owned=owned):
            for remaining in events[index + 1:]:
                (deferred.append if deferred is not None else registry.completion_queue.put)(remaining)
            break
    _notif_dispatch_completions(sid, session, completions, registry, deferred)


def _poll_bot_live_delivery_once(sid: str, session: dict) -> bool:
    """Run one durable envelope only after local FIFO/continuations yield the idle boundary."""
    from tools.bot_live_delivery import claim_pending_delivery, complete_delivery, find_canonical_live_owner, has_mailbox

    home = _session_home(session)
    # Most profiles never receive a delivery: without a mailbox there is nothing to claim, and the owner
    # lookup below costs a state.db open plus the exclusive active-session registry lock every pass (#111719).
    if not has_mailbox(home):
        return False
    with _session_turn_admission(session) as admitted:
        if not admitted or any(session.get(key) for key in (
                "running", "_closing", "_finalized", "queued_prompt", "queued_prompts",
                "_auto_continue_scheduled")) or session.get("agent") is None:
            return False
        lease = session.get("active_session_lease")
        if lease is None or getattr(lease, "released", False):
            return False
        owner = find_canonical_live_owner(home)
        if (not owner or owner.get("lease_id") != lease.lease_id
                or owner.get("live_session_id") != sid
                or owner.get("session_id") != session.get("session_key")):
            return False
        # The mailbox matches each envelope to this pinned lease/live id and compression lineage.
        claimed = claim_pending_delivery(home, owner)
        if claimed is None:
            return False
        session["running"] = True

    delivery_id = str(claimed["id"])

    def terminal_receipt(terminal: dict) -> None:
        status = str(terminal.get("status") or "failed")
        error = str(terminal.get("error") or "")
        reason = "cancelled" if status == "cancelled" else ""
        if status not in {"settled", "cancelled"}:
            from tools.bot_failure_reasons import classify_agent_error
            reason = classify_agent_error(error)
        # Let a failed write propagate: the turn must not retire its crash marker without its receipt.
        complete_delivery(home, delivery_id, status=status,
                          reply=str(terminal.get("text") or "") if status == "settled" else "",
                          error=error, reason=reason)

    try:
        started = _run_prompt_submit(f"__bot_dm__{delivery_id}", sid, session, claimed["message"],
                                     image_paths=[], terminal_callback=terminal_receipt,
                                     turn_author=claimed.get("author") or None,
                                     **({"display_metadata": {"notification_category": "diagnostic"}}
                                        if claimed.get("notification_category") == "diagnostic" else {}))
    except Exception as exc:
        _notif_release_turn(session)
        terminal_receipt({"status": "failed", "error": str(exc)})
        raise
    if not started:
        _notif_release_turn(session)
        terminal_receipt({"status": "failed", "error": "live session owner could not start the delivery turn"})
    return started


# A failing mailbox poll (typically the active-session registry lock unavailable under contention) is
# retried on the next ``_BOT_DELIVERY_POLL_SECONDS`` pass; log the failure once per window, not per attempt.
_BOT_POLL_WARN_INTERVAL_S = 60.0


def _poll_bot_live_delivery_guarded(sid: str, session: dict, now: float) -> None:
    """One poller-loop pass of the mailbox poll. A failure is logged at WARNING once per
    ``_BOT_POLL_WARN_INTERVAL_S`` (with the count of suppressed repeats) and at DEBUG otherwise. An
    unthrottled poll logged ``Bot live-owner delivery poll failed`` ~2×/minute per session for days,
    91% of an install's WARNING output (#111719)."""
    try:
        _poll_bot_live_delivery_once(sid, session)
    except Exception:
        suppressed = int(session.get("_bot_poll_warn_suppressed", 0))
        if now - session.get("_bot_poll_warned_at", -_BOT_POLL_WARN_INTERVAL_S) < _BOT_POLL_WARN_INTERVAL_S:
            session["_bot_poll_warn_suppressed"] = suppressed + 1
            logger.debug("Bot live-owner delivery poll failed (repeat)", exc_info=True)
            return
        session["_bot_poll_warned_at"], session["_bot_poll_warn_suppressed"] = now, 0
        logger.warning("Bot live-owner delivery poll failed (%d repeat(s) suppressed since the last report)",
                       suppressed, exc_info=True)
        return
    session["_bot_poll_warn_suppressed"] = 0


def _notification_poller_loop(stop_event: threading.Event, sid: str, session: dict) -> None:
    with _session_profile_runtime_scope(session):
        _notification_poller_scoped_loop(stop_event, sid, session)


def _notification_poller_scoped_loop(stop_event: threading.Event, sid: str, session: dict) -> None:
    """Daemon thread (started by _init_session()) that drains the process-global completion_queue for this session
    (ownership routing: _notif_handle_event) and polls ``kanban_notify_subs`` every ``_KANBAN_POLL_SECONDS`` — the
    delivery path for platform="tui" rows.

    Also polls ``kanban_notify_subs`` every ``_KANBAN_POLL_SECONDS`` for this session's TUI kanban
    subscriptions and delivers terminal task events the same way (status.update + agent turn) — the delivery
    path tools/kanban_tools.py documents for platform="tui" rows (issue #59890).
    """
    from tools.process_registry import process_registry
    from tools.process_registry_notifications import format_process_notification
    queue = process_registry.completion_queue
    emitted = session.setdefault("_notification_emitted", set())
    handle = lambda events, deferred: _notif_handle_ready(  # noqa: E731
        sid, session, events, emitted, process_registry, format_process_notification, deferred)
    last_kanban_poll = last_loop_poll = last_bot_poll = 0.0
    while not stop_event.is_set() and not session.get("_finalized"):
        now = time.monotonic()
        if now - last_bot_poll >= _BOT_DELIVERY_POLL_SECONDS:  # bot DM → live-owner delivery latency ≤ 5 s
            last_bot_poll = now
            _poll_bot_live_delivery_guarded(sid, session, now)
        # /loop and /heartbeat wakeup drivers: fire a due tick for THIS session while idle (same claim-under-lock
        # as kanban dispatch). An active non-parked /goal owns the idle boundary and defers the loop tick.
        if now - last_loop_poll >= _LOOP_POLL_SECONDS:
            last_loop_poll = now
            for what, fire in (("loop wakeup", _maybe_fire_tui_loop_tick), ("heartbeat", _maybe_fire_tui_heartbeat_tick)):
                try:
                    fire(sid, session)
                except Exception as tick_exc:
                    _notif_log_failure(f"{what} poll failed", tick_exc)
        if now - last_kanban_poll >= _KANBAN_POLL_SECONDS:
            last_kanban_poll = now
            _notif_poll_kanban(sid, session)
        try:
            evt = queue.get(timeout=0.5)
        except Exception:
            continue
        ready = [evt]
        for _ in range(queue.qsize()):
            try:
                ready.append(queue.get_nowait())
            except Exception:
                break
        try:
            handle(ready, None)
        except Exception as exc:
            # This thread is the session's only path to notifications, /loop, /heartbeat and its
            # bot mailbox; one bad event must not end all four.
            _notif_log_failure("notification dispatch failed", exc)
    # Drain remaining events after the stop signal so nothing is lost on shutdown; foreign and orphaned-delegation
    # events are handed back to the shared queue afterwards.
    deferred: list = []
    ready = []
    for _ in range(queue.qsize()):
        try:
            ready.append(queue.get_nowait())
        except Exception:
            break
    handle(ready, deferred)
    for evt in deferred:
        queue.put(evt)


def _async_delegation_display_metadata(evt: dict) -> dict:
    """Build display-only metadata before the completion event is formatted."""
    from tools.process_registry_notifications import async_delegation_display_text
    raw_results = evt.get("results")
    results: list[dict] = [r for r in raw_results if isinstance(r, dict)] if isinstance(raw_results, list) else []
    task_count = len(results) or 1
    completed_count = sum(1 for r in results if r.get("status") in {"completed", "success"})
    failed_count = sum(1 for r in results if r.get("status") in {"failed", "error"})
    duration = evt.get("total_duration_seconds") or evt.get("duration_seconds")
    return {"display_text": async_delegation_display_text(evt),
            "delegation_id": str(evt.get("delegation_id") or ""), "task_count": task_count,
            "completed_count": completed_count or task_count - failed_count, "failed_count": failed_count,
            **({"duration_seconds": duration} if isinstance(duration, (int, float)) else {})}


_desktop_ui_wired = False


def _wire_desktop_sinks() -> None:
    """Idempotently wire process-registry and desktop-tool sinks to renderer events: `agent.terminal.output` and
    `terminal.close` (drops a tab without killing the process) route to the window owning the process; desktop-only
    tools pass the turn's ``HERMES_UI_SESSION_ID`` as ``sid``. `_emit` is thread-safe."""
    global _desktop_ui_wired
    from tools.process_registry import process_registry

    def _owner_sid(session) -> str:
        # session may be None (process already finished/pruned) — the tab can still linger and be closed.
        session_key = str(getattr(session, "session_key", "") or "") if session is not None else ""
        if not session_key:
            return ""
        with _sessions_lock:
            return next((sid for sid, s in _sessions.items() if str(s.get("session_key") or "") == session_key), "")
    if getattr(process_registry, "on_output", None) is None:
        process_registry.on_output = lambda session, chunk: _emit(
            "agent.terminal.output", _owner_sid(session), {"process_id": session.id, "chunk": chunk})
    if getattr(process_registry, "on_close", None) is None:
        process_registry.on_close = lambda session, pid: _emit("terminal.close", _owner_sid(session), {"process_id": pid})
    if not _desktop_ui_wired:
        with contextlib.suppress(Exception):
            from tools import desktop_ui
            desktop_ui.set_emitter(lambda sid, event, payload: _emit(event, sid, payload))
            _desktop_ui_wired = True


# (stop_event, thread) for every poller started in this process, pruned of dead threads on each spawn. Test teardowns
# reap leaked pollers through it: an unjoined poller steals events off the process-global queue in a LATER test.
_notification_pollers: list = []


def _start_notification_poller(sid: str, session: dict) -> threading.Event:
    """Start the background notification poller for a TUI session (thread name is greppable)."""
    _wire_desktop_sinks()
    stop = threading.Event()
    t = threading.Thread(target=_notification_poller_loop, args=(stop, sid, session), daemon=True, name=f"tui-notif-poller-{sid}")
    _notification_pollers[:] = [(s, th) for (s, th) in _notification_pollers if th.is_alive()] + [(stop, t)]
    t.start()
    return stop


def _hud_surface_note(session: dict) -> str:
    """The per-surface note for this turn ("" for the plain app window): HUD → the read-the-window-below
    prior; voice-live → the spoken-delegation contract (transcript in, speakable prose out)."""
    surface = session.get("client_surface")
    if surface == "hud":
        from agent.prompt_builder import hud_surface_note
        return hud_surface_note(getattr(session.get("agent"), "valid_tool_names", None))
    if surface == "voice-live":
        from tools.voice_live import voice_live_turn_note
        return voice_live_turn_note(session.get("voice_live_context") or "")
    return ""


def _prepend_note(run_message: Any, note: str) -> Any:
    """Prefix a per-turn note onto the MODEL INPUT, leaving the prompt alone: everything the model must know that the
    user did not type (interrupted reply, reactions, surface) arrives this way, so no scaffolding reaches the
    transcript and no sent message is rewritten — the cached prefix survives."""
    if note and isinstance(run_message, str):
        return f"{note}\n\n{run_message}"
    if note and isinstance(run_message, list):
        return [{"type": "text", "text": note}, *run_message]
    return run_message


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
