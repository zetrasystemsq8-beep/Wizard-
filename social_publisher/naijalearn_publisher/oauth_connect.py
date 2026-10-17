"""OAuth 2.0 authorization-code flow against Meta, run on your own machine.

You log in on facebook.com in your own browser. This tool never sees a password, and it never
writes the token to disk: it prints the Page ID and Page token once so you can store them as secrets.
"""
from __future__ import annotations

import hmac
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

from .config import ConfigError, _int, _required, graph_version
from .destinations.facebook_page import GRAPH_HOST
from .http import HttpClient, TransportError, urllib_http

REQUIRED_SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts")
OK_TASKS = {"CREATE_CONTENT", "MANAGE"}
CALLBACK_PATH = "/callback"


class OAuthError(RuntimeError):
    pass


def build_authorize_url(app_id: str, redirect_uri: str, state: str, version: str) -> str:
    query = urlencode({"client_id": app_id, "redirect_uri": redirect_uri, "state": state,
                       "scope": ",".join(REQUIRED_SCOPES), "response_type": "code"})
    return f"https://www.facebook.com/{version}/dialog/oauth?{query}"


def parse_callback(path: str, expected_state: str) -> str:
    query = parse_qs(urlparse(path).query)
    if "error" in query:
        raise OAuthError(query.get("error_description", query["error"])[0])
    state = query.get("state", [""])[0]
    if not hmac.compare_digest(state, expected_state):
        raise OAuthError("State mismatch (possible forged callback). Start again.")
    code = query.get("code", [""])[0]
    if not code:
        raise OAuthError("Callback contained no authorization code")
    return code


class _Server(HTTPServer):
    expected_state = ""
    code: str | None = None
    error: str | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        try:
            self.server.code = parse_callback(self.path, self.server.expected_state)
            status, text = 200, "Connected. You can close this tab and return to the terminal."
        except OAuthError as exc:
            self.server.error = str(exc)
            status, text = 400, f"Failed: {exc}"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, *args) -> None:  # keep the authorization code out of console logs
        pass


def _graph_get(http: HttpClient, version: str, path: str, params: dict) -> dict:
    try:
        resp = http("GET", f"{GRAPH_HOST}/{version}/{path}", query=params)
    except TransportError as exc:
        raise OAuthError(f"Cannot reach Facebook: {exc}") from None
    body = resp.body if isinstance(resp.body, dict) else {}
    if resp.status >= 400 or "error" in body:
        message = (body.get("error") or {}).get("message") or f"HTTP {resp.status}"
        raise OAuthError(f"Facebook rejected the request: {message}")
    return body


def exchange_for_page_token(http: HttpClient, *, version: str, app_id: str, app_secret: str,
                            redirect_uri: str, code: str, page_id: str | None):
    short = _graph_get(http, version, "oauth/access_token", {
        "client_id": app_id, "redirect_uri": redirect_uri, "client_secret": app_secret, "code": code})
    long_lived = _graph_get(http, version, "oauth/access_token", {
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
        "fb_exchange_token": short["access_token"]})
    accounts = _graph_get(http, version, "me/accounts", {
        "fields": "id,name,access_token,tasks", "limit": "100", "access_token": long_lived["access_token"]})
    pages = accounts.get("data") or []
    if page_id:
        pages = [p for p in pages if p.get("id") == page_id]
        if not pages:
            raise OAuthError(f"Page {page_id} not found. Your Facebook user needs a role on it with "
                             "'Create content' (or Manage) access, and you must tick that Page in the consent dialog.")
    elif len(pages) != 1:
        listing = "\n".join(f"  {p.get('id')}  {p.get('name')}" for p in pages) or "  (none)"
        raise OAuthError(f"Set FB_PAGE_ID to one of these Pages and run again:\n{listing}")
    return pages[0]


def run_connect(env: Mapping[str, str], *, http: HttpClient = urllib_http, open_browser: bool = True,
                out: Callable[[str], None] = print, timeout_seconds: int = 300) -> int:
    try:
        app_id, app_secret = _required(env, "FB_APP_ID"), _required(env, "FB_APP_SECRET")
        version = graph_version(env)
        port = _int(env, "OAUTH_PORT", 8765, 1)
    except ConfigError as exc:
        out(f"error: {exc}")
        return 2
    redirect_uri = f"http://localhost:{port}{CALLBACK_PATH}"
    state = secrets.token_urlsafe(24)
    server = _Server(("127.0.0.1", port), _Handler)
    server.expected_state = state
    server.timeout = 5
    url = build_authorize_url(app_id, redirect_uri, state, version)
    out(f"Add this exact URL under Facebook Login > Valid OAuth Redirect URIs: {redirect_uri}")
    out("Opening Facebook's login page. If nothing opens, visit:\n" + url)
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + timeout_seconds
    try:
        while server.code is None and server.error is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if server.code is None:
        out(f"error: {server.error or 'Timed out waiting for the Facebook callback'}")
        return 1
    try:
        page = exchange_for_page_token(http, version=version, app_id=app_id, app_secret=app_secret,
                                       redirect_uri=redirect_uri, code=server.code,
                                       page_id=(env.get("FB_PAGE_ID") or "").strip() or None)
    except OAuthError as exc:
        out(f"error: {exc}")
        return 1
    if not OK_TASKS & set(page.get("tasks") or []):
        out("warning: this user lacks CREATE_CONTENT/MANAGE on the Page; publishing will fail.")
    out(f"\nConnected Page: {page['name']}\nFB_PAGE_ID={page['id']}\nFB_PAGE_ACCESS_TOKEN={page['access_token']}")
    out("\nStore both as GitHub Actions secrets, e.g.:\n  gh secret set FB_PAGE_ID\n  gh secret set FB_PAGE_ACCESS_TOKEN\n"
        "Then clear your terminal history/scrollback. Nothing was written to disk.")
    return 0
