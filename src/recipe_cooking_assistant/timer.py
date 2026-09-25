"""Source-duration parsing and a single-step timer clock.

Durations come only from explicit wording in the current step. Nothing here
calls a model or writes recipe data.

Range policy: one range such as "8–10 minutes" starts at the lower value and
the full range stays visible, so the cook can begin checking early. Wording
like "about" stays marked approximate. Two or more independent durations in
one step do not start a timer. Adjacent "1 hour 30 minutes" or
"1 hour and 30 minutes" is one compound duration, not two timers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = r"(?:\d+(?:\.\d+)?)"
_RANGE = rf"(?P<low>{_NUMBER})\s*(?:–|—|-|to)\s*(?P<high>{_NUMBER})"
_UNIT = (
    r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)"
)
_APPROX = r"(?:about|around|approximately|approx\.?|roughly|~)\s+"
_DURATION = re.compile(
    rf"(?P<approx>{_APPROX})?(?:{_RANGE}|(?P<single>{_NUMBER}))\s*{_UNIT}\b",
    re.IGNORECASE,
)
_COMPOUND = re.compile(
    rf"(?P<approx>{_APPROX})?(?P<hours>{_NUMBER})\s*(?:hours?|hrs?)\s+(?:and\s+)?"
    rf"(?P<minutes>{_NUMBER})\s*(?:minutes?|mins?)\b",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "second": 1,
    "seconds": 1,
    "sec": 1,
    "secs": 1,
    "minute": 60,
    "minutes": 60,
    "min": 60,
    "mins": 60,
    "hour": 3600,
    "hours": 3600,
    "hr": 3600,
    "hrs": 3600,
}


@dataclass(frozen=True)
class TimerOffer:
    """One reliable duration. seconds is the value the timer should start from."""

    seconds: int
    label: str
    approximate: bool
    range_label: str | None = None


@dataclass(frozen=True)
class AmbiguousDurations:
    labels: tuple[str, ...]


def parse_step_timer(text: str) -> TimerOffer | AmbiguousDurations | None:
    """Return one offer, an ambiguous set, or None when no explicit duration exists."""
    if _COMPOUND.search(text) and len(_spans(text)) == 1:
        match = _COMPOUND.search(text)
        assert match is not None
        hours = float(match.group("hours"))
        minutes = float(match.group("minutes"))
        total = int(round(hours * 3600 + minutes * 60))
        if total <= 0:
            return None
        label = _format_seconds(total)
        return TimerOffer(
            seconds=total,
            label=label,
            approximate=bool(match.group("approx")),
        )

    spans = _spans(text)
    if not spans:
        return None
    if len(spans) > 1:
        return AmbiguousDurations(tuple(span.label for span in spans))
    span = spans[0]
    return TimerOffer(
        seconds=span.seconds,
        label=span.label,
        approximate=span.approximate,
        range_label=span.range_label,
    )


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    seconds: int
    label: str
    approximate: bool
    range_label: str | None


def _spans(text: str) -> list[_Span]:
    found: list[_Span] = []
    for match in _DURATION.finditer(text):
        unit = match.group("unit").lower()
        factor = _UNIT_SECONDS[unit]
        approximate = bool(match.group("approx"))
        if match.group("low"):
            low = float(match.group("low"))
            high = float(match.group("high"))
            if high < low:
                low, high = high, low
            seconds = int(round(low * factor))
            if seconds <= 0:
                continue
            range_label = f"{_trim_number(low)}–{_trim_number(high)} {_unit_word(unit, high)}"
            label = range_label
            found.append(
                _Span(
                    match.start(),
                    match.end(),
                    seconds,
                    label,
                    approximate,
                    range_label,
                )
            )
        else:
            amount = float(match.group("single"))
            seconds = int(round(amount * factor))
            if seconds <= 0:
                continue
            label = f"{_trim_number(amount)} {_unit_word(unit, amount)}"
            found.append(
                _Span(match.start(), match.end(), seconds, label, approximate, None)
            )
    if not found:
        return []
    # A compound hour+minute phrase is one span for ambiguity checks.
    compound = _COMPOUND.search(text)
    if compound and len(found) == 2 and compound.start() <= found[0].start and compound.end() >= found[1].end:
        return [
            _Span(
                compound.start(),
                compound.end(),
                found[0].seconds + found[1].seconds,
                _format_seconds(found[0].seconds + found[1].seconds),
                bool(compound.group("approx")),
                None,
            )
        ]
    return found


def _trim_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:g}"


def _unit_word(unit: str, amount: float) -> str:
    base = {
        "sec": "second",
        "secs": "second",
        "second": "second",
        "seconds": "second",
        "min": "minute",
        "mins": "minute",
        "minute": "minute",
        "minutes": "minute",
        "hr": "hour",
        "hrs": "hour",
        "hour": "hour",
        "hours": "hour",
    }[unit]
    if abs(amount - 1) > 1e-9:
        return base + "s"
    return base


def _format_seconds(total: int) -> str:
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour" if hours == 1 else f"{hours} hours")
    if minutes:
        parts.append(f"{minutes} minute" if minutes == 1 else f"{minutes} minutes")
    if seconds or not parts:
        parts.append(f"{seconds} second" if seconds == 1 else f"{seconds} seconds")
    return " ".join(parts)


def format_clock(total_seconds: int) -> str:
    total = max(0, int(total_seconds))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def storage_key(recipe_id: str, step_number: int) -> str:
    return f"rca-timer:{recipe_id}:{step_number}"


@dataclass
class TimerClock:
    """In-browser timer contract. Expiration does not advance a cooking step."""

    recipe_id: str
    step_number: int
    duration_ms: int
    remaining_ms: int
    running: bool = False
    anchor_ms: int | None = None
    expired: bool = False

    def start(self, now_ms: int) -> None:
        self.remaining_ms = self.duration_ms
        self.running = True
        self.anchor_ms = now_ms
        self.expired = False

    def pause(self, now_ms: int) -> None:
        self.remaining_ms = self.view(now_ms)
        self.running = False
        self.anchor_ms = None

    def resume(self, now_ms: int) -> None:
        if self.expired or self.remaining_ms <= 0:
            return
        self.running = True
        self.anchor_ms = now_ms

    def reset(self) -> None:
        self.remaining_ms = self.duration_ms
        self.running = False
        self.anchor_ms = None
        self.expired = False

    def view(self, now_ms: int) -> int:
        remaining = self.remaining_ms
        if self.running and self.anchor_ms is not None:
            remaining = self.remaining_ms - max(0, now_ms - self.anchor_ms)
        if remaining <= 0:
            self.remaining_ms = 0
            self.running = False
            self.anchor_ms = None
            self.expired = True
            return 0
        return remaining

    def snapshot(self, now_ms: int) -> dict[str, int | bool | str | None]:
        remaining = self.view(now_ms)
        if self.running:
            self.remaining_ms = remaining
            self.anchor_ms = now_ms
        return {
            "key": storage_key(self.recipe_id, self.step_number),
            "recipe_id": self.recipe_id,
            "step_number": self.step_number,
            "duration_ms": self.duration_ms,
            "remaining_ms": remaining,
            "running": self.running,
            "anchor_ms": self.anchor_ms,
            "expired": self.expired,
            "advances_step": False,
        }

    @classmethod
    def restore(cls, payload: dict[str, object], now_ms: int) -> TimerClock:
        clock = cls(
            recipe_id=str(payload["recipe_id"]),
            step_number=int(payload["step_number"]),  # type: ignore[arg-type]
            duration_ms=int(payload["duration_ms"]),  # type: ignore[arg-type]
            remaining_ms=int(payload["remaining_ms"]),  # type: ignore[arg-type]
            running=bool(payload["running"]),
            anchor_ms=int(payload["anchor_ms"]) if payload.get("anchor_ms") is not None else None,  # type: ignore[arg-type]
            expired=bool(payload["expired"]),
        )
        clock.view(now_ms)
        return clock
