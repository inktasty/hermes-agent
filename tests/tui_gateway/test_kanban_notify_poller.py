"""Tests for the TUI-side kanban notification poller (issue #59890).

``kanban_create`` auto-subscribes TUI/desktop sessions with
``platform="tui"`` / ``chat_id=HERMES_SESSION_KEY``, but no component ever
read those rows back: the gateway notifier skips them (no "tui" messaging
adapter) and the TUI notification poller only watched process completions.
``last_event_id`` stayed 0 forever and no notification was ever delivered.

These tests cover the delivery half that now lives in tui_gateway/server.py:
``_collect_kanban_claims`` (cursor claim + formatting + archive-only
unsubscribe), ``_format_kanban_event_text``, and the poller that turns one
claim into one turn — including the two ways a claim is given back.
"""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import server
from tui_gateway.server import (
    _collect_kanban_claims,
    _format_kanban_event_text,
)

SESSION_KEY = "tui-session-key-1"
SID = "sid-kanban-poller-test"


def _session(key: str = SESSION_KEY) -> dict:
    return {"session_key": key}


def _collect(session: dict, **kwargs) -> list:
    """The claim records one poll takes for ``session`` (no pings: the texts are what matters here)."""
    return _collect_kanban_claims(SID, session, **kwargs)


def _texts(session: dict, **kwargs) -> list:
    return [text for claim in _collect(session, **kwargs) for text in claim["texts"]]


def _create_subscribed_task(*, chat_id: str = SESSION_KEY, platform: str = "tui"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="notify tui", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        return tid
    finally:
        conn.close()


def _complete(tid: str, summary: str = "all done") -> None:
    conn = kbc.connect()
    try:
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def _split_bound_task() -> str:
    """A subscription whose split claim needs its cursor bound: completed(1), crashed(2), completed(3).

    The turn carries the first result alone, but a claim narrowed to the ``completed`` kind also matches the
    LATER completed event and moves the cursor onto it — over the crash in between.
    """
    tid = _create_subscribed_task()
    _complete(tid, summary="first result")
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "crashed", {"error": "boom"})
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.complete_task(conn, tid, summary="second result")
    finally:
        conn.close()
    return tid


def _sub_rows(tid: str) -> list:
    conn = kbc.connect()
    try:
        return kbn.list_notify_subs(conn, task_id=tid)
    finally:
        conn.close()


def _cursor(tid: str) -> int:
    return _sub_rows(tid)[0]["last_event_id"]


def _poll_session(*, running: bool = False) -> dict:
    """A live session dict as the poller sees it: the turn claim is ``running`` under ``history_lock``."""
    return {"session_key": SESSION_KEY, "history_lock": threading.RLock(), "running": running}


def _split_poll_session(monkeypatch, *, running: bool = False) -> dict:
    """The same session with warnings suppressed: the poll claims one presentation category per turn."""
    from gateway import warning_notifications as _wn

    monkeypatch.setattr(_wn, "warning_notifications_enabled", lambda platform, user_config=None: False)
    return _poll_session(running=running)


def _sub_ident(tid: str) -> dict:
    return dict(task_id=tid, platform="tui", chat_id=SESSION_KEY, thread_id="")


def _competing_advance(tid: str) -> int:
    """A second consumer claims the same subscription row, moving its cursor past this poll's claim."""
    before = _cursor(tid)
    conn = kbc.connect()
    try:
        _old, moved, _events = kbn.claim_unseen_events_for_sub(conn, kinds=("completed",), **_sub_ident(tid))
    finally:
        conn.close()
    assert _cursor(tid) > before, "the concurrent consumer must really move the row"
    return moved


class TestCollectKanbanClaims:
    def test_zero_sub_board_is_never_opened_writable(self):
        conn = kbc.connect()
        conn.close()
        kb.create_board("second-board")

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _texts(_session())

        assert texts == []
        spy_connect.assert_not_called()

    def test_done_reopen_notifies_once_per_event_until_archive(self):
        tid = _create_subscribed_task()
        _complete(tid, summary="shipped the fix")

        first = _texts(_session())

        assert len(first) == 1
        assert tid in first[0]
        assert "done" in first[0]
        assert "shipped the fix" in first[0]
        rows = _sub_rows(tid)
        assert len(rows) == 1, "done must retain the originating session"
        first_cursor = rows[0]["last_event_id"]

        # The retained subscription must not replay the completed event.
        assert _texts(_session()) == []

        conn = kbc.connect()
        try:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,)
                )
                kb._append_event(conn, tid, "status", {"status": "ready"})
            assert kb.complete_task(conn, tid, summary="review corrections")
        finally:
            conn.close()

        reopened = _texts(_session())

        assert len(reopened) == 2
        assert "ready" in reopened[0]
        assert "review corrections" in reopened[1]
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY
        assert rows[0]["last_event_id"] > first_cursor
        assert _texts(_session()) == []

        conn = kbc.connect()
        try:
            assert kb.archive_task(conn, tid)
        finally:
            conn.close()

        # Archive is notification-terminal and removes the retained route.
        assert _texts(_session()) == []
        assert _sub_rows(tid) == []

    def test_matching_tui_sub_delivers_and_advances_cursor(self):
        tid = _create_subscribed_task()
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        conn = kbc.connect()
        try:
            kb.block_task(conn, tid, reason="waiting on review")
        finally:
            conn.close()

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            first = _texts(_session())
            second = _texts(_session())

        assert len(first) == 1
        assert "blocked" in first[0]
        assert "waiting on review" in first[0]
        assert second == []
        assert spy_connect.called
        # Blocked is not a final status -> subscription stays alive so a
        # respawned task's next terminal event still reaches the user.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] > pre_cursor

    def test_non_tui_subscription_does_not_open_board_writable(self):
        tid = _create_subscribed_task(platform="telegram", chat_id="chat-1")
        # New subs start caught up at creation time (issue #29905); record the
        # pre-completion cursors so we can assert they were never claimed.
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _texts(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_other_tui_session_does_not_open_board_writable(self):
        tid = _create_subscribed_task(chat_id="some-other-session")
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _texts(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_probe_error_falls_back_to_writable_delivery(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="fallback delivery")

        def fail_probe(*args, **kwargs):
            raise OSError("probe unavailable")

        monkeypatch.setattr(kbn, "count_notify_subs", fail_probe)
        with patch.object(kbc, "connect", wraps=kbc.connect) as spy_connect:
            texts = _texts(_session())

        assert len(texts) == 1
        assert tid in texts[0]
        spy_connect.assert_called_once()

    def test_no_session_key_is_a_noop(self):
        tid = _create_subscribed_task()
        _complete(tid)

        assert _collect_kanban_claims(SID, {"session_key": ""}) == []
        assert _collect_kanban_claims(SID, {"session_key": None}) == []
        assert len(_sub_rows(tid)) == 1

    def test_split_claim_never_skips_the_other_category(self):
        """With warnings suppressed the poller claims one presentation category per turn.

        A claim moves the cursor to the highest id it RETURNS, so a kind-narrowed claim would also skip past an
        event of the other category sitting below that id. The claim therefore stops at the run the turn carries:
        nothing is lost, and the events come out in order, one category per poll.
        """
        tid = _create_subscribed_task()
        _complete(tid, summary="first result")
        conn = kbc.connect()
        try:
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "crashed", {"error": "boom"})
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
                kb._append_event(conn, tid, "status", {"status": "ready"})
            assert kb.complete_task(conn, tid, summary="second result")
        finally:
            conn.close()

        first = _texts(_session(), split=True)
        second = _texts(_session(), split=True)
        third = _texts(_session(), split=True)

        assert len(first) == 1 and "first result" in first[0]
        assert len(second) == 1 and "worker crashed" in second[0]
        assert len(third) == 2 and "ready" in third[0] and "second result" in third[1]
        assert _texts(_session(), split=True) == []

    def test_split_claim_bounds_when_the_run_ends(self, monkeypatch):
        """The narrowed claim stops at the run's end: the later same-kind event waits, the crash is not skipped.

        The cap rides in the claim itself, so there is no second cursor move to confirm: ``new_cursor`` is the
        run's last event id. Landing there (and not on the later ``completed`` a kind-only claim would reach for)
        is what keeps both the crash it steps over and the later result visible to the next poll.
        """
        tid = _split_bound_task()
        committed = _cursor(tid)

        first = _texts(_session(), split=True)
        bounded_cursor = _cursor(tid)

        assert len(first) == 1 and "first result" in first[0], first
        # The claim landed on the first event's id: the crash below the later ``completed`` is still visible to
        # the next poll (a cursor left on the second result would retire it unread).
        assert bounded_cursor > committed, "the first result was claimed"
        second = _texts(_session(), split=True)
        assert len(second) == 1 and "worker crashed" in second[0], second
        third = _texts(_session(), split=True)
        assert len(third) == 1 and "second result" in third[0], third
        assert _cursor(tid) > bounded_cursor
        assert _texts(_session(), split=True) == []

    def test_split_claim_records_the_cursor_the_bound_left_in_the_row(self):
        """The claim record is what a later rewind CASes against, so it must carry the row's ACTUAL cursor.

        A confirmed bound moves the row back to the run's end, and the broader claim's cursor is no longer it.
        Recording the stale value makes every later rewind (refused/raised turn, collection failure) lose its
        CAS, leaving the events this turn could not deliver behind a cursor no future poll reads past.
        """
        tid = _split_bound_task()
        pre_cursor = _cursor(tid)

        claims = _collect(_session(), split=True)

        assert len(claims) == 1, claims
        record = claims[0]
        assert record["new_cursor"] == _cursor(tid), (record["new_cursor"], _cursor(tid))
        assert len(record["texts"]) == 1 and "first result" in record["texts"][0], record["texts"]

        server._notif_rewind_claims(claims)

        assert _cursor(tid) == pre_cursor, "the record's cursor must rewind the row to its pre-claim value"
        retry = _texts(_session(), split=True)
        assert len(retry) == 1 and "first result" in retry[0], retry
        # Drain the rest: leave no half-read subscription behind for a sibling test's poller.
        assert "worker crashed" in _texts(_session(), split=True)[0]
        assert len(_texts(_session(), split=True)) == 1
        assert _texts(_session(), split=True) == []

    def test_split_claim_that_loses_the_range_never_rewinds_the_cursors_row(self, monkeypatch):
        """Another consumer claims the range in the window between this poll's read and its claim.

        The claim then returns nothing and moves nothing, so this poll holds no events and registers no claim:
        rewinding a cursor it never advanced would put back events the other consumer already took and re-deliver
        them from under it.
        """
        tid = _split_bound_task()
        real_bounded = kbn.claim_unseen_events_for_sub_bounded
        real_claim = kbn.claim_unseen_events_for_sub
        advanced: list = []

        def stolen(conn, **kwargs):
            # Another consumer claims the same range in the window between this poll's read and its claim, so the
            # bounded claim that follows finds the row already past every id in its range.
            _old, new, _events = real_claim(conn, kinds=kwargs["kinds"], **_sub_ident(tid))
            advanced.append(new)
            return real_bounded(conn, **kwargs)

        monkeypatch.setattr(kbn, "claim_unseen_events_for_sub_bounded", stolen)
        assert _texts(_session(), split=True) == []

        assert advanced and advanced[0] > 0, "the other consumer's claim must have run"
        assert _cursor(tid) == advanced[0], "this poll must leave the other consumer's claim alone"

    def test_post_claim_error_rewinds_the_cursor_and_the_next_tick_delivers(self, monkeypatch):
        """A failure AFTER the claim is a delivery that never happened too.

        The claim is the only durable record of an owed report: if collection simply aborts (task lookup,
        formatter, a later board), the cursor stays advanced and no turn ever carries the event.
        """
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="lookup blew up")
        session = _session()
        real_get_task = kb.get_task
        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        claimed: list = []

        def boom(conn, task_id):
            claimed.append(_cursor(tid))  # the claim has already committed when the lookup runs
            raise RuntimeError("task lookup blew up")

        monkeypatch.setattr(kb, "get_task", boom)
        with pytest.raises(RuntimeError):
            _collect_kanban_claims(SID, session)

        assert claimed and claimed[0] > pre_cursor, "the claim must commit before the failure"
        assert _cursor(tid) == pre_cursor, "a post-claim failure must hand the claim back"

        # Restore only the injected failure: the autouse fixtures share this monkeypatch.
        monkeypatch.setattr(kb, "get_task", real_get_task)
        # Nothing was lost: the next tick finds the same event and returns it for a turn.
        texts = _texts(session)
        assert len(texts) == 1 and "lookup blew up" in texts[0], texts
        assert _cursor(tid) > pre_cursor

    def test_a_later_sub_failure_rewinds_the_claims_already_taken(self, monkeypatch):
        """One poll walks several subscriptions: a failure late in the walk must not strand the earlier claims."""
        first = _create_subscribed_task()
        second = _create_subscribed_task()
        _complete(first, summary="first report")
        _complete(second, summary="second report")
        conn = kbc.connect()
        try:
            order = [s["task_id"] for s in kbn.list_notify_subs(conn) if s["chat_id"] == SESSION_KEY]
        finally:
            conn.close()
        assert set(order) == {first, second}, order
        later = order[-1]
        cursors = {tid: _cursor(tid) for tid in (first, second)}
        real_get_task = kb.get_task

        def boom_late(conn, task_id):
            if task_id == later:
                raise RuntimeError("second subscription read blew up")
            return real_get_task(conn, task_id)

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(kb, "get_task", boom_late)
        with pytest.raises(RuntimeError):
            _collect_kanban_claims(SID, _session())

        assert {tid: _cursor(tid) for tid in (first, second)} == cursors, "the partial batch must go back"

        monkeypatch.setattr(kb, "get_task", real_get_task)
        texts = _texts(_session())
        assert len(texts) == 2, texts
        assert "first report" in texts[0] and "second report" in texts[1]

    def test_profile_scoped_session_reads_the_shared_board(self, tmp_path):
        """The kanban board is shared across profiles BY DESIGN (see the
        hermes_cli/kanban_db.py module docstring): ``kanban_home()`` anchors on
        ``get_default_hermes_root()``, which resolves the process env and
        ignores context-local profile overrides. A Desktop session bound to a
        non-launch profile (``session["profile_home"]``) must therefore still
        have its subscription claimed from the one shared board — the poller
        needs no per-profile home binding.
        """
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        tid = _create_subscribed_task()
        _complete(tid, summary="cross-profile delivery")

        other_profile_home = tmp_path / "profiles" / "reviewer"
        other_profile_home.mkdir(parents=True)
        session = {
            "session_key": SESSION_KEY,
            "profile_home": str(other_profile_home),
        }
        # Simulate the strictest case: a context-local profile override is
        # active while the poller collects (as a profile-bound RPC would set).
        token = set_hermes_home_override(str(other_profile_home))
        try:
            texts = _texts(session)
        finally:
            reset_hermes_home_override(token)

        assert len(texts) == 1
        assert tid in texts[0]
        assert "cross-profile delivery" in texts[0]
        # Completion is reversible, so the shared-board subscription remains
        # owned by this exact Desktop session until the task is archived.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY


class TestFormatKanbanEventText:
    SUB = {"task_id": "t_abc123"}
    TASK = SimpleNamespace(title="build the thing", assignee="worker", result=None)

    def test_silent_kinds_return_none(self):
        for kind in ("archived", "unblocked"):
            ev = SimpleNamespace(kind=kind, payload={})
            assert _format_kanban_event_text(self.SUB, self.TASK, ev, "main") is None


    def test_timed_out_with_bad_payload_does_not_raise(self):
        ev = SimpleNamespace(kind="timed_out", payload={"limit_seconds": "not-a-number"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "")
        assert "timed out" in text


class TestNotificationPollerLoopKanbanWiring:
    """Drive a real TUI subscription through ``_notification_poller_loop``.

    Covers the wiring above ``_collect_kanban_claims``: status.update
    emission, agent-turn dispatch when the session is idle, and a busy
    session that claims nothing at all — the unclaimed cursor holds the
    report, so the turn that finally runs is the one that carries it.
    """

    def _start_poller(self, session: dict, monkeypatch):
        emits: list = []
        submits: list = []
        monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.01)
        monkeypatch.setattr(
            server, "_emit", lambda event, sid, payload=None: emits.append((event, payload))
        )
        monkeypatch.setattr(
            server,
            "_run_prompt_submit",
            lambda rid, sid, sess, text, **kwargs: submits.append(text) or True,
        )
        stop = threading.Event()
        thread = threading.Thread(
            target=server._notification_poller_loop,
            args=(stop, "sid-poller-test", session),
            daemon=True,
        )
        thread.start()
        return stop, thread, emits, submits

    @staticmethod
    def _wait_for(predicate, timeout: float = 5.0) -> bool:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if predicate():
                return True
            _time.sleep(0.02)
        return False

    def _poller_session(self, *, running: bool = False) -> dict:
        return {
            "session_key": SESSION_KEY,
            "history_lock": threading.Lock(),
            "running": running,
        }

    def _split_poller_session(self, monkeypatch, *, running: bool = False) -> dict:
        """A session whose warnings are suppressed: the poller claims one presentation category per turn."""
        from gateway import warning_notifications as _wn

        monkeypatch.setattr(_wn, "warning_notifications_enabled", lambda platform, user_config=None: False)
        return self._poller_session(running=running)

    @staticmethod
    def _status_texts(emits, tid: str) -> list:
        return [p["text"] for e, p in emits if e == "status.update" and p and tid in p["text"]]

    def test_idle_session_gets_status_update_and_agent_turn(self, monkeypatch):
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="poller e2e done")
        session = self._poller_session(running=False)

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            assert self._wait_for(lambda: submits), "agent turn was never dispatched"
        finally:
            stop.set()
            thread.join(timeout=5)

        assert self._status_texts(emits, tid), emits
        # The turn-start frame belongs to the admitted turn (_run_prompt_submit emits it), never to the
        # poller: a frame sent before admission survives a refused turn as a permanent "working" state.
        assert not any(e == "message.start" for e, _ in emits), emits
        assert any(tid in text for text in submits), submits
        assert session["running"] is True  # poller claimed the turn
        assert "_kanban_pending" not in session
        # Claimed and carried by that turn: the cursor moved, so nothing replays.
        assert _cursor(tid) > pre_cursor

    def test_busy_session_claims_nothing_and_delivers_once_idle(self, monkeypatch):
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="held while busy")
        session = self._poller_session(running=True)

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            # Busy: the status line appears, but the report stays unclaimed and
            # nothing is held in memory for a later flush.
            assert self._wait_for(lambda: self._status_texts(emits, tid))
            assert not submits
            assert "_kanban_pending" not in session
            assert _cursor(tid) == pre_cursor, "a busy session must advance no cursor"
            # The ping is checkpointed per event id, so it does not re-fire on
            # every 0.01s poll (that is what last_ping_event_id is for).
            assert self._wait_for(lambda: len(self._status_texts(emits, tid)) > 1, timeout=0.4) is False

            with session["history_lock"]:
                session["running"] = False

            assert self._wait_for(lambda: submits), "the unclaimed report was never delivered"
        finally:
            stop.set()
            thread.join(timeout=5)

        assert any(tid in text for text in submits), submits
        assert _cursor(tid) > pre_cursor
        assert session["running"] is True

    def test_refused_turn_rewinds_the_claim_and_the_next_tick_retries(self, monkeypatch):
        """_run_prompt_submit declining is a delivery that never happened."""
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="refused once")
        session = self._poller_session(running=False)
        submits: list = []

        def refused(rid, sid, sess, text, **kwargs):
            submits.append(text)
            return False

        emits: list = []
        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: emits.append(args))
        monkeypatch.setattr(server, "_run_prompt_submit", refused)
        server._notif_poll_kanban("sid-direct", session)

        assert submits and tid in submits[0], submits
        assert not any(a and a[0] == "message.start" for a in emits), (
            "a refused turn must not announce itself: the client latches 'working' on that frame and "
            "no turn runs to send its end"
        )
        assert _cursor(tid) == pre_cursor, "a refused turn must rewind its claim"
        assert session["running"] is False, "the session must be claimable again"
        assert "_kanban_pending" not in session

        monkeypatch.setattr(
            server, "_run_prompt_submit", lambda rid, sid, sess, text, **kwargs: True
        )
        server._notif_poll_kanban("sid-direct", session)

        assert _cursor(tid) > pre_cursor

    def test_raised_turn_rewinds_the_claim_and_the_next_tick_retries(self, monkeypatch):
        """A raise from the dispatch is the same failed delivery as a refusal."""
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="raised once")
        session = self._poller_session(running=False)
        submits: list = []

        def boom(rid, sid, sess, text, **kwargs):
            submits.append(text)
            raise RuntimeError("turn dispatch blew up")

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit", boom)
        server._notif_poll_kanban("sid-direct", session)

        assert submits and tid in submits[0], submits
        assert _cursor(tid) == pre_cursor, "a raised turn must rewind its claim"
        assert session["running"] is False, "the session must be claimable again"

        monkeypatch.setattr(
            server, "_run_prompt_submit", lambda rid, sid, sess, text, **kwargs: True
        )
        server._notif_poll_kanban("sid-direct", session)

        assert _cursor(tid) > pre_cursor

    def test_post_claim_error_rewinds_and_leaves_the_session_claimable(self, monkeypatch):
        """A failure between the claim and the submit must not strand the claim or hold the turn.

        Collection swallows its own failure at the poller, so a lost rewind here is invisible: the cursor would
        stay advanced, no turn would ever carry the report, and a turn left held would keep the session busy.
        """
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="poll-time lookup blew up")
        session = self._poller_session(running=False)
        real_get_task = kb.get_task
        submits: list = []

        def boom(conn, task_id):
            raise RuntimeError("task lookup blew up")

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            server, "_run_prompt_submit",
            lambda rid, sid, sess, text, **kwargs: submits.append(text) or True,
        )
        monkeypatch.setattr(kb, "get_task", boom)
        server._notif_poll_kanban("sid-direct", session)

        assert submits == []
        assert _cursor(tid) == pre_cursor, "a poll that cannot deliver must hand its claim back"
        assert session["running"] is False, "a poll that cannot deliver must release the turn it claimed"

        monkeypatch.setattr(kb, "get_task", real_get_task)
        server._notif_poll_kanban("sid-direct", session)

        assert len(submits) == 1 and tid in submits[0], submits
        assert _cursor(tid) > pre_cursor

    def test_split_refused_turn_rewinds_the_bounded_cursor_and_the_next_tick_retries(self, monkeypatch):
        """With warnings suppressed the claim is bounded, so its record must rewind from the BOUNDED cursor.

        The split turn carries the first result alone; the row sits at the run's end after the bound, not at the
        broader claim's cursor. A refusal that CASes against the stale value loses, and the first result stays
        skipped behind the advanced cursor.
        """
        tid = _split_bound_task()
        pre_cursor = _cursor(tid)
        session = self._split_poller_session(monkeypatch)
        submits: list = []

        def refused(rid, sid, sess, text, **kwargs):
            submits.append(text)
            return False

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit", refused)
        server._notif_poll_kanban("sid-direct", session)

        assert len(submits) == 1 and "first result" in submits[0], submits
        assert _cursor(tid) == pre_cursor, "a refused split turn must rewind its bounded claim"
        assert session["running"] is False, "the session must be claimable again"

        monkeypatch.setattr(
            server, "_run_prompt_submit", lambda rid, sid, sess, text, **kwargs: submits.append(text) or True
        )
        server._notif_poll_kanban("sid-direct", session)

        assert len(submits) == 2 and "first result" in submits[1], submits
        assert _cursor(tid) > pre_cursor

    def test_split_raised_turn_rewinds_the_bounded_cursor_and_the_next_tick_retries(self, monkeypatch):
        """A raise from the split dispatch is the same failed delivery as a refusal: the bound must come back."""
        tid = _split_bound_task()
        pre_cursor = _cursor(tid)
        session = self._split_poller_session(monkeypatch)
        submits: list = []

        def boom(rid, sid, sess, text, **kwargs):
            submits.append(text)
            raise RuntimeError("split turn dispatch blew up")

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit", boom)
        server._notif_poll_kanban("sid-direct", session)

        assert len(submits) == 1 and "first result" in submits[0], submits
        assert _cursor(tid) == pre_cursor, "a raised split turn must rewind its bounded claim"
        assert session["running"] is False, "the session must be claimable again"

        monkeypatch.setattr(
            server, "_run_prompt_submit", lambda rid, sid, sess, text, **kwargs: submits.append(text) or True
        )
        server._notif_poll_kanban("sid-direct", session)

        assert len(submits) == 2 and "first result" in submits[1], submits
        assert _cursor(tid) > pre_cursor


class TestKanbanReturnPath:
    """One claim, two outcomes: a turn carries it, or it goes back for the next tick.

    The unclaimed cursor is the buffer (a busy session holds nothing in memory and re-pings nothing), a submit
    that refuses or raises delivered nothing and therefore rewinds its claim, and a concurrent consumer that
    moves the row past the claim in between must not strand the report: the forced rewind reads the row's
    CURRENT cursor, so the next tick still finds the report and runs the turn that carries it.
    """

    def test_busy_session_advances_no_cursor_holds_nothing_and_pings_each_event_once(self, monkeypatch):
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="held while busy")
        session = _poll_session(running=True)
        pings: list = []
        monkeypatch.setattr(server, "_emit", lambda *args: pings.append(args))

        server._notif_poll_kanban(SID, session)
        server._notif_poll_kanban(SID, session)

        # Rendered once: the ping is checkpointed per event id (``last_ping_event_id``), so the same unclaimed
        # event does not re-fire on every poll while the session stays busy.
        assert len(pings) == 1, pings
        assert tid in pings[0][2]["text"], pings[0]
        assert _cursor(tid) == pre_cursor, "a busy session must advance no cursor"
        assert "_kanban_pending" not in session, "a busy session must hold nothing in memory"
        assert session["running"] is True

    def test_idle_session_claims_the_report_and_delivers_it_as_one_turn(self, monkeypatch):
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="delivered report")
        session = _poll_session()
        submits: list = []
        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit",
                            lambda rid, sid, sess, text, **kwargs: submits.append(text) or True)

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1 and tid in submits[0] and "delivered report" in submits[0], submits
        assert session["running"] is True, "the poller holds the turn it just submitted"
        assert "_kanban_pending" not in session
        assert _cursor(tid) > pre_cursor, "the turn that carries the report is the one that claims it"

        session["running"] = False
        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1, "a claimed report must not be delivered twice"

    @pytest.mark.parametrize("flag", [False, None])
    def test_refused_submit_rewinds_the_claim_and_the_next_tick_retries(self, monkeypatch, flag):
        """``_notif_submit`` returns the submit's flag: False and None both delivered nothing, so the claim goes back."""
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="refused once")
        session = _poll_session()
        submits: list = []
        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit",
                            lambda rid, sid, sess, text, **kwargs: submits.append(text) or flag)

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1 and "refused once" in submits[0], submits
        assert _cursor(tid) == pre_cursor, "a refused submit delivers nothing, so the claim must go back"
        assert session["running"] is False, "the session must be claimable again"

        monkeypatch.setattr(server, "_run_prompt_submit",
                            lambda rid, sid, sess, text, **kwargs: submits.append(text) or True)
        server._notif_poll_kanban(SID, session)

        assert len(submits) == 2 and "refused once" in submits[1], submits
        assert _cursor(tid) > pre_cursor

    def test_raised_submit_rewinds_the_claim_and_the_next_tick_retries(self, monkeypatch):
        """A raise from the dispatch is the same failed delivery as a refusal."""
        tid = _create_subscribed_task()
        pre_cursor = _cursor(tid)
        _complete(tid, summary="raised once")
        session = _poll_session()
        submits: list = []

        def boom(rid, sid, sess, text, **kwargs):
            submits.append(text)
            raise RuntimeError("turn dispatch blew up")

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit", boom)

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1 and "raised once" in submits[0], submits
        assert _cursor(tid) == pre_cursor, "a raised submit delivers nothing, so the claim must go back"
        assert session["running"] is False, "the session must be claimable again"

        monkeypatch.setattr(server, "_run_prompt_submit",
                            lambda rid, sid, sess, text, **kwargs: submits.append(text) or True)
        server._notif_poll_kanban(SID, session)

        assert len(submits) == 2 and "raised once" in submits[1], submits
        assert _cursor(tid) > pre_cursor

    @pytest.mark.parametrize("failure", ["refusal", "raise"])
    def test_failed_submit_after_a_concurrent_advance_still_retries(self, monkeypatch, failure):
        """The failed turn and the moved row: a REAL second claim advances the cursor past our claim first.

        A CAS-guarded rewind would decline against the moved row, and the report this turn never delivered would
        stay retired behind the advanced cursor, where no later tick reads it. The forced rewind takes the row
        back from wherever it now is, so the next tick finds the report again and runs the turn that carries it.
        """
        tid = _split_bound_task()
        pre_cursor = _cursor(tid)
        session = _split_poll_session(monkeypatch)
        submits: list = []
        claimed: list = []

        def dispatch(rid, sid, sess, text, **kwargs):
            submits.append(text)
            if len(submits) == 1:
                claimed.append(_cursor(tid))
                _competing_advance(tid)
                if failure == "raise":
                    raise RuntimeError("turn dispatch blew up")
                return False
            return True

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit", dispatch)

        server._notif_poll_kanban(SID, session)

        assert claimed and claimed[0] > pre_cursor, claimed
        assert len(submits) == 1 and "first result" in submits[0], submits
        assert _cursor(tid) == pre_cursor, "the failed submit must take the row back past the concurrent advance"
        assert session["running"] is False, "the session must be claimable again"

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 2 and "first result" in submits[1], submits
        assert _cursor(tid) > pre_cursor


class TestArchivedTaskReturnPath:
    """An archived task must not retire a report its turn never delivered.

    Archive is the terminal state that retires the subscription (``done`` is reversible, so the route survives
    it), but the removal has to wait for the report's claim to be accepted: while a submit can still refuse or
    raise, the claim's cursor pair is the only record of the owed report, so the row must survive the failed
    turn and the next tick must still find the report in it.
    """

    @staticmethod
    def _archived_with_pending_report() -> str:
        """A task archived while its completion is still unclaimed: the report is owed, the route still exists."""
        tid = _create_subscribed_task()
        _complete(tid, summary="carried past the archive")
        conn = kbc.connect()
        try:
            assert kb.archive_task(conn, tid)
        finally:
            conn.close()
        assert _sub_rows(tid), "archiving a task alone must not drop the route"
        return tid

    @pytest.mark.parametrize("failure", ["refusal", "raise"])
    def test_failed_submit_keeps_the_route_and_the_next_tick_delivers_the_report(self, monkeypatch, failure):
        tid = self._archived_with_pending_report()
        pre_cursor = _cursor(tid)
        session = _poll_session()
        submits: list = []
        advanced: list = []

        def dispatch(rid, sid, sess, text, **kwargs):
            submits.append(text)
            if len(submits) == 1:
                if failure == "raise":
                    raise RuntimeError("turn dispatch blew up")
                return False
            # The claim is in the row before the accepted submit retires it.
            advanced.append(_cursor(tid))
            return True

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit", dispatch)

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1 and "carried past the archive" in submits[0], submits
        assert _cursor(tid) == pre_cursor, "a failed turn hands its claim back"
        assert session["running"] is False, "the session must be claimable again"
        assert len(_sub_rows(tid)) == 1, "the route must outlive the submit that never landed"

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 2 and "carried past the archive" in submits[1], submits
        assert advanced and advanced[0] > pre_cursor, advanced
        assert _sub_rows(tid) == [], "accepting the report is what retires the archived route"

    def test_split_claim_on_an_archived_task_keeps_the_route_until_acceptance(self, monkeypatch):
        """The same guarantee on the one-category-per-turn path, whose claim primitive is a different one."""
        tid = self._archived_with_pending_report()
        session = _split_poll_session(monkeypatch)
        submits: list = []

        def dispatch(rid, sid, sess, text, **kwargs):
            submits.append(text)
            return False if len(submits) == 1 else True

        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit", dispatch)

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1 and "carried past the archive" in submits[0], submits
        assert len(_sub_rows(tid)) == 1, "the refused split claim must leave a rewindable row"

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 2 and "carried past the archive" in submits[1], submits
        assert _sub_rows(tid) == []

    def test_archived_task_with_no_pending_report_retires_the_route_at_once(self, monkeypatch):
        """Nothing claimed → nothing to lose: the route goes on the poll that sees the archive."""
        tid = _create_subscribed_task()
        _complete(tid, summary="already delivered")
        session = _poll_session()
        submits: list = []
        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_run_prompt_submit",
                            lambda rid, sid, sess, text, **kwargs: submits.append(text) or True)

        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1 and len(_sub_rows(tid)) == 1, "done keeps the route"

        conn = kbc.connect()
        try:
            assert kb.archive_task(conn, tid)
        finally:
            conn.close()

        assert len(_sub_rows(tid)) == 1, "the archive itself removes nothing"

        session["running"] = False
        server._notif_poll_kanban(SID, session)

        assert len(submits) == 1, "nothing is left to deliver"
        assert _sub_rows(tid) == [], "a poll with nothing claimed retires the archived route"
