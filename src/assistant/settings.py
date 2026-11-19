"""Per-conversation model, effort and fast mode: validation, defaults, and the /status and /context text."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

ChangeKind = Literal["model", "effort", "fast"]
Freshness = Literal["live", "busy", "paused", "stale"]


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    label: str
    fast: bool


MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("claude-opus-5-5", "Opus 5.5", True),
    ModelInfo("claude-sonnet-5-5", "Sonnet 5.5", False),
    ModelInfo("claude-haiku-5-5", "Haiku 5.5", False),
    ModelInfo("claude-fable-5-2", "Fable 5.2", True),
)
MODEL_BY_ID = {info.model_id: info for info in MODELS}
DEFAULT_MODEL = "claude-sonnet-5-5"
FAST_MODEL = "claude-opus-5-5"
FABLE_MODEL = "claude-fable-5-2"
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
# Verified with a scratch client (SDK 0.2.183): without it the CLI reports fast_mode_disabled_reason=sdk_opt_in_required.
FAST_SETTINGS = '{"fastMode": true}'

FABLE_WARNING = "Fable uses far more of your plan's limits."
FAST_MODELS_TEXT = "fast mode only runs on Opus 5.5 and Fable 5.2"
FAST_SPEED_NOTE = "It can still answer at standard speed; /status shows the last reply's speed."
PAUSED_NOTE = "(paused; refreshed on your next message)"
BUSY_NOTE = "(a reply is running; numbers are from the last one)"
STALE_NOTE = "(live numbers unavailable; last known shown)"
TOP_CATEGORIES = 8
_NOT_USAGE = frozenset({"free space"})


class SettingError(ValueError):
    pass


def model_label(model_id: str) -> str:
    info = MODEL_BY_ID.get(model_id)
    return info.label if info else model_id


def supports_fast(model_id: str) -> bool:
    info = MODEL_BY_ID.get(model_id)
    return bool(info and info.fast)


@dataclass(frozen=True)
class Effective:
    model: str
    effort: str
    fast: bool


@dataclass(frozen=True)
class ConversationSettings:
    """None / False = the default; a fresh conversation starts with all defaults."""

    model: str | None = None
    effort: str | None = None
    fast: bool = False

    def effective(self, default_model: str) -> Effective:
        return Effective(self.model or default_model, self.effort or DEFAULT_EFFORT, self.fast)


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    value: str

    def describe(self) -> str:
        return f"{self.kind}={self.value}"


def _change_model(current: ConversationSettings, model_id: str) -> tuple[ConversationSettings, str]:
    if model_id not in MODEL_BY_ID:
        raise SettingError(f"Unknown model {model_id!r}. Choose one of: {', '.join(MODEL_BY_ID)}")
    lines = [f"Model: {model_label(model_id)}."]
    fast = current.fast and supports_fast(model_id)
    if current.fast and not fast:
        lines.append(f"Fast mode is now off: {FAST_MODELS_TEXT}.")
    if model_id == FABLE_MODEL:
        lines.append(FABLE_WARNING)
    return replace(current, model=model_id, fast=fast), "\n".join(lines)


def _change_effort(current: ConversationSettings, effort: str) -> tuple[ConversationSettings, str]:
    if effort not in EFFORTS:
        raise SettingError(f"Unknown effort {effort!r}. Choose one of: {', '.join(EFFORTS)}")
    return replace(current, effort=effort), f"Effort: {effort}."


def _change_fast(current: ConversationSettings, value: str, default_model: str) -> tuple[ConversationSettings, str]:
    if value not in ("on", "off"):
        raise SettingError(f"Fast mode is 'on' or 'off', not {value!r}")
    if value == "off":
        return replace(current, fast=False), "Fast mode: off."
    model = current.effective(default_model).model
    lines = ["Fast mode: on."]
    if not supports_fast(model):
        lines.append(f"Switched to {model_label(FAST_MODEL)}: {FAST_MODELS_TEXT}.")
        model = FAST_MODEL
    lines.append(FAST_SPEED_NOTE)
    # The model is pinned so a later default-model change can't leave fast on a model without it.
    return replace(current, model=model, fast=True), "\n".join(lines)


def apply_change(current: ConversationSettings, change: Change, default_model: str) -> tuple[ConversationSettings, str]:
    """The new settings and the reply line(s) saying what changed; SettingError on a bad value."""
    if change.kind == "model":
        return _change_model(current, change.value)
    if change.kind == "effort":
        return _change_effort(current, change.value)
    if change.kind == "fast":
        return _change_fast(current, change.value, default_model)
    raise SettingError(f"Unknown setting {change.kind!r}")


@dataclass(frozen=True)
class ContextSnapshot:
    total: int
    maximum: int
    percentage: float
    categories: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_usage(cls, usage: Mapping[str, Any]) -> ContextSnapshot:
        """From get_context_usage(): the biggest non-deferred categories, free space left out."""
        raw = [c for c in usage.get("categories") or () if not c.get("isDeferred")]
        used = [(str(c["name"]), int(c["tokens"])) for c in raw
                if int(c.get("tokens") or 0) > 0 and str(c.get("name", "")).lower() not in _NOT_USAGE]
        used.sort(key=lambda item: item[1], reverse=True)
        return cls(int(usage["totalTokens"]), int(usage["maxTokens"]), float(usage["percentage"]),
                   tuple(used[:TOP_CATEGORIES]))


@dataclass(frozen=True)
class FastInfo:
    """What the CLI reported: init `fast_mode_state` / reason, and the last reply's `usage.speed`."""

    state: str | None = None
    reason: str | None = None
    speed: str | None = None


@dataclass(frozen=True)
class StatusView:
    effective: Effective
    fast_info: FastInfo
    context: ContextSnapshot | None
    freshness: Freshness
    mode: str
    started_at: float | None
    remote: str | None = None


def describe_remote(on: bool, live: bool, state: str | None) -> str:
    if not on:
        return "remote off"
    if not live:
        return "remote on (paused)"
    return f"remote on ({state})" if state else "remote on"


def format_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.3g}M"


def format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} d ago"


def describe_fast(fast: bool, info: FastInfo) -> str:
    """Never claims a fast reply unless the last reply's usage said so."""
    if not fast:
        return "fast off"
    if info.state is None:
        return "fast on (no reply yet)"
    if info.state != "on":
        reason = f": {info.reason}" if info.reason else ""
        return f"fast on, but the CLI says {info.state}{reason}"
    return f"fast on (last reply: {info.speed})" if info.speed else "fast on"


def describe_context(context: ContextSnapshot | None) -> str:
    if context is None:
        return "context n/a (no reply yet)"
    return f"context {context.percentage:.0f}% ({format_tokens(context.total)}/{format_tokens(context.maximum)})"


def freshness_note(freshness: Freshness) -> str:
    return {"live": "", "busy": BUSY_NOTE, "paused": PAUSED_NOTE, "stale": STALE_NOTE}[freshness]


def format_status(view: StatusView, now: float) -> str:
    started = f"started {format_age(now - view.started_at)}" if view.started_at else "not started yet"
    parts = [model_label(view.effective.model), view.effective.effort, describe_fast(view.effective.fast, view.fast_info),
             describe_context(view.context), view.mode, started, *([view.remote] if view.remote else [])]
    note = freshness_note(view.freshness) if view.context is not None else ""
    return " · ".join(parts) + (f"\n{note}" if note else "")


def format_context(view: StatusView) -> str:
    text = describe_context(view.context)
    head = f"{text[:1].upper()}{text[1:]} · {model_label(view.effective.model)}"
    lines = [head]
    if view.context is not None:
        lines.extend(f"- {name}: {format_tokens(tokens)}" for name, tokens in view.context.categories)
        note = freshness_note(view.freshness)
        if note:
            lines.append(note)
    return "\n".join(lines)
