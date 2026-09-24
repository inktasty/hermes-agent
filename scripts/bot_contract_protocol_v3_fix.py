#!/usr/bin/env python3
"""Re-apply the Bot Chat desk-contract protocol (``protocol_version`` 3) to Hermes source.

Why: a Bot Chat is an eternal session, so its prompt is rebuilt only when the capability epoch
moves. The desk contract adds four lines to the Bot Mode protocol section and bumps
``protocol_version`` 2 -> 3, which is the salt that moves the epoch and makes every existing bot
adopt the new text once.

The trap this script also restores: the section TEXT is cached per (process, home)
(``tools/bot_mode_probe._cached``) and the rebuild path reaches it through that cache, so a
version bump alone would stamp the NEW epoch onto the OLD text, the staleness check would then
report clean forever, and the four lines would never arrive. ``agent/conversation_loop.py``
therefore force-refreshes the section beside the skills-cache clear.

Both files are Hermes source, so `hermes update` can stash the edits
(``updates.non_interactive_local_changes: stash``). Run this after any update:

    python3 scripts/bot_contract_protocol_v3_fix.py

Exit 0 = both edits in place (applied now, or already there).
Exit 1 = a target file is missing.
Exit 2 = anchors no longer match; upstream reshaped the code, re-derive the edit by hand.
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE = REPO / "tools" / "bot_mode_probe.py"
LOOP = REPO / "agent" / "conversation_loop.py"

CONST_MARKER = "_DESK_CONTRACT = ("
CALL_MARKER = 'f"{_DESK_CONTRACT}"'
LOOP_MARKER = "get_bot_mode_protocol_section(_agent_home(agent), force_refresh=True)"
VERSION_LINE_OLD = '    surface["protocol_version"] = 2'
VERSION_LINE_NEW = '    surface["protocol_version"] = 3'

DESK_CONTRACT_BLOCK = r'''# The desk contract every Bot Chat carries: place long work off-thread and report it, re-check
# facts after a silence, and keep anything durable out of the process. Held in a named constant
# (not inlined in ``_build_section``) so the text is one seam — the same-process regression test
# edits it to prove the rebuild path force-refreshes the cached section (``_cached``).
_DESK_CONTRACT = (
    "Place work, do not hold the chat. If a job will outlive this turn, dispatch it now and "
    "keep the conversation responsive.\n"
    "Report on your own. When a dispatched job lands, post the outcome and the artifact, "
    "unprompted, in one or two lines.\n"
    "Check before you trust. When the gap between messages was long, re-verify anything that "
    "can have changed before answering from memory.\n"
    "Anything that must survive a restart goes to a card or a scheduled job, never a "
    "background child.\n"
)
'''

PROBE_HEADING_OLD = '_PROTOCOL_HEADING = "## Messaging other agents"\n'
PROBE_HEADING_NEW = PROBE_HEADING_OLD + DESK_CONTRACT_BLOCK

PROBE_CALLSITE_OLD = r'''        "acknowledgements.\n"
        f"You are `@{_handle(me)}`. Your teammates (live roster; roles from their "
'''
PROBE_CALLSITE_NEW = r'''        "acknowledgements.\n"
        f"{_DESK_CONTRACT}"
        f"You are `@{_handle(me)}`. Your teammates (live roster; roles from their "
'''

LOOP_OLD = r'''            try:
                from agent.prompt_builder import clear_skills_system_prompt_cache
                clear_skills_system_prompt_cache(clear_snapshot=True)
            except Exception:
                pass
            agent._cached_system_prompt = agent._build_system_prompt(system_message)
'''
LOOP_NEW = r'''            try:
                from agent.prompt_builder import clear_skills_system_prompt_cache
                clear_skills_system_prompt_cache(clear_snapshot=True)
            except Exception:
                pass
            # The protocol section is cached per (process, home) too (``bot_mode_probe._cached``),
            # and the rebuild below reaches it through that cache. Without this refresh a
            # protocol_version bump stamps the NEW epoch onto the OLD section text: the staleness
            # check then reports clean forever and the new lines never reach the bot. Refresh
            # through the SAME home the build resolves, so the cache the build reads is the one
            # cleared here.
            try:
                from agent.system_prompt import _agent_home
                from tools.bot_mode_probe import get_bot_mode_protocol_section
                get_bot_mode_protocol_section(_agent_home(agent), force_refresh=True)
            except Exception:
                pass
            agent._cached_system_prompt = agent._build_system_prompt(system_message)
'''


def _apply(source: str, edits) -> tuple[str, int]:
    """Apply each (old, new) pair once. Returns (source, 0) or (source, 2) on a bad anchor."""
    for old, new in edits:
        if source.count(old) != 1:
            print(f"anchor not unique or absent ({source.count(old)} matches): {old.splitlines()[0]!r}")
            return source, 2
        source = source.replace(old, new, 1)
    return source, 0


def _patch_probe() -> int:
    source = PROBE.read_text(encoding="utf-8")
    edits = []
    if CONST_MARKER not in source:
        edits.append((PROBE_HEADING_OLD, PROBE_HEADING_NEW))
    if CALL_MARKER not in source:
        edits.append((PROBE_CALLSITE_OLD, PROBE_CALLSITE_NEW))
    changed = bool(edits)
    if edits:
        source, rc = _apply(source, tuple(edits))
        if rc:
            return rc
    if VERSION_LINE_OLD in source:
        if source.count(VERSION_LINE_OLD) != 1:
            print(f"anchor not unique or absent: {VERSION_LINE_OLD!r}")
            return 2
        source = source.replace(VERSION_LINE_OLD, VERSION_LINE_NEW, 1)
        changed = True
    elif VERSION_LINE_NEW not in source:
        print(f"anchor not unique or absent: {VERSION_LINE_OLD!r}")
        return 2
    if changed:
        PROBE.write_text(source, encoding="utf-8")
        print(f"patched: {PROBE}")
    else:
        print(f"already patched: {PROBE}")
    return 0


def _patch_loop() -> int:
    source = LOOP.read_text(encoding="utf-8")
    if LOOP_MARKER in source:
        print(f"already patched: {LOOP}")
        return 0
    source, rc = _apply(source, ((LOOP_OLD, LOOP_NEW),))
    if rc:
        return rc
    LOOP.write_text(source, encoding="utf-8")
    print(f"patched: {LOOP}")
    return 0


def main() -> int:
    for path in (PROBE, LOOP):
        if not path.is_file():
            print(f"missing: {path}")
            return 1
    for step in (_patch_probe, _patch_loop):
        rc = step()
        if rc:
            return rc
    print("both edits in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
