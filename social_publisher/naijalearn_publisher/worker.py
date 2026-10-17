"""Queue worker: claim due approved posts, publish, retry with backoff, log every attempt."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .config import WorkerSettings
from .destinations.base import PublishError, Publisher

log = logging.getLogger("naijalearn_publisher")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RunSummary:
    published: int = 0
    retried: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def compute_backoff(attempt_no: int, base: int, cap: int, retry_after: int | None = None,
                    rng: Callable[[], float] = random.random) -> int:
    delay = min(cap, base * 2 ** max(attempt_no - 1, 0))
    delay *= 0.8 + 0.4 * rng()  # +/-20% jitter
    if retry_after:
        delay = max(delay, retry_after)
    return int(delay)


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def throttle_reason(store, destination: str, settings: WorkerSettings, now: datetime) -> str | None:
    """Spam-safety: cap posts per rolling 24h and keep a minimum gap between posts."""
    times = store.recent_published_times(destination, (now - timedelta(hours=24)).isoformat())
    if len(times) >= settings.max_per_day:
        return f"daily cap of {settings.max_per_day} reached"
    if times and (now - max(times)).total_seconds() < settings.min_interval_seconds:
        return f"minimum interval of {settings.min_interval_seconds}s since last post not reached"
    return None


def _record(store, post: Mapping[str, Any], started: datetime, finished: datetime, outcome: str, **extra) -> None:
    try:
        store.record_attempt(post_id=post["id"], attempt_no=int(post["attempts"]),
                             started_at=started.isoformat(), finished_at=finished.isoformat(),
                             outcome=outcome, **extra)
    except Exception:  # an audit-log failure must never undo a publish
        log.exception("Could not record attempt for post %s", post["id"])


def _process(store, publisher: Publisher, post: Mapping[str, Any], settings: WorkerSettings,
             clock, rng) -> str:
    secrets = publisher.secrets()
    attempt_no = int(post["attempts"])
    started = clock()
    try:
        result = publisher.publish(post)
    except PublishError as exc:
        message = redact(str(exc), secrets)
        retry = exc.retryable and attempt_no < int(post["max_attempts"])
        if retry:
            delay = compute_backoff(attempt_no, settings.retry_base_seconds, settings.retry_cap_seconds,
                                    exc.retry_after_seconds, rng)
            store.schedule_retry(post["id"], (clock() + timedelta(seconds=delay)).isoformat(), message)
            log.warning("Post %s attempt %d failed (retry in %ds): %s", post["id"], attempt_no, delay, message)
        else:
            store.mark_failed(post["id"], message)
            log.error("Post %s failed permanently after %d attempt(s): %s", post["id"], attempt_no, message)
        _record(store, post, started, clock(), "retryable_error" if exc.retryable else "permanent_error",
                http_status=exc.http_status, error_code=exc.code, error_subcode=exc.subcode,
                error_message=message, trace_id=exc.trace_id)
        return "retry" if retry else "failed"
    except Exception as exc:
        log.exception("Unexpected error publishing %s", post["id"])
        message = redact(f"Unexpected error ({type(exc).__name__}). Check the Page before requeueing.", secrets)
        store.mark_failed(post["id"], message)
        _record(store, post, started, clock(), "permanent_error", error_message=message)
        return "failed"

    try:
        store.mark_published(post["id"], result.external_id, result.permalink)
    except Exception:
        log.exception("Post %s IS LIVE as %s but the database update failed. Do not requeue it.",
                      post["id"], result.external_id)
        raise
    _record(store, post, started, clock(), "success", http_status=result.http_status,
            external_id=result.external_id)
    log.info("Published post %s as %s", post["id"], result.external_id)
    return "published"


def run_once(store, publishers: Mapping[str, Publisher], settings: WorkerSettings, *,
             clock: Callable[[], datetime] = utcnow, rng: Callable[[], float] = random.random) -> RunSummary:
    summary = RunSummary()
    for destination, publisher in publishers.items():
        for _ in range(settings.max_per_run):
            reason = throttle_reason(store, destination, settings, clock())
            if reason:
                summary.notes.append(f"{destination}: {reason}")
                break
            claimed = store.claim_due(destination, 1, settings.lock_timeout_minutes)
            if not claimed:
                break
            outcome = _process(store, publisher, claimed[0], settings, clock, rng)
            if outcome == "published":
                summary.published += 1
            elif outcome == "retry":
                summary.retried += 1
            else:
                summary.failed += 1
    return summary
