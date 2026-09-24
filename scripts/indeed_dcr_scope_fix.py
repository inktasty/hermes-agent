#!/usr/bin/env python3
"""Re-apply the local Indeed MCP OAuth fix to Hermes' MCP OAuth provider.

Why: the MCP Python SDK puts the discovery-advertised ``scope`` into the dynamic client
registration (DCR) body. Indeed's authorization server rejects that field outright
(``400 invalid_client_metadata: Requested scopes are invalid or not supported``) even though it
advertises those same scopes and accepts them on the authorization request, so every Indeed MCP
login dies before the browser step. ``tools/mcp_oauth_provider.py`` drops ``scope`` from the
registration request for that authorization server only.

The edit lives in Hermes source, so `hermes update` can stash it
(``updates.non_interactive_local_changes: stash``). Run this after any update:

    python3 scripts/indeed_dcr_scope_fix.py

Exit 0  = fix is in place (patched now, or already there).
Exit 1  = source file missing.
Exit 2  = anchors no longer match; upstream reshaped the code, re-derive the edit by hand.
"""

from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path(__file__).resolve().parent.parent / "tools" / "mcp_oauth_provider.py"

MARKER = "_DCR_SCOPE_REJECTING_HOSTS"

IMPORT_OLD = """import logging
import re
from typing import TYPE_CHECKING, Any
"""
IMPORT_NEW = """import json
import logging
import re
from typing import TYPE_CHECKING, Any
"""

FUNC_OLD = """    if "user-agent" not in request.headers:
        request.headers["User-Agent"] = DEFAULT_AUTH_REQUEST_USER_AGENT
    return request
"""
FUNC_NEW = '''    if "user-agent" not in request.headers:
        request.headers["User-Agent"] = DEFAULT_AUTH_REQUEST_USER_AGENT
    return request


# Authorization servers that reject a dynamic client registration whose metadata carries ``scope``
# ("invalid_client_metadata: Requested scopes are invalid or not supported" - Indeed, whose
# registration endpoint rejects the very scopes its own metadata advertises and its authorization
# endpoint accepts). RFC 7591 makes ``scope`` optional at registration, so the field is dropped for
# that registration endpoint only; the authorization URL the SDK builds still asks for the
# discovery-advertised scopes, which is where the grant is decided.
_DCR_SCOPE_REJECTING_HOSTS = frozenset({"secure.indeed.com"})


def drop_rejected_registration_scope(request):
    """Drop ``scope`` from a dynamic client registration request for authorization servers that
    reject it; every other SDK-built request (metadata fetch, token exchange) passes through."""
    try:
        parts = urlsplit(str(request.url))
        if (request.method.upper() != "POST"
                or (parts.hostname or "").lower() not in _DCR_SCOPE_REJECTING_HOSTS
                or not parts.path.rstrip("/").endswith("/register")):
            return request
        payload = json.loads(request.content.decode("utf-8"))
        if not isinstance(payload, dict) or "scope" not in payload:
            return request
        payload.pop("scope")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        # Mutate in place: the request was built by whatever HTTP library the SDK is bound to
        # (httpx2 in this install), so rebuilding it here would need that same library.
        request.stream = type(request.stream)(data)
        request._content = data  # noqa: SLF001 - httpx caches the body for ``.content``
        if "content-length" in request.headers:
            request.headers["Content-Length"] = str(len(data))
        logger.info("MCP OAuth: dropped request-scope from client registration with %s "
                    "(that authorization server rejects the field)", parts.hostname)
        return request
    except Exception:  # the fixup must never be the thing that breaks a login
        logger.debug("MCP OAuth: could not adjust the client registration request", exc_info=True)
        return request
'''

CALLSITE_OLD = """                    if out is not request:
                        stamp_default_user_agent(out)
"""
CALLSITE_NEW = """                    if out is not request:
                        stamp_default_user_agent(out)
                        out = drop_rejected_registration_scope(out)
"""


def main() -> int:
    if not TARGET.is_file():
        print(f"missing: {TARGET}")
        return 1

    source = TARGET.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"already patched: {TARGET}")
        return 0

    edits = ((IMPORT_OLD, IMPORT_NEW), (FUNC_OLD, FUNC_NEW), (CALLSITE_OLD, CALLSITE_NEW))
    for old, new in edits:
        if source.count(old) != 1:
            print(f"anchor not unique or absent ({source.count(old)} matches): {old.splitlines()[0]!r}")
            return 2
        source = source.replace(old, new, 1)

    TARGET.write_text(source, encoding="utf-8")
    print(f"patched: {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
