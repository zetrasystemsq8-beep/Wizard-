"""Tiny stdlib HTTP layer. Injectable so every network call can be faked in tests."""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any
    text: str


class TransportError(Exception):
    """Network failure. ambiguous=True means the server may already have processed the request."""

    def __init__(self, message: str, *, ambiguous: bool) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


HttpClient = Callable[..., HttpResponse]


def _build(status: int, raw: bytes) -> HttpResponse:
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text) if text.strip() else None
    except ValueError:
        body = None
    return HttpResponse(status=status, body=body, text=text)


def urllib_http(method, url, *, headers=None, form=None, json_body=None, query=None, timeout=30.0):
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = None
    hdrs = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return _build(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return _build(exc.code, exc.read())
    except urllib.error.URLError as exc:
        reason = exc.reason
        before_send = isinstance(reason, (socket.gaierror, ConnectionRefusedError))
        raise TransportError(f"{type(reason).__name__}: {reason}", ambiguous=not before_send) from None
    except OSError as exc:  # includes timeouts and connection resets
        raise TransportError(f"{type(exc).__name__}: {exc}", ambiguous=True) from None
