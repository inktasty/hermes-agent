"""Bot Chat per-message local time and gap line (``bot_mode.message_timestamps.enabled``).

Contract (spec section 4 item B): in a bot chat every user message reaches the model with a
local-time prefix built from the row's OWN stored timestamp, the current turn's message also
carries a gap line when the previous user message is at least 4h old, the stored row and its
``api_content`` sidecar never carry the prefix, and re-assembling the same transcript is
byte-identical (the prompt cache keeps hitting).
"""

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.turn_context import (
    BOT_CHAT_GAP_THRESHOLD_SECONDS,
    _bot_chat_gap_line,
    _bot_chat_time_enabled,
    build_api_messages,
)
from gateway.message_timestamps import format_message_timestamp
from hermes_state import SessionDB

_NOW_DT = datetime(2026, 9, 22, 20, 0, 0, tzinfo=timezone.utc)
_NOW = _NOW_DT.timestamp()

# Every timestamp assertion in this file is pinned to this zone, never the process-local one.
# The code under test renders in the Hermes-configured zone (``hermes_time.get_timezone()``), so
# the fixture writes this name into the temp-home config and the expected strings pass the same
# zone to ``format_message_timestamp`` explicitly. Without the pin the two sides disagree on any
# box whose process TZ differs from the configured zone.
_PINNED_TZ_NAME = "UTC"
_PINNED_TZ = ZoneInfo(_PINNED_TZ_NAME)


class _Agent:
    """The subset of the agent surface ``build_api_messages`` and the turn reset read."""

    api_mode = "chat_completions"
    ephemeral_system_prompt = None
    _bot_mode_protocol = True
    _session_title_hint = "Bot Chat"
    _compression_warning = None
    _memory_store = None
    max_iterations = 4

    @staticmethod
    def _copy_reasoning_content_for_api(_source, _target):
        return None

    @staticmethod
    def _should_sanitize_tool_calls():
        return False

    def __init__(self):
        self._tool_guardrails = type("G", (), {"reset_for_turn": staticmethod(lambda: None)})()


def _agent(*, timestamp=_NOW, title="Bot Chat"):
    agent = _Agent()
    agent._current_turn_timestamp = timestamp
    agent._session_title_hint = title
    return agent


@pytest.fixture
def bot_chat_time_config():
    """``bot_mode.message_timestamps.enabled: true`` in the active (per-test temp) home, plus an
    explicit ``timezone`` so the code under test renders in a pinned zone (``_PINNED_TZ``) rather
    than whatever the process happens to be running in."""
    from hermes_constants import get_config_path

    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "bot_mode:\n  message_timestamps:\n    enabled: true\n"
        f"timezone: {_PINNED_TZ_NAME}\n",
        encoding="utf-8",
    )
    # hermes_time caches the resolved zone per config source; drop any entry so the value just
    # written is the one the code under test resolves.
    import hermes_time

    hermes_time.reset_cache()
    return config_path


def _assemble(agent, history, idx=None):
    """One request assembly; returns the wire copy only (the stored rows are untouched)."""
    api_messages, _ = build_api_messages(
        agent,
        history,
        current_turn_user_idx=len(history) - 1 if idx is None else idx,
        ext_prefetch_cache="",
        plugin_user_context="",
        moa_config=None,
        active_system_prompt="",
    )
    return api_messages


def _user_rows(api_messages):
    return [m for m in api_messages if m.get("role") == "user"]


def _hour_before(hours, *, minute=0):
    return (_NOW_DT - timedelta(hours=hours, minutes=minute)).timestamp()


# ---------------------------------------------------------------------------
# The prefix
# ---------------------------------------------------------------------------

def test_prefix_comes_from_the_rows_own_stored_timestamp(bot_chat_time_config):
    """The local-time prefix is the row's stored time rendered by the shared helper — not the
    current clock — and the stored row keeps its clean content."""
    stored_ts = _hour_before(1)
    history = [
        {"role": "user", "content": "hello there", "timestamp": stored_ts},
        {"role": "assistant", "content": "hi", "timestamp": stored_ts + 1},
    ]

    wire_user = _user_rows(_assemble(_agent(), history))[0]

    assert wire_user["content"] == f"{format_message_timestamp(stored_ts, tz=_PINNED_TZ)} hello there"
    # The durable row is untouched: no prefix there, and no sidecar was invented.
    assert history[0]["content"] == "hello there"
    assert "api_content" not in history[0]


def test_historical_rows_are_prefixed_after_the_sidecar_substitution(bot_chat_time_config):
    """Rule: render AFTER the stored-copy substitution, or every older row (which replays its
    sidecar) would silently stay unprefixed."""
    stored_ts = _hour_before(2)
    history = [
        {"role": "user", "content": "clean text", "api_content": "clean text [with memory]",
         "timestamp": stored_ts},
        {"role": "assistant", "content": "ok", "timestamp": stored_ts + 1},
    ]

    wire_user = _user_rows(_assemble(_agent(), history))[0]

    assert wire_user["content"] == f"{format_message_timestamp(stored_ts, tz=_PINNED_TZ)} clean text [with memory]"
    # The sidecar itself is the durable copy and stays clean.
    assert history[0]["api_content"] == "clean text [with memory]"


def test_an_existing_prefix_is_stripped_not_doubled(bot_chat_time_config):
    """A messaging surface that already stamped the row keeps its embedded time, rendered once."""
    stored_ts = _hour_before(3)
    stamped = f"{format_message_timestamp(stored_ts, tz=_PINNED_TZ)} routed in from telegram"
    history = [{"role": "user", "content": stamped, "timestamp": stored_ts}]

    wire_user = _user_rows(_assemble(_agent(), history))[0]

    assert wire_user["content"] == stamped
    assert wire_user["content"].count(str(datetime.fromtimestamp(stored_ts, tz=timezone.utc).year)) == 1


def test_two_consecutive_assemblies_are_byte_identical(bot_chat_time_config):
    """Determinism: the same transcript assembles to the same bytes every time, so the prompt
    cache keeps hitting (the risk this whole item is mitigated against)."""
    history = [
        {"role": "user", "content": "first", "api_content": "first [with memory]",
         "timestamp": _hour_before(9)},
        {"role": "assistant", "content": "one", "timestamp": _hour_before(9) + 1},
        {"role": "user", "content": "second", "timestamp": _hour_before(6, minute=12)},
        {"role": "assistant", "content": "two", "timestamp": _hour_before(6, minute=11)},
        {"role": "user", "content": "third", "timestamp": _NOW},
    ]
    agent = _agent()

    first = _assemble(agent, history)
    second = _assemble(agent, history)

    assert first == second
    assert "Gap: 6h" in _user_rows(first)[-1]["content"]


def test_assistant_rows_are_never_prefixed(bot_chat_time_config):
    stored_ts = _hour_before(1)
    history = [
        {"role": "user", "content": "hello", "timestamp": stored_ts},
        {"role": "assistant", "content": "a reply", "api_content": "a reply",
         "timestamp": stored_ts + 1},
    ]

    api_messages = _assemble(_agent(), history)

    assert api_messages[1]["content"] == "a reply"


# ---------------------------------------------------------------------------
# The gap line
# ---------------------------------------------------------------------------

def test_gap_line_after_a_long_silence_and_only_then(bot_chat_time_config):
    """Threshold is 4h: at or past it the gap line appears, under it the message stays clean."""
    long_gap = [
        {"role": "user", "content": "old question", "timestamp": _hour_before(6)},
        {"role": "assistant", "content": "old answer", "timestamp": _hour_before(6) + 1},
        {"role": "user", "content": "new question", "timestamp": _NOW},
    ]
    short_gap = [
        {"role": "user", "content": "old question", "timestamp": _hour_before(3)},
        {"role": "assistant", "content": "old answer", "timestamp": _hour_before(3) + 1},
        {"role": "user", "content": "new question", "timestamp": _NOW},
    ]

    with_gap = _user_rows(_assemble(_agent(), long_gap))[-1]["content"]
    without_gap = _user_rows(_assemble(_agent(), short_gap))[-1]["content"]

    assert with_gap == (
        "[Gap: 6h since the previous message]\n"
        f"{format_message_timestamp(_NOW, tz=_PINNED_TZ)} new question"
    )
    assert without_gap == f"{format_message_timestamp(_NOW, tz=_PINNED_TZ)} new question"


def test_gap_line_appears_only_on_the_current_turn(bot_chat_time_config):
    """A historical row replays stored bytes: a gap re-rendered there would move between turns."""
    history = [
        {"role": "user", "content": "old question", "timestamp": _hour_before(6)},
        {"role": "assistant", "content": "old answer", "timestamp": _hour_before(6) + 1},
        {"role": "user", "content": "new question", "timestamp": _NOW},
    ]

    wire_users = _user_rows(_assemble(_agent(), history))

    assert "Gap:" not in wire_users[0]["content"]
    assert "Gap:" in wire_users[-1]["content"]


def test_no_gap_line_on_the_first_message_of_a_chat(bot_chat_time_config):
    history = [{"role": "user", "content": "hello for the first time", "timestamp": _NOW}]

    wire_user = _user_rows(_assemble(_agent(), history))[0]

    assert "Gap:" not in wire_user["content"]
    assert wire_user["content"] == f"{format_message_timestamp(_NOW, tz=_PINNED_TZ)} hello for the first time"


def test_gap_line_bytes_are_identical_across_two_assemblies(bot_chat_time_config):
    history = [
        {"role": "user", "content": "old question", "timestamp": _hour_before(5, minute=30)},
        {"role": "assistant", "content": "old answer", "timestamp": _hour_before(5, minute=29)},
        {"role": "user", "content": "new question", "timestamp": _NOW},
    ]
    agent = _agent()

    first = _user_rows(_assemble(agent, history))[-1]["content"]
    second = _user_rows(_assemble(agent, history))[-1]["content"]

    assert first == second
    assert "Gap: 5h 30m since the previous message" in first


def test_gap_threshold_is_four_hours_to_the_second():
    """The boundary belongs to the gap (a rendering contract, not a snapshot of the wording)."""
    previous = _NOW - BOT_CHAT_GAP_THRESHOLD_SECONDS

    assert _bot_chat_gap_line(previous, _NOW)
    assert _bot_chat_gap_line(previous + 1, _NOW) == ""
    # No baseline (first message, or nothing survived compaction) and unstamped rows stay silent.
    assert _bot_chat_gap_line(None, _NOW) == ""
    assert _bot_chat_gap_line(previous, None) == ""


# ---------------------------------------------------------------------------
# Blast radius: bot chats only, off everywhere else
# ---------------------------------------------------------------------------

def test_off_by_default_without_the_config_key():
    """No ``bot_mode.message_timestamps.enabled`` anywhere = clean wire bytes, everywhere."""
    stored_ts = _hour_before(6)
    history = [{"role": "user", "content": "hello", "timestamp": stored_ts}]

    assert _bot_chat_time_enabled(_agent()) is False
    assert _user_rows(_assemble(_agent(), history))[0]["content"] == "hello"


def test_a_non_bot_chat_is_never_prefixed_or_gapped(bot_chat_time_config):
    stored_ts = _hour_before(6)
    history = [
        {"role": "user", "content": "old question", "timestamp": stored_ts},
        {"role": "assistant", "content": "old answer", "timestamp": stored_ts + 1},
        {"role": "user", "content": "new question", "timestamp": _NOW},
    ]

    api_messages = _assemble(_agent(title="My notes"), history)

    assert [m["content"] for m in api_messages if m.get("role") == "user"] == [
        "old question",
        "new question",
    ]


def test_protocol_disabled_turns_the_prefix_off(bot_chat_time_config):
    agent = _agent()
    agent._bot_mode_protocol = False
    history = [{"role": "user", "content": "hello", "timestamp": _NOW}]

    assert _bot_chat_time_enabled(agent) is False
    assert _user_rows(_assemble(agent, history))[0]["content"] == "hello"


def test_the_gate_is_resolved_once_per_turn_and_re_read_after_a_turn_reset(
    bot_chat_time_config, monkeypatch
):
    """A per-turn invalidation is what lets a config change land on the next turn; within one
    turn the answer is reused so repeated assemblies cannot drift."""
    from agent.turn_context import _reset_per_turn_agent_state

    agent = _agent()
    assert _bot_chat_time_enabled(agent) is True

    calls = []
    import agent.turn_context as turn_context

    real = turn_context._bot_chat_message_timestamps_enabled

    def _counting(a):
        calls.append(a)
        return real(a)

    monkeypatch.setattr(turn_context, "_bot_chat_message_timestamps_enabled", _counting)

    assert _bot_chat_time_enabled(agent) is True  # memoized: no second read
    assert calls == []

    _reset_per_turn_agent_state(agent)
    assert _bot_chat_time_enabled(agent) is True
    assert len(calls) == 1


def test_bot_chat_time_prefix_never_reaches_the_stored_row_or_sidecar(bot_chat_time_config):
    """Rule 4: the render belongs to the wire copy only, including the current turn's stored
    copy — that sidecar is persisted too."""
    stored_ts = _hour_before(6)
    history = [
        {"role": "user", "content": "old question", "api_content": "old question [ctx]",
         "timestamp": stored_ts},
        {"role": "assistant", "content": "old answer", "timestamp": stored_ts + 1},
        {"role": "user", "content": "new question", "api_content": "new question [ctx2]",
         "timestamp": _NOW},
    ]
    before = [dict(m) for m in history]

    wire = _user_rows(_assemble(_agent(), history, idx=2))

    assert wire[-1]["content"].startswith("[Gap: 6h since the previous message]\n")
    assert "new question [ctx2]" in wire[-1]["content"]
    assert history == before
    assert history[0]["content"] == "old question"
    assert "api_content" in history[2] and history[2]["api_content"] == "new question [ctx2]"


# ---------------------------------------------------------------------------
# End to end: a real turn in a real Bot Chat against an in-process mock provider
# ---------------------------------------------------------------------------

class _MockProvider(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible endpoint that records every request body it is sent."""

    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        resp = type(self).response_queue.pop(0) if type(self).response_queue else _text_response("DONE")
        message = resp["choices"][0]["message"]
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if message.get("content"):
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": message["content"]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass


def _text_response(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _chat_requests(handler) -> list:
    return [r for r in handler.captured_requests if "messages" in r]


def _wire_user_messages(request: dict) -> list:
    return [m for m in request.get("messages", []) if m.get("role") == "user"]


@pytest.fixture()
def bot_chat_turn_env():
    """A real AIAgent against a mock provider, a real session store and a temp HERMES_HOME
    whose config.yaml turns the bot-chat key on. ``make_agent()`` builds a fresh agent bound
    to the same session, so a second call models the next process's turn."""
    _MockProvider.captured_requests = []
    _MockProvider.response_queue = []
    server = HTTPServer(("127.0.0.1", 0), _MockProvider)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    test_home = tempfile.mkdtemp(prefix="hermes_bot_time_")
    hermes_home = Path(test_home) / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "bot_mode:\n  message_timestamps:\n    enabled: true\n", encoding="utf-8"
    )
    previous_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)

    from run_agent import AIAgent

    db = SessionDB(db_path=hermes_home / "state.db")
    sid = "sess-bot-time"

    def make_agent():
        agent = AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat", model="test-model",
            max_iterations=10, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
            session_db=db, session_id=sid,
        )
        # What tui_gateway/server.py::_attach_built_agent does for a canonical Bot Chat session.
        agent._session_title_hint = "Bot Chat"
        return agent

    try:
        yield make_agent, _MockProvider, db, sid
    finally:
        server.shutdown()
        db.close()
        shutil.rmtree(test_home, ignore_errors=True)
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home


def test_a_real_bot_chat_turn_sends_the_prefix_and_stores_a_clean_row(bot_chat_turn_env):
    """The acceptance path end to end: the model receives the prefix, the DB row does not."""
    make_agent, handler, db, sid = bot_chat_turn_env
    agent = make_agent()
    handler.response_queue.append(_text_response("done"))

    agent.run_conversation("status please", conversation_history=[], task_id="t")

    sent = _wire_user_messages(_chat_requests(handler)[0])[0]["content"]
    row = [r for r in db.get_messages(sid) if r["role"] == "user"][0]

    assert sent.startswith(f"{format_message_timestamp(row['timestamp'])} ")
    assert "status please" in sent
    # Rule 4 at the real persist boundary: the stored row (and its sidecar) stay clean.
    assert row["content"] == "status please"
    assert not str(row["api_content"] or "").startswith("[")
    # First message of the chat: no baseline, so no gap line.
    assert "Gap:" not in sent


def test_a_long_silence_gaps_the_next_turn_and_the_replayed_row_keeps_its_own_bytes(bot_chat_turn_env):
    """Turn N+1 after a 6h silence: the older row is prefixed from ITS OWN stored time (not the
    current clock), the current message gets the gap line, and a further turn replays those
    same bytes — the cache-relevant half of the contract."""
    import sqlite3

    make_agent, handler, db, sid = bot_chat_turn_env

    agent1 = make_agent()
    handler.response_queue.append(_text_response("turn one done"))
    agent1.run_conversation("first question", conversation_history=[], task_id="t1")
    first_turn_bytes = _wire_user_messages(_chat_requests(handler)[0])[0]["content"]
    assert first_turn_bytes.endswith(" first question")

    # The chat went quiet: the stored rows move 6h back from the time they carry.
    stored_epoch = [r for r in db.get_messages(sid) if r["role"] == "user"][0]["timestamp"]
    silence_epoch = float(stored_epoch) - 6 * 60 * 60
    connection = sqlite3.connect(db.db_path)
    try:
        connection.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (silence_epoch, sid))
        connection.commit()
    finally:
        connection.close()

    handler.captured_requests = []
    agent2 = make_agent()
    handler.response_queue.append(_text_response("turn two done"))
    agent2.run_conversation(
        "second question", conversation_history=db.get_messages_as_conversation(sid), task_id="t2"
    )

    users = _wire_user_messages(_chat_requests(handler)[0])
    # The older row is rendered from what the store says, never from the clock.
    assert users[0]["content"] == f"{format_message_timestamp(silence_epoch)} first question"
    assert users[0]["content"] != first_turn_bytes
    assert "Gap:" not in users[0]["content"]  # a historical row never carries a gap line
    assert users[-1]["content"].startswith("[Gap: 6h since the previous message]\n")
    assert "second question" in users[-1]["content"]

    # Turn N+2 with nothing changed: the older row replays byte-identical, and the gap line
    # stays behind with the turn that fired it (rule: never re-rendered on a historical row).
    handler.captured_requests = []
    agent3 = make_agent()
    handler.response_queue.append(_text_response("turn three done"))
    agent3.run_conversation(
        "third question", conversation_history=db.get_messages_as_conversation(sid), task_id="t3"
    )

    replayed = _wire_user_messages(_chat_requests(handler)[0])
    rows = [r for r in db.get_messages(sid) if r["role"] == "user"]

    # The oldest row replays byte-identical; the row that fired a gap line last turn replays
    # WITHOUT it — a historical row is never re-gapped, and its prefix still comes from its own
    # stored time.
    assert replayed[0]["content"] == users[0]["content"]
    assert replayed[1]["content"] == f"{format_message_timestamp(rows[1]['timestamp'])} second question"
    assert replayed[1]["content"] == users[1]["content"].split("\n", 1)[1]
    assert "Gap:" not in replayed[1]["content"]
    # The new current turn is well under the threshold, so it carries no gap line either.
    assert replayed[2]["content"] == f"{format_message_timestamp(rows[2]['timestamp'])} third question"

    assert [r["content"] for r in rows] == ["first question", "second question", "third question"]

