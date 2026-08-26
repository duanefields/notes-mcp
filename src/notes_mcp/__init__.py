"""Apple Notes MCP server."""

import os

# FastMCP checks PyPI for a newer version on startup and prints a banner.
# Neither is wanted here: this server exists to read a private notes archive,
# so it should not make an unrequested outbound request every time it starts,
# and on an unattended host that call is startup latency and one more thing to
# fail when the network is down. The banner *is* the network call -- run() ->
# log_server_banner() -> check_for_newer_version() -- so suppressing it is what
# actually stops the request; turning the update check off as well is belt and
# suspenders.
#
# This has to live here, above every import of fastmcp. `fastmcp.settings` is a
# pydantic-settings object built once at import time, so setting these any
# later is a silent no-op. Importing this package always runs before
# `notes_mcp.server` pulls in fastmcp.
#
# "off" is the only value that disables the update check: the setting is typed
# Literal["stable", "prerelease", "off"], and FASTMCP_CHECK_FOR_UPDATES=false
# raises a pydantic ValidationError at `import fastmcp` -- the server never
# starts. FASTMCP_SHOW_SERVER_BANNER really is a bool, so "false" is right
# there. setdefault, so an operator who wants them back can still ask.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
