"""Conversation keys: `dm:<user>`, `thread:<post or thread>`, `pthread:<thread>`; legacy `priv:`/`pub:<channel>`.

Mode always follows the key.
"""

from __future__ import annotations

from typing import Literal

Kind = Literal["dm", "thread", "pthread", "priv", "pub"]
Mode = Literal["private", "public"]

DM: Kind = "dm"
THREAD: Kind = "thread"
PUBLIC_THREAD: Kind = "pthread"
# v3 channel-level conversations: no longer created; old records still load and expire.
PRIVATE_CHANNEL: Kind = "priv"
PUBLIC_CHANNEL: Kind = "pub"
PRIVATE: Mode = "private"
PUBLIC: Mode = "public"

KINDS: tuple[Kind, ...] = (DM, THREAD, PUBLIC_THREAD, PRIVATE_CHANNEL, PUBLIC_CHANNEL)
THREAD_KINDS: tuple[Kind, ...] = (THREAD, PUBLIC_THREAD)
PUBLIC_KINDS: tuple[Kind, ...] = (PUBLIC_THREAD, PUBLIC_CHANNEL)


def make_key(kind: Kind, snowflake: int) -> str:
    return f"{kind}:{snowflake}"


def key_kind(key: str) -> Kind:
    """Unprefixed keys (the console) behave like a DM."""
    prefix = key.partition(":")[0]
    return next((kind for kind in KINDS if kind == prefix), DM)


def mode_for_key(key: str) -> Mode:
    # Derived from the key, never from stored state, so a public key can't end up with the private toolset.
    return PUBLIC if key_kind(key) in PUBLIC_KINDS else PRIVATE


def key_id(key: str) -> int | None:
    prefix, sep, rest = key.partition(":")
    return int(rest) if sep and prefix in KINDS and rest.isdigit() else None


def migrate_key(key: str) -> str:
    """v1 keyed everything on the bare Discord user id of a DM."""
    return make_key(DM, int(key)) if key.isdigit() else key
