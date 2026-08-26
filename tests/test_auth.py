"""The auth layer is a port, so these tests pin the parts a port gets wrong.

The flow itself is exercised against a running server; what is checked here is
configuration, which is where a bad port fails silently rather than loudly.
"""

import pytest

from notes_mcp import auth


def test_no_auth_by_default(monkeypatch):
    monkeypatch.delenv("NOTES_MCP_AUTH", raising=False)
    assert auth.build_auth() is None


def test_unknown_mode_is_refused(monkeypatch):
    monkeypatch.setenv("NOTES_MCP_AUTH", "basic")
    with pytest.raises(ValueError, match="basic"):
        auth.build_auth()


def test_password_mode_requires_a_password(monkeypatch):
    monkeypatch.setenv("NOTES_MCP_AUTH", "password")
    monkeypatch.delenv("NOTES_MCP_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="NOTES_MCP_PASSWORD"):
        auth.build_auth()


def test_password_mode_requires_a_base_url(monkeypatch):
    """A wrong base URL produces a 401 that does not explain itself, so the
    server refuses to start without one rather than half-working."""
    monkeypatch.setenv("NOTES_MCP_AUTH", "password")
    monkeypatch.setenv("NOTES_MCP_PASSWORD", "x" * 20)
    monkeypatch.delenv("NOTES_MCP_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="NOTES_MCP_BASE_URL"):
        auth.build_auth()


def test_password_mode_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTES_MCP_AUTH", "password")
    monkeypatch.setenv("NOTES_MCP_PASSWORD", "x" * 20)
    monkeypatch.setenv("NOTES_MCP_BASE_URL", "https://notes.example.com")
    monkeypatch.setenv("NOTES_MCP_STATE_DIR", str(tmp_path))
    assert auth.build_auth() is not None


def test_scope_did_not_come_along_from_the_source_project():
    """The scope is embedded in every issued token and every client
    registration. Shipping imessage:manage here would work, and would be
    wrong forever -- changing it later forces every client to authorize
    again."""
    assert auth.SCOPE == "notes:manage"
