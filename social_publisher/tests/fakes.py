from __future__ import annotations

from datetime import datetime, timedelta, timezone

from naijalearn_publisher.destinations.base import PublishError, PublishResult, Publisher
from naijalearn_publisher.http import HttpResponse
from naijalearn_publisher.store import parse_dt

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


class FakeHttp:
    """Queue of canned responses (or exceptions). Records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body, status=200):
    return HttpResponse(status, body, str(body))


class FakePublisher(Publisher):
    name = "facebook_page"

    def __init__(self, *outcomes, secret="SECRET-TOKEN"):
        self.outcomes = list(outcomes)
        self.published = []
        self._secret = secret

    def secrets(self):
        return (self._secret,)

    def publish(self, post):
        outcome = self.outcomes.pop(0) if self.outcomes else PublishResult("page_1", "https://www.facebook.com/page_1", 200)
        if isinstance(outcome, Exception):
            raise outcome
        self.published.append(post["id"])
        return outcome


class InMemoryStore:
    """Mirrors the SQL semantics of SupabaseStore closely enough to test the worker."""

    def __init__(self, clock):
        self.clock = clock
        self.posts = {}
        self.attempts = []

    def add(self, post_id="p1", status="approved", scheduled_offset=-60, **over):
        row = {"id": post_id, "destination": "facebook_page", "kind": "text", "message": f"msg {post_id}",
               "link_url": None, "image_url": None, "status": status,
               "scheduled_at": self.clock() + timedelta(seconds=scheduled_offset),
               "next_attempt_at": self.clock() - timedelta(days=1), "attempts": 0, "max_attempts": 3,
               "published_at": None, "last_error": None, "external_id": None}
        row.update(over)
        self.posts[post_id] = row
        return row

    def claim_due(self, destination, limit, lock_timeout_minutes):
        now = self.clock()
        due = sorted((p for p in self.posts.values() if p["destination"] == destination and p["status"] == "approved"
                      and p["scheduled_at"] <= now and p["next_attempt_at"] <= now), key=lambda p: p["scheduled_at"])
        claimed = due[:limit]
        for p in claimed:
            p["status"] = "publishing"
            p["attempts"] += 1
        return [dict(p) for p in claimed]

    def mark_published(self, post_id, external_id, permalink):
        p = self.posts[post_id]
        p.update(status="published", published_at=self.clock(), external_id=external_id, last_error=None)

    def schedule_retry(self, post_id, next_attempt_at, error):
        self.posts[post_id].update(status="approved", next_attempt_at=parse_dt(next_attempt_at), last_error=error)

    def mark_failed(self, post_id, error):
        self.posts[post_id].update(status="failed", last_error=error)

    def record_attempt(self, **fields):
        self.attempts.append(fields)

    def recent_published_times(self, destination, since_iso):
        since = parse_dt(since_iso)
        return [p["published_at"] for p in self.posts.values()
                if p["status"] == "published" and p["destination"] == destination and p["published_at"] >= since]


def retryable(message="temporary", **kw):
    return PublishError(message, retryable=True, **kw)


def permanent(message="nope", **kw):
    return PublishError(message, retryable=False, **kw)
