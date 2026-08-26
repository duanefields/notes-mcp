"""Optional authentication for the HTTP transport.

Selected with ``NOTES_MCP_AUTH``:

``none`` (default)
    No authentication. Correct for stdio and for a server bound to localhost or a
    trusted LAN.

``password``
    A self-contained OAuth 2.1 authorization server guarded by a single shared
    password. Intended for a personal single-user deployment exposed to the public
    internet, where an MCP client (such as a Claude connector) can only be handed a
    bare URL and must discover authentication for itself.

The password mode implements only the parts of OAuth that are specific to it: a
login screen and the credential check. PKCE verification, dynamic client
registration, discovery metadata, redirect-URI validation and the ``401`` challenge
are all provided by FastMCP and the MCP SDK.

This is a port of the provider in things-mcp, by way of dav-mcp and
imessage-mcp. It is domain-independent apart from the scope name, the env var
prefix, and the title on the login page. Fixes belong in the others as well as
here -- there are now four copies.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from fastmcp.server.auth.auth import (
    AuthProvider,
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)

logger = logging.getLogger(__name__)

# Server-internal and never displayed.
#
# Changing it is not free: the scope is embedded in every issued token and in
# each client's registration, and `required_scopes` rejects a token that does
# not carry it. Every client therefore has to authorize again. Do not change
# this casually.
SCOPE = "notes:manage"

AUTH_CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
LOGIN_TXN_TTL_SECONDS = 10 * 60

# Delay added to a failed login before responding. A shared password is expected to
# be high-entropy, so this is not the primary defense; it exists to keep a scripted
# guessing attempt from being cheap.
FAILED_LOGIN_DELAY_SECONDS = 1.0


class PasswordOAuthProvider(OAuthProvider):
    """An OAuth authorization server whose only credential is a shared password.

    ``authorize`` does not mint a code directly. It parks the request and redirects
    to ``/login``, which renders a password form; the code is issued only once that
    form is submitted correctly. This is the same interstitial shape FastMCP's own
    ``OAuthProxy`` uses for its consent screen.

    Registered clients and tokens are persisted so that a restart does not force
    every client to re-authorize. Authorization codes and in-flight login
    transactions are deliberately kept in memory only -- both are short-lived, and
    losing them costs at most one retry of a flow that is already on screen.
    """

    def __init__(
        self,
        *,
        password: str,
        base_url: AnyHttpUrl | str,
        state_path: Path | None = None,
    ):
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(
                # Claude and other MCP clients register themselves at connect time;
                # without this there is no /register endpoint and registration fails
                # with an opaque invalid_client.
                enabled=True,
                valid_scopes=[SCOPE],
                default_scopes=[SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        )

        if not password:
            raise ValueError("password must not be empty")

        self._password = password
        self._state_path = state_path

        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}

        # Ephemeral, by design. See the class docstring.
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self._pending_logins: dict[str, tuple[str, AuthorizationParams, float]] = {}

        self._load_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            logger.warning(
                "Could not read auth state at %s; starting empty. "
                "Existing clients will need to re-authorize.",
                self._state_path,
            )
            return

        for client in raw.get("clients", []):
            try:
                info = OAuthClientInformationFull.model_validate(client)
            except ValueError:
                continue
            if info.client_id:
                self.clients[info.client_id] = info

        now = time.time()
        for token in raw.get("access_tokens", []):
            try:
                obj = AccessToken.model_validate(token)
            except ValueError:
                continue
            if obj.expires_at is None or obj.expires_at > now:
                self.access_tokens[obj.token] = obj

        for token in raw.get("refresh_tokens", []):
            try:
                obj = RefreshToken.model_validate(token)
            except ValueError:
                continue
            if obj.expires_at is None or obj.expires_at > now:
                self.refresh_tokens[obj.token] = obj

        logger.info(
            "Loaded auth state: %d client(s), %d access token(s), %d refresh token(s)",
            len(self.clients),
            len(self.access_tokens),
            len(self.refresh_tokens),
        )

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        payload: dict[str, Any] = {
            "clients": [c.model_dump(mode="json") for c in self.clients.values()],
            "access_tokens": [
                t.model_dump(mode="json") for t in self.access_tokens.values()
            ],
            "refresh_tokens": [
                t.model_dump(mode="json") for t in self.refresh_tokens.values()
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename so a crash mid-write cannot truncate existing state.
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.chmod(0o600)
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.warning(
                "Could not persist auth state to %s: %s", self._state_path, exc
            )

    # ------------------------------------------------------------------
    # Client registration
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")
        self.clients[client_info.client_id] = client_info
        self._save_state()
        logger.info(
            "Registered OAuth client %s (%s)",
            client_info.client_id,
            client_info.client_name or "unnamed",
        )

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the user to the password form.

        The framework turns the returned URL into a redirect. No authorization code
        exists until the password has been checked in ``_handle_login_submit``.
        """
        if client.client_id is None or client.client_id not in self.clients:
            raise AuthorizeError(
                error="unauthorized_client",
                error_description="Client is not registered.",
            )

        self._expire_pending_logins()

        txn = secrets.token_urlsafe(32)
        self._pending_logins[txn] = (client.client_id, params, time.time())
        return f"{self._base()}/login?txn={txn}"

    def _base(self) -> str:
        return str(self.base_url).rstrip("/")

    def _expire_pending_logins(self) -> None:
        cutoff = time.time() - LOGIN_TXN_TTL_SECONDS
        for txn, (_, _, started) in list(self._pending_logins.items()):
            if started < cutoff:
                del self._pending_logins[txn]

    def _issue_code(self, client_id: str, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        scopes = params.scopes if params.scopes is not None else [SCOPE]
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=client_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=scopes,
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            code_challenge=params.code_challenge,
        )
        return code

    # ------------------------------------------------------------------
    # Login interstitial
    # ------------------------------------------------------------------

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        routes.append(Route("/login", self._handle_login, methods=["GET", "POST"]))
        return routes

    async def _handle_login(self, request: Request) -> Response:
        if request.method == "POST":
            return await self._handle_login_submit(request)
        txn = request.query_params.get("txn", "")
        if txn not in self._pending_logins:
            return HTMLResponse(_login_page(txn="", error=_EXPIRED), status_code=400)
        return HTMLResponse(_login_page(txn=txn))

    async def _handle_login_submit(self, request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        supplied = str(form.get("password", ""))

        pending = self._pending_logins.get(txn)
        if pending is None:
            return HTMLResponse(_login_page(txn="", error=_EXPIRED), status_code=400)

        if not secrets.compare_digest(supplied, self._password):
            client_ip = request.headers.get(
                "cf-connecting-ip", request.client.host if request.client else "unknown"
            )
            logger.warning("Failed login attempt from %s", client_ip)
            # Deliberately keeps the transaction alive so a typo is recoverable.
            await anyio.sleep(FAILED_LOGIN_DELAY_SECONDS)
            return HTMLResponse(
                _login_page(txn=txn, error="Incorrect password."), status_code=401
            )

        del self._pending_logins[txn]
        client_id, params, _ = pending
        code = self._issue_code(client_id, params)
        logger.info("Login succeeded; issuing authorization code to %s", client_id)
        return RedirectResponse(
            construct_redirect_uri(
                str(params.redirect_uri), code=code, state=params.state
            ),
            status_code=302,
        )

    # ------------------------------------------------------------------
    # Token exchange
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            del self.auth_codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.auth_codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Authorization code already used.")
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required.")
        return self._issue_tokens(client.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at is not None and token.expires_at < time.time():
            del self.refresh_tokens[refresh_token]
            self._save_state()
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes).issubset(set(refresh_token.scopes)):
            raise TokenError(
                "invalid_scope",
                "Requested scopes exceed those authorized by the refresh token.",
            )
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required.")

        # Rotate: the presented refresh token is single-use.
        self.refresh_tokens.pop(refresh_token.token, None)
        return self._issue_tokens(client.client_id, scopes or refresh_token.scopes)

    def _issue_tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = time.time()

        self.access_tokens[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL_SECONDS),
        )
        self.refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL_SECONDS),
        )
        self._prune_expired()
        self._save_state()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def _prune_expired(self) -> None:
        now = time.time()
        for store in (self.access_tokens, self.refresh_tokens):
            for key, token in list(store.items()):
                if token.expires_at is not None and token.expires_at < now:
                    del store[key]

    # ------------------------------------------------------------------
    # Token verification and revocation
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        obj = self.access_tokens.get(token)
        if obj is None:
            return None
        if obj.expires_at is not None and obj.expires_at < time.time():
            del self.access_tokens[token]
            self._save_state()
            return None
        return obj

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)
        self._save_state()


_EXPIRED = "This sign-in link has expired. Start the connection again from your client."


def _login_page(*, txn: str, error: str | None = None) -> str:
    """Render the password form.

    ``txn`` is an unguessable server-issued handle for the parked authorization
    request, so echoing it back in a hidden field also serves as the CSRF token.
    """
    banner = f'<p class="error">{error}</p>' if error else ""
    form = (
        ""
        if not txn
        else f"""
      <form method="post">
        <input type="hidden" name="txn" value="{txn}">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password"
               autofocus required>
        <button type="submit">Sign in</button>
      </form>"""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in - Apple Notes MCP</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; margin: 0; padding: 1.5rem;
    background: #f6f6f7; color: #17171a;
  }}
  main {{
    width: 100%; max-width: 22rem; padding: 2rem;
    background: #fff; border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,.1), 0 8px 24px rgba(0,0,0,.06);
  }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.25rem; }}
  p.sub {{ margin: 0 0 1.5rem; color: #6b6b73; font-size: .875rem; }}
  label {{ display: block; font-size: .8125rem; font-weight: 600; margin-bottom: .375rem; }}
  input {{
    width: 100%; box-sizing: border-box; padding: .625rem .75rem; font-size: 1rem;
    border: 1px solid #d3d3d8; border-radius: 8px; background: #fff; color: inherit;
  }}
  input:focus {{ outline: 2px solid #4a6cf7; outline-offset: 1px; border-color: transparent; }}
  button {{
    width: 100%; margin-top: 1rem; padding: .625rem; font-size: 1rem; font-weight: 600;
    color: #fff; background: #4a6cf7; border: 0; border-radius: 8px; cursor: pointer;
  }}
  button:hover {{ background: #3f5de0; }}
  p.error {{
    margin: 0 0 1rem; padding: .625rem .75rem; font-size: .875rem;
    color: #8c1d18; background: #fdeceb; border-radius: 8px;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #17171a; color: #ececf1; }}
    main {{ background: #212125; box-shadow: none; }}
    p.sub {{ color: #9b9ba4; }}
    input {{ background: #2b2b31; border-color: #3d3d45; }}
    p.error {{ color: #ffb4ab; background: #3b1512; }}
  }}
</style>
</head>
<body>
  <main>
    <h1>Apple Notes MCP</h1>
    <p class="sub">Enter the server password to authorize this client.</p>
    {banner}{form}
  </main>
</body>
</html>"""


def build_auth() -> AuthProvider | None:
    """Construct the auth provider for the HTTP transport from the environment.

    Returns ``None`` when authentication is disabled, which is the default and
    leaves the server usable over stdio or on a trusted localhost bind.
    """
    mode = os.environ.get("NOTES_MCP_AUTH", "none").strip().lower()

    if mode in ("", "none"):
        return None

    if mode != "password":
        raise ValueError(
            f"Unknown NOTES_MCP_AUTH mode {mode!r}. Supported values: none, password."
        )

    password = os.environ.get("NOTES_MCP_PASSWORD", "")
    if not password:
        raise ValueError(
            "NOTES_MCP_AUTH=password requires NOTES_MCP_PASSWORD to be set."
        )

    base_url = os.environ.get("NOTES_MCP_BASE_URL", "").strip()
    if not base_url:
        raise ValueError(
            "NOTES_MCP_AUTH=password requires NOTES_MCP_BASE_URL, the public URL "
            "clients reach this server on (for example https://notes.example.com). "
            "It becomes the OAuth issuer and must match exactly, including scheme."
        )

    state_dir = os.environ.get("NOTES_MCP_STATE_DIR", "").strip()
    state_path = (
        Path(state_dir).expanduser() if state_dir else Path.home() / ".notes-mcp"
    ) / "oauth-state.json"

    return PasswordOAuthProvider(
        password=password,
        base_url=base_url.rstrip("/"),
        state_path=state_path,
    )
