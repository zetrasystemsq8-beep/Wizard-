"""Command line: add / approve / cancel / requeue / list / history / run / doctor / connect."""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import ConfigError, load_supabase_settings, load_worker_settings
from .destinations import UnsupportedDestination, build_all_publishers, supported_destinations
from .store import StoreError, SupabaseStore
from .worker import run_once

MAX_MESSAGE_CHARS = 63000


def parse_when(text: str) -> datetime:
    try:
        when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Could not parse time {text!r}. Use ISO 8601, e.g. 2026-10-20T09:00:00+01:00") from None
    if when.tzinfo is None:
        raise ValueError("Time must include a UTC offset, e.g. 2026-10-20T09:00:00+01:00 for Nigeria (WAT)")
    return when.astimezone(timezone.utc)


def _check_https(label: str, url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} must be a public https:// URL")
    return url


def content_hash(destination: str, message: str, link_url: str | None, image_url: str | None) -> str:
    normalised = " ".join(message.split()).lower()
    return hashlib.sha256("\n".join([destination, normalised, link_url or "", image_url or ""]).encode()).hexdigest()


def build_post_fields(*, message: str, when: datetime, link_url: str | None = None,
                      image_url: str | None = None, destination: str = "facebook_page",
                      max_attempts: int = 5) -> dict:
    if destination not in supported_destinations():
        raise UnsupportedDestination(f"Unsupported destination {destination!r}. "
                                     f"Supported: {', '.join(supported_destinations())}")
    message = message.strip()
    if not message:
        raise ValueError("Message must not be empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Message is longer than {MAX_MESSAGE_CHARS} characters")
    if link_url and image_url:
        raise ValueError("A post can have a link or an image, not both")
    if link_url:
        _check_https("--link", link_url)
    if image_url:
        _check_https("--image-url", image_url)
    kind = "link" if link_url else "photo" if image_url else "text"
    return {"destination": destination, "kind": kind, "message": message, "link_url": link_url,
            "image_url": image_url, "content_hash": content_hash(destination, message, link_url, image_url),
            "scheduled_at": when.isoformat(), "max_attempts": max_attempts, "status": "draft"}


def _store(env=None) -> SupabaseStore:
    settings = load_supabase_settings(env)
    return SupabaseStore(settings.url, settings.service_key)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(text: str, width: int = 50) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _cmd_add(args) -> int:
    message = args.message if args.message is not None else Path(args.message_file).read_text(encoding="utf-8")
    fields = build_post_fields(message=message, when=parse_when(args.at), link_url=args.link,
                               image_url=args.image_url, destination=args.destination,
                               max_attempts=args.max_attempts)
    store = _store()
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    duplicates = store.find_duplicates(fields["destination"], fields["content_hash"], since)
    if duplicates and not args.allow_duplicate:
        print(f"error: identical content was already queued in the last 30 days ({duplicates[0]['id']}). "
              "Facebook rejects duplicate posts. Edit the text or pass --allow-duplicate.", file=sys.stderr)
        return 2
    post = store.insert_post(fields)
    if args.approve:
        store.set_status(post["id"], ["draft"], "approved", {"approved_at": _now_iso()})
    print(f"{post['id']}  {'approved' if args.approve else 'draft'}  {fields['scheduled_at']}")
    return 0


def _cmd_transition(args, from_statuses, to_status, extra=None, note: str = "") -> int:
    row = _store().set_status(args.id, from_statuses, to_status, extra)
    if row is None:
        print(f"error: post not found or not in status {'/'.join(from_statuses)}", file=sys.stderr)
        return 2
    print(f"{row['id']}  {row['status']}{note}")
    return 0


def _cmd_list(args) -> int:
    for post in _store().list_posts(args.status, args.limit):
        print(f"{post['id']}  {post['status']:<10} {post['scheduled_at']}  tries={post['attempts']}  "
              f"{_preview(post['message'])}")
    return 0


def _cmd_history(args) -> int:
    store = _store()
    if args.id:
        post = store.get_post(args.id)
        if not post:
            print("error: post not found", file=sys.stderr)
            return 2
        print(f"{post['id']}  {post['status']}  attempts={post['attempts']}/{post['max_attempts']}")
        for key in ("published_at", "permalink", "last_error"):
            if post.get(key):
                print(f"  {key}: {post[key]}")
    for a in store.list_attempts(args.id, args.limit):
        print(f"  #{a['attempt_no']} {a['finished_at']}  {a['outcome']}  http={a.get('http_status')}  "
              f"code={a.get('error_code')}  {_preview(a.get('error_message') or a.get('external_id') or '', 80)}")
    return 0


def _cmd_run(args) -> int:
    summary = run_once(_store(), build_all_publishers(), load_worker_settings())
    print(f"published={summary.published} retried={summary.retried} failed={summary.failed}")
    for note in summary.notes:
        print(f"note: {note}")
    return 1 if summary.failed else 0  # a red workflow run is the free "something needs attention" alert


def _cmd_doctor(args) -> int:
    from .doctor import run_doctor
    return run_doctor(os.environ)


def _cmd_connect(args) -> int:
    from .oauth_connect import run_connect
    return run_connect(os.environ, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="naijalearn_publisher")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="queue a post as a draft")
    body = add.add_mutually_exclusive_group(required=True)
    body.add_argument("--message")
    body.add_argument("--message-file")
    add.add_argument("--at", required=True, help="ISO 8601 time with offset, e.g. 2026-10-20T09:00:00+01:00")
    add.add_argument("--link")
    add.add_argument("--image-url")
    add.add_argument("--destination", default="facebook_page")
    add.add_argument("--max-attempts", type=int, default=5)
    add.add_argument("--approve", action="store_true", help="approve immediately")
    add.add_argument("--allow-duplicate", action="store_true")
    add.set_defaults(func=_cmd_add)

    for name, help_text in (("approve", "approve a draft for publishing"), ("cancel", "cancel a post"),
                            ("requeue", "retry a failed post (check the Page first)")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("id")
        cmd.set_defaults(func={
            "approve": lambda a: _cmd_transition(a, ["draft"], "approved", {"approved_at": _now_iso()}),
            "cancel": lambda a: _cmd_transition(a, ["draft", "approved", "failed"], "cancelled"),
            "requeue": lambda a: _cmd_transition(a, ["failed"], "approved", {
                "attempts": 0, "next_attempt_at": _now_iso(), "last_error": None},
                " (verify it is not already on the Page)"),
        }[name])

    listing = sub.add_parser("list", help="list queued posts")
    listing.add_argument("--status")
    listing.add_argument("--limit", type=int, default=30)
    listing.set_defaults(func=_cmd_list)

    history = sub.add_parser("history", help="show attempts (all, or for one post id)")
    history.add_argument("id", nargs="?")
    history.add_argument("--limit", type=int, default=30)
    history.set_defaults(func=_cmd_history)

    sub.add_parser("run", help="publish everything that is due").set_defaults(func=_cmd_run)
    sub.add_parser("doctor", help="check configuration, token and permissions").set_defaults(func=_cmd_doctor)
    connect = sub.add_parser("connect", help="OAuth: obtain a Page access token")
    connect.add_argument("--no-browser", action="store_true")
    connect.set_defaults(func=_cmd_connect)
    return parser


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, StoreError, ValueError, UnsupportedDestination) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
