"""Supabase (PostgREST) access for the queue. Uses the service key, which must never ship in the app."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from .http import HttpClient, urllib_http


class StoreError(RuntimeError):
    pass


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore:
    def __init__(self, url: str, service_key: str, http: HttpClient = urllib_http) -> None:
        self._base = url.rstrip("/") + "/rest/v1"
        self._http = http
        self._headers = {"apikey": service_key}
        if service_key.startswith("eyJ"):  # legacy JWT keys also go in Authorization; sb_secret_ keys do not
            self._headers["Authorization"] = f"Bearer {service_key}"

    def _call(self, method: str, path: str, *, query=None, json_body=None, prefer: str | None = None):
        headers = dict(self._headers)
        if prefer:
            headers["Prefer"] = prefer
        resp = self._http(method, f"{self._base}/{path}", headers=headers, query=query, json_body=json_body)
        if resp.status >= 400:
            raise StoreError(f"Supabase {method} {path} failed: HTTP {resp.status}: {resp.text[:300]}")
        return resp.body

    def _patch(self, post_id: str, fields: dict[str, Any], only_status: str | None = None) -> list[dict]:
        query = {"id": f"eq.{post_id}"}
        if only_status:
            query["status"] = f"eq.{only_status}"
        return list(self._call("PATCH", "social_posts", query=query, json_body=fields,
                               prefer="return=representation") or [])

    # --- worker operations -------------------------------------------------
    def claim_due(self, destination: str, limit: int, lock_timeout_minutes: int) -> list[dict]:
        rows = self._call("POST", "rpc/social_claim_due_posts", json_body={
            "p_destination": destination, "p_limit": limit, "p_lock_timeout_minutes": lock_timeout_minutes})
        return list(rows or [])

    def mark_published(self, post_id: str, external_id: str, permalink: str | None) -> None:
        self._patch(post_id, {"status": "published", "published_at": _now(), "external_id": external_id,
                              "permalink": permalink, "last_error": None, "locked_at": None})

    def schedule_retry(self, post_id: str, next_attempt_at: str, error: str) -> None:
        self._patch(post_id, {"status": "approved", "next_attempt_at": next_attempt_at,
                              "last_error": error, "locked_at": None}, only_status="publishing")

    def mark_failed(self, post_id: str, error: str) -> None:
        self._patch(post_id, {"status": "failed", "last_error": error, "locked_at": None},
                    only_status="publishing")

    def record_attempt(self, **fields: Any) -> None:
        self._call("POST", "social_post_attempts", json_body=fields, prefer="return=minimal")

    def recent_published_times(self, destination: str, since_iso: str) -> list[datetime]:
        rows = self._call("GET", "social_posts", query={
            "select": "published_at", "destination": f"eq.{destination}", "status": "eq.published",
            "published_at": f"gte.{since_iso}", "order": "published_at.desc"})
        return [parse_dt(r["published_at"]) for r in rows or [] if r.get("published_at")]

    # --- CLI operations ----------------------------------------------------
    def insert_post(self, fields: dict[str, Any]) -> dict:
        rows = self._call("POST", "social_posts", json_body=fields, prefer="return=representation")
        return rows[0]

    def set_status(self, post_id: str, from_statuses: Sequence[str], to_status: str,
                   extra: dict[str, Any] | None = None) -> dict | None:
        query = {"id": f"eq.{post_id}", "status": f"in.({','.join(from_statuses)})"}
        rows = self._call("PATCH", "social_posts", query=query,
                          json_body={"status": to_status, **(extra or {})}, prefer="return=representation")
        return rows[0] if rows else None

    def get_post(self, post_id: str) -> dict | None:
        rows = self._call("GET", "social_posts", query={"select": "*", "id": f"eq.{post_id}"})
        return rows[0] if rows else None

    def list_posts(self, status: str | None = None, limit: int = 30) -> list[dict]:
        query = {"select": "*", "order": "scheduled_at.desc", "limit": str(limit)}
        if status:
            query["status"] = f"eq.{status}"
        return list(self._call("GET", "social_posts", query=query) or [])

    def list_attempts(self, post_id: str | None = None, limit: int = 30) -> list[dict]:
        query = {"select": "*", "order": "id.desc", "limit": str(limit)}
        if post_id:
            query["post_id"] = f"eq.{post_id}"
        return list(self._call("GET", "social_post_attempts", query=query) or [])

    def find_duplicates(self, destination: str, content_hash: str, since_iso: str) -> list[dict]:
        return list(self._call("GET", "social_posts", query={
            "select": "id,status,scheduled_at", "destination": f"eq.{destination}",
            "content_hash": f"eq.{content_hash}", "status": "neq.cancelled",
            "created_at": f"gte.{since_iso}"}) or [])

    def ping(self) -> None:
        self._call("GET", "social_posts", query={"select": "id", "limit": "1"})
