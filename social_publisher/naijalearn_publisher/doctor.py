"""`doctor`: verify Supabase access, Page token validity, expiry and granted permissions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping

from .config import ConfigError, graph_version, load_facebook_settings, load_supabase_settings
from .destinations.facebook_page import GRAPH_HOST
from .http import HttpClient, TransportError, urllib_http
from .store import SupabaseStore

REQUIRED_SCOPES = ("pages_manage_posts",)


def run_doctor(env: Mapping[str, str], http: HttpClient = urllib_http, out: Callable[[str], None] = print) -> int:
    problems = 0

    def ok(msg: str) -> None:
        out(f"[ok]   {msg}")

    def bad(msg: str) -> None:
        nonlocal problems
        problems += 1
        out(f"[FAIL] {msg}")

    try:
        sb = load_supabase_settings(env)
        SupabaseStore(sb.url, sb.service_key, http).ping()
        ok("Supabase reachable and queue tables exist")
    except Exception as exc:
        bad(f"Supabase: {exc}")

    try:
        fb = load_facebook_settings(env)
    except ConfigError as exc:
        bad(str(exc))
        return 1

    try:
        resp = http("GET", f"{GRAPH_HOST}/{fb.graph_version}/{fb.page_id}",
                    query={"fields": "id,name", "access_token": fb.page_token})
        if resp.status == 200 and isinstance(resp.body, dict) and resp.body.get("id") == fb.page_id:
            ok(f"Page token can read Page {fb.page_id} ({resp.body.get('name')})")
        else:
            err = (resp.body or {}).get("error", {}) if isinstance(resp.body, dict) else {}
            bad(f"Page lookup failed: HTTP {resp.status} {err.get('message', resp.text[:200])}")
    except TransportError as exc:
        bad(f"Cannot reach Facebook: {exc}")

    app_id, app_secret = (env.get("FB_APP_ID") or "").strip(), (env.get("FB_APP_SECRET") or "").strip()
    if not (app_id and app_secret):
        out("[skip] Set FB_APP_ID and FB_APP_SECRET to also check token expiry and scopes")
    else:
        try:
            resp = http("GET", f"{GRAPH_HOST}/{graph_version(env)}/debug_token",
                        query={"input_token": fb.page_token, "access_token": f"{app_id}|{app_secret}"})
            data = (resp.body or {}).get("data", {}) if isinstance(resp.body, dict) else {}
            if not data.get("is_valid"):
                bad("Token is not valid. Run `connect` again.")
            else:
                ok("Token is valid")
                scopes = set(data.get("scopes") or [])
                missing = [s for s in REQUIRED_SCOPES if s not in scopes]
                if missing:
                    bad(f"Token is missing permissions: {', '.join(missing)}")
                else:
                    ok("Token has pages_manage_posts")
                expires = int(data.get("expires_at") or 0)
                if expires:
                    when = datetime.fromtimestamp(expires, timezone.utc)
                    out(f"[info] Token expires {when.isoformat()}")
                else:
                    out("[info] Token has no expiry (it can still be revoked)")
        except TransportError as exc:
            bad(f"Cannot reach Facebook: {exc}")
    return 1 if problems else 0
