"""Facebook Page publishing through Meta's official Pages API (Graph API).

Uses a Page access token with pages_manage_posts. No browser automation, no passwords.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..config import FacebookSettings
from ..http import HttpClient, TransportError, urllib_http
from .base import PublishError, PublishResult, Publisher

GRAPH_HOST = "https://graph.facebook.com"
# Graph error codes that are temporary: 1/2 unknown or temporary, 4/17/32/613 rate limits, 341 app limit.
TRANSIENT_CODES = frozenset({1, 2, 4, 17, 32, 341, 613})
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})
RATE_LIMIT_WAIT_SECONDS = 3600
DUPLICATE_CODE = 506


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FacebookPagePublisher(Publisher):
    name = "facebook_page"

    def __init__(self, settings: FacebookSettings, http: HttpClient = urllib_http, timeout: float = 30.0) -> None:
        self._settings = settings
        self._http = http
        self._timeout = timeout

    def secrets(self) -> tuple[str, ...]:
        return (self._settings.page_token,)

    def publish(self, post: Mapping[str, Any]) -> PublishResult:
        endpoint, form = self._build_request(post)
        form["access_token"] = self._settings.page_token
        url = f"{GRAPH_HOST}/{self._settings.graph_version}/{self._settings.page_id}/{endpoint}"
        try:
            response = self._http("POST", url, form=form, timeout=self._timeout)
        except TransportError as exc:
            if exc.ambiguous:
                raise PublishError(
                    "Network failure after the request may have been sent, so the post might be live. "
                    f"Check the Page before requeueing. ({exc})",
                    retryable=False,
                ) from None
            raise PublishError(f"Could not reach Facebook: {exc}", retryable=True) from None
        return self._parse(response)

    @staticmethod
    def _build_request(post: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
        kind = post.get("kind")
        message = post.get("message") or ""
        if kind == "text":
            return "feed", {"message": message}
        if kind == "link":
            if not post.get("link_url"):
                raise PublishError("Link post has no link_url", retryable=False)
            return "feed", {"message": message, "link": post["link_url"]}
        if kind == "photo":
            if not post.get("image_url"):
                raise PublishError("Photo post has no image_url", retryable=False)
            return "photos", {"caption": message, "url": post["image_url"]}
        raise PublishError(f"Unsupported post kind {kind!r}", retryable=False)

    @staticmethod
    def _parse(response) -> PublishResult:
        body = response.body if isinstance(response.body, dict) else {}
        error = body.get("error")
        if 200 <= response.status < 300 and not error:
            post_id = body.get("post_id") or body.get("id")
            if post_id:
                return PublishResult(str(post_id), f"https://www.facebook.com/{post_id}", response.status)
            raise PublishError(
                "Facebook returned success without a post id. Check the Page before requeueing.",
                retryable=False,
                http_status=response.status,
            )
        if not isinstance(error, dict):
            error = {}
        code = _to_int(error.get("code"))
        message = str(error.get("message") or response.text[:300] or f"HTTP {response.status}")
        if code == DUPLICATE_CODE:
            message += " (If an earlier attempt timed out, the post may already be live. Check the Page.)"
        retryable = bool(error.get("is_transient")) or code in TRANSIENT_CODES or response.status >= 500
        raise PublishError(
            message,
            retryable=retryable,
            http_status=response.status,
            code=code,
            subcode=_to_int(error.get("error_subcode")),
            trace_id=error.get("fbtrace_id"),
            retry_after_seconds=RATE_LIMIT_WAIT_SECONDS if code in RATE_LIMIT_CODES else None,
        )
