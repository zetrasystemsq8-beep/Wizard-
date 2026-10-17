"""Destination registry. To add a destination: write a Publisher subclass, add a builder here,
and widen the destination CHECK constraint in the SQL migration. Facebook Groups is deliberately absent."""
from __future__ import annotations

import os
from typing import Callable, Mapping

from ..config import load_facebook_settings
from .base import Publisher
from .facebook_page import FacebookPagePublisher


class UnsupportedDestination(ValueError):
    pass


def _facebook_page(env: Mapping[str, str]) -> Publisher:
    return FacebookPagePublisher(load_facebook_settings(env))


DESTINATION_BUILDERS: dict[str, Callable[[Mapping[str, str]], Publisher]] = {
    "facebook_page": _facebook_page,
}


def supported_destinations() -> list[str]:
    return sorted(DESTINATION_BUILDERS)


def build_publisher(name: str, env: Mapping[str, str] | None = None) -> Publisher:
    env = os.environ if env is None else env
    builder = DESTINATION_BUILDERS.get(name)
    if builder is None:
        hint = ""
        if "group" in name.lower():
            hint = " Facebook Groups publishing is not offered by Meta's official Pages API."
        raise UnsupportedDestination(
            f"Unsupported destination {name!r}. Supported: {', '.join(supported_destinations())}.{hint}"
        )
    return builder(env)


def build_all_publishers(env: Mapping[str, str] | None = None) -> dict[str, Publisher]:
    return {name: build_publisher(name, env) for name in supported_destinations()}
