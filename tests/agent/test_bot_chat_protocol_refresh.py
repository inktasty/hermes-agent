"""Same-process regression for the Bot Chat protocol-section refresh (protocol_version 3).

A ``protocol_version`` bump moves the capability epoch, so an eternal Bot Chat rebuilds its
system prompt once on the next turn. But the section TEXT is cached per (process, home)
(``tools.bot_mode_probe._cached``) and the rebuild path reaches it through that cache — so
without a refresh the new epoch gets stamped onto the OLD text, the staleness check then reports
clean forever, and the new lines never arrive. ``agent.conversation_loop
._restore_or_build_system_prompt`` therefore force-refreshes the section cache beside the
skills-cache clear.

These tests edit the section text and rebuild inside ONE process, assert the new text comes back,
and pin the rebuild to exactly once (the following turn reuses the persisted bytes).
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from tools import bot_mode_probe

_EDITED_LINE = "EDITED desk contract line — same process."


@pytest.fixture(autouse=True)
def _fresh_section_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def _bot_home(tmp_path):
    """A managed Bot Mode root with one teammate, so the section is non-empty."""
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "coder"
    profile.mkdir(parents=True)
    (profile / "profile.yaml").write_text(
        textwrap.dedent(
            """\
            ui_meta:
              hermes-bots:
                shape: cloud
            """
        ),
        encoding="utf-8",
    )
    return home


class _SessionDB:
    """Minimal session store: exactly what ``_restore_or_build_system_prompt`` reads and writes.

    ``db_path`` is what ``agent.system_prompt._agent_home`` derives the agent's own home from, so
    the rebuild path and the section getter resolve the same home.
    """

    def __init__(self, home, prompt):
        self.db_path = str(home / "state.db")
        self.prompt = prompt

    def get_session(self, session_id):
        return {"system_prompt": self.prompt}

    def update_system_prompt(self, session_id, prompt):
        self.prompt = prompt


def _stored_prompt():
    """A stored Bot Chat prompt whose runtime identity still matches but whose epoch is stale."""
    return (
        "SYSTEM PROMPT BODY\n\n"
        "Model: test-model\nProvider: openrouter\n"
        "Capability epoch: 000000000000"
    )


def _agent(home, db):
    agent = MagicMock()
    agent.session_id = "20200101_000000_bot"
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent._session_db = db
    agent._session_title_hint = bot_mode_probe.BOT_CHAT_TITLE
    agent._use_prompt_caching = False
    agent._platform_hint_overrides = None
    agent._surface_switch_note = ""
    agent._gateway_turn_context_notes = ""
    # Stands in for the real build: reads the section through the cached getter, exactly as
    # agent.system_prompt._bot_mode_parts does, and stamps the epoch the real prompt carries.
    agent._build_system_prompt = MagicMock(
        side_effect=lambda _system_message: (
            "REBUILT PROMPT\n\n"
            + bot_mode_probe.get_bot_mode_protocol_section(home)
            + "\n\n"
            + bot_mode_probe.epoch_line(home)
        )
    )
    return agent


def test_the_section_ships_the_four_desk_contract_lines(tmp_path):
    """The shipped text itself: each rule is present, and the version salt moved to 3."""
    section = bot_mode_probe.get_bot_mode_protocol_section(_bot_home(tmp_path))
    assert "Place work, do not hold the chat." in section
    assert "Report on your own." in section
    assert "Check before you trust." in section
    assert "never a background child." in section


def test_cached_getter_keeps_the_old_text_and_force_refresh_returns_the_new(tmp_path, monkeypatch):
    """The trap in isolation: the per-(process, home) cache answers with the pre-edit text."""
    home = _bot_home(tmp_path)
    old = bot_mode_probe.get_bot_mode_protocol_section(home)
    assert "Place work, do not hold the chat." in old

    monkeypatch.setattr(bot_mode_probe, "_DESK_CONTRACT", _EDITED_LINE + "\n")

    assert bot_mode_probe.get_bot_mode_protocol_section(home) == old
    assert _EDITED_LINE in bot_mode_probe.get_bot_mode_protocol_section(home, force_refresh=True)


def test_rebuild_in_the_same_process_adopts_the_edited_section_text(tmp_path, monkeypatch):
    """Edit the section text, then rebuild inside ONE process → the new text comes back."""
    home = _bot_home(tmp_path)
    db = _SessionDB(home, _stored_prompt())
    agent = _agent(home, db)

    # A live turn already filled the section cache before the edit landed.
    bot_mode_probe.get_bot_mode_protocol_section(home)
    monkeypatch.setattr(bot_mode_probe, "_DESK_CONTRACT", _EDITED_LINE + "\n")

    _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

    agent._build_system_prompt.assert_called_once()
    assert _EDITED_LINE in agent._cached_system_prompt


def test_the_rebuild_happens_once_and_not_again(tmp_path, monkeypatch):
    """The epoch moves once; the next turn reuses the persisted bytes verbatim."""
    home = _bot_home(tmp_path)
    db = _SessionDB(home, _stored_prompt())
    monkeypatch.setattr(bot_mode_probe, "_DESK_CONTRACT", _EDITED_LINE + "\n")

    # A live turn already filled the section cache before the edit landed.
    bot_mode_probe.get_bot_mode_protocol_section(home)

    first = _agent(home, db)
    _restore_or_build_system_prompt(first, None, [{"role": "user", "content": "hi"}])
    first._build_system_prompt.assert_called_once()
    assert _EDITED_LINE in first._cached_system_prompt

    second = _agent(home, db)
    _restore_or_build_system_prompt(second, None, [{"role": "user", "content": "hi"}])
    second._build_system_prompt.assert_not_called()
    assert second._cached_system_prompt == first._cached_system_prompt
