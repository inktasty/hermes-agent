"""Diagnostic wake execution and structured controls survive a presentation veto."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.notification_presentation import notification_turn
from tui_gateway import server


@pytest.mark.parametrize("muted", [False, True])
def test_real_tui_emitter_keeps_control_frames_and_restores_callbacks(monkeypatch, muted):
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    callback = Mock()
    agent = SimpleNamespace(status_callback=callback, clarify_callback=callback)
    with notification_turn(agent, muted=muted, session_id="session"):
        server._emit("message.delta", "session", {"text": "diagnostic echoed by model"})
        server._emit("message.complete", "session", {"text": "diagnostic echoed by model"})
        agent.clarify_callback("question", ["choice"])
        server._emit("notification.clear", "session", {"key": "cleared"})
    assert [frame["params"]["type"] for frame in frames] == (
        ["notification.clear"] if muted else ["message.delta", "message.complete", "notification.clear"])
    assert agent.status_callback is callback
    callback.assert_called_once_with("question", ["choice"])
    server._emit("message.delta", "session", {"text": "next human result"})
    assert frames[-1]["params"]["payload"]["text"] == "next human result"


@pytest.mark.parametrize("suppress", [False, True])
def test_tui_kanban_splits_diagnostics_from_results_before_wake(tmp_path, monkeypatch, suppress):
    """A suppressed warning is a MUTED turn, so a result must never ride in one.

    With suppression off both events are claimed together and one visible turn carries them. With it on the
    poller claims one presentation category per turn: the crash goes out as the muted diagnostic turn, and the
    result is still waiting on its subscription's cursor — not in an in-memory buffer — for the next poll.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"display: {{suppress_warning_notifications: {str(not suppress).lower()}}}")
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "config.yaml").write_text(f"display: {{suppress_warning_notifications: {str(suppress).lower()}}}")

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="requested result", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="session-key")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "crashed", {"error": "worker crashed"})
        kb.complete_task(conn, tid, summary="requested result")
    finally:
        conn.close()

    emitted, submitted = [], []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_notif_submit",
                        lambda *args, **kwargs: submitted.append((args, kwargs)) or True)
    session = {"session_key": "session-key", "profile_home": str(owner),
               "history_lock": threading.RLock(), "running": False}

    server._notif_poll_kanban("session", session)

    assert len(submitted) == 1, submitted
    assert "worker crashed" in submitted[0][0][3]
    if not suppress:
        # No split: both events are claimed by the one turn.
        assert "requested result" in submitted[0][0][3]
        assert submitted[0][1] == {}
        return
    # Split: the diagnostic turn carries the crash alone and is flagged so the
    # presentation layer can mute it; the result is left unclaimed for the
    # next idle poll rather than hidden inside a muted turn.
    assert "requested result" not in submitted[0][0][3]
    assert submitted[0][1]["display_metadata"]["notification_category"] == "diagnostic"
    assert session["running"] is True  # poller holds the turn it submitted
    assert "_kanban_pending" not in session

    session["running"] = False
    server._notif_poll_kanban("session", session)

    assert len(submitted) == 2, submitted
    assert "requested result" in submitted[1][0][3]
    assert submitted[1][1] == {}


def test_split_delivery_stops_at_the_run_so_the_later_same_kind_event_waits(tmp_path, monkeypatch):
    """The split claim is capped at the run's end, so the later same-kind event is never retired unread.

    A kind-filtered claim would also match the LATER ``completed`` and move the cursor over the crash in between.
    The cap is part of the claim, so it stops at the run: the first result rides its own visible turn, and the
    crash and the second result each still have their turn — the result never rides in a muted diagnostic one.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("display: {suppress_warning_notifications: true}")
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "config.yaml").write_text("display: {suppress_warning_notifications: true}")

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="bounded result", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="session-key")
        kb.complete_task(conn, tid, summary="first result")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "crashed", {"error": "worker crashed"})
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.complete_task(conn, tid, summary="second result")
        results = [int(row["id"]) for row in conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id", (tid,)).fetchall()]
        assert len(results) == 2, results
    finally:
        conn.close()

    emitted, submitted = [], []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_notif_submit",
                        lambda *args, **kwargs: submitted.append((args, kwargs)) or True)
    session = {"session_key": "session-key", "profile_home": str(owner),
               "history_lock": threading.RLock(), "running": False}

    server._notif_poll_kanban("session", session)

    assert len(submitted) == 1, submitted
    text = submitted[0][0][3]
    assert "first result" in text, text
    assert "worker crashed" not in text and "second result" not in text, "the claim stops at the run's end"
    assert submitted[0][1] == {}  # a visible result turn, not a muted diagnostic one
    assert session["running"] is True  # the poller holds the turn it submitted

    conn = kbc.connect()
    try:
        cursor = int(kbn.list_notify_subs(conn, task_id=tid)[0]["last_event_id"])
    finally:
        conn.close()
    assert cursor == results[0], (cursor, results)
    assert cursor < results[1], "the later same-kind event must not be retired behind the cursor"

    session["running"] = False
    server._notif_poll_kanban("session", session)

    assert len(submitted) == 2, submitted
    assert "worker crashed" in submitted[1][0][3]
    assert submitted[1][1]["display_metadata"]["notification_category"] == "diagnostic"
    assert "second result" not in submitted[1][0][3], "a muted turn never carries an unclaimed result"

    session["running"] = False
    server._notif_poll_kanban("session", session)

    assert len(submitted) == 3, submitted
    assert "second result" in submitted[2][0][3]
    assert submitted[2][1] == {}
