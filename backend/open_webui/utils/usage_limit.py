"""Detect provider usage limits and work out when they reset.

Used by the chat queue (``utils/chat_queue.py``) to pause when a limit is hit
(Anthropic rate limit, API spend cap, subscription window, overload...) and to
resume automatically once it is over.

Everything here is pure: no I/O, no database. Times are epoch seconds (float).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional

try:  # zoneinfo is stdlib on 3.9+, tzdata may be missing on Windows.
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

KIND_RATE_LIMIT = 'rate_limit'  # short window (per minute): retry soon
KIND_USAGE_LIMIT = 'usage_limit'  # long window (5h subscription window, monthly cap...)
KIND_OVERLOADED = 'overloaded'  # provider capacity problem, retry with backoff
KIND_CREDIT = 'credit'  # out of credit: only the user can fix it

# Never wait less / more than this, whatever the provider says.
MIN_WAIT_SECONDS = 5
MAX_WAIT_SECONDS = 35 * 24 * 3600  # a monthly API spend cap can be ~a month away

_TEXT_LIMIT_PATTERNS = [
    (KIND_CREDIT, re.compile(r'credit balance is too low|insufficient (?:credits?|funds|balance)', re.I)),
    (
        KIND_USAGE_LIMIT,
        re.compile(
            r'usage limit(?:s)? (?:reached|exceeded|hit)'
            r'|(?:reached|hit|exceeded) (?:your|the) (?:\w+[ -]){0,3}(?:usage )?limit'
            r'|(?:you(?:\'|’)?ve|you have) (?:reached|hit|used)'
            r'.{0,40}(?:limit|quota|cap)'
            r'|regain access'
            r'|(?:5|five)[- ]hour (?:usage )?(?:limit|window)'
            r'|weekly (?:usage )?limit'
            r'|monthly (?:usage )?(?:limit|quota|cap)'
            r'|quota (?:exceeded|exhausted|reached)'
            r'|exceeded your current quota'
            r'|\blimit (?:was |has been )?(?:reached|exceeded|hit)\b'
            r'|limit will reset|limit resets?',
            re.I,
        ),
    ),
    (
        KIND_RATE_LIMIT,
        re.compile(
            r'rate[_ -]?limit(?:ed|_error)?'
            r'|too many requests'
            r'|requests? per minute|tokens? per minute|\brpm\b|\btpm\b'
            r'|throttl(?:ed|ing)',
            re.I,
        ),
    ),
    (
        KIND_OVERLOADED,
        re.compile(r'overloaded(?:_error)?|at capacity|service (?:is )?(?:temporarily )?unavailable', re.I),
    ),
]

# Text that looks like a limit but is not one (context size, output size...).
_FALSE_POSITIVES = re.compile(
    r'context[ _-]?(?:length|window)|maximum context|max(?:imum)? (?:output )?tokens|prompt is too long'
    r'|input is too long|request too large|content[ _-]?filter',
    re.I,
)

_UNITS = {
    's': 1,
    'sec': 1,
    'secs': 1,
    'second': 1,
    'seconds': 1,
    'm': 60,
    'min': 60,
    'mins': 60,
    'minute': 60,
    'minutes': 60,
    'h': 3600,
    'hr': 3600,
    'hrs': 3600,
    'hour': 3600,
    'hours': 3600,
    'd': 86400,
    'day': 86400,
    'days': 86400,
}
_DURATION_PART = re.compile(
    r'(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])', re.I
)
_RELATIVE = re.compile(
    r'(?:in|after|within|for)\s+(?:about |around |approximately |~)?'
    r'((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])[\s,]*(?:and\s+)?)+)',
    re.I,
)
_ISO = re.compile(
    r'(\d{4}-\d{2}-\d{2})(?:[T ]|\s+at\s+)(\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?\s*(Z|UTC|GMT|[+-]\d{2}:?\d{2})?',
    re.I,
)
_CLAUDE_CODE_EPOCH = re.compile(r'limit reached\s*\|\s*(\d{10,13})', re.I)
_CLOCK = re.compile(
    r'(?:reset(?:s)?|resume(?:s)?|available|renew(?:s)?|try again|until|at)\s*(?:again\s*)?(?:at|on|around|by)?\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?'
    r'(?:\s*\(([A-Za-z_]+/[A-Za-z_]+|UTC|GMT)\))?',
    re.I,
)


@dataclass
class LimitInfo:
    kind: str
    message: str = ''
    status: Optional[int] = None
    # Absolute time (epoch seconds) when the limit is over, if the provider said so.
    reset_at: Optional[float] = None
    # How the reset time was found: header | iso | relative | clock | epoch | None
    source: Optional[str] = None
    details: dict = field(default_factory=dict)

    def wait_seconds(self, now: Optional[float] = None) -> Optional[float]:
        if self.reset_at is None:
            return None
        return max(0.0, self.reset_at - (now if now is not None else time.time()))

    def to_dict(self) -> dict:
        return {
            'kind': self.kind,
            'message': self.message[:500],
            'status': self.status,
            'reset_at': self.reset_at,
            'source': self.source,
        }


def error_to_text(error: Any) -> str:
    """Flatten whatever the middleware stored as an error into searchable text."""
    if error is None:
        return ''
    if isinstance(error, str):
        return error
    if isinstance(error, (bytes, bytearray)):
        return bytes(error).decode('utf-8', 'replace')
    if isinstance(error, Mapping):
        parts = []
        for key in ('type', 'code', 'status', 'message', 'detail', 'content', 'error'):
            value = error.get(key)
            if value is not None:
                parts.append(error_to_text(value))
        if not parts:
            try:
                return json.dumps(error, ensure_ascii=False, default=str)
            except Exception:
                return str(error)
        return ' '.join(p for p in parts if p)
    if isinstance(error, (list, tuple)):
        return ' '.join(error_to_text(item) for item in error)
    return str(error)


def _clamp_reset(reset_at: float, now: float) -> float:
    return min(max(reset_at, now + MIN_WAIT_SECONDS), now + MAX_WAIT_SECONDS)


def _to_zone(name: Optional[str]):
    if not name:
        return None
    if name.upper() in ('UTC', 'GMT', 'Z'):
        return timezone.utc
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def parse_duration_seconds(text: str) -> Optional[float]:
    """'4 hours 23 minutes' -> 15780.0, '2h30m' -> 9000.0."""
    total = 0.0
    found = False
    for number, unit in _DURATION_PART.findall(text):
        total += float(number) * _UNITS[unit.lower()]
        found = True
    return total if found else None


def _parse_iso(match: re.Match) -> Optional[float]:
    day, clock, zone = match.group(1), match.group(2), match.group(3)
    if len(clock) == 5:
        clock += ':00'
    try:
        parsed = datetime.fromisoformat(f'{day}T{clock}')
    except ValueError:
        return None
    if zone and zone.upper() not in ('Z', 'UTC', 'GMT'):
        sign = 1 if zone[0] == '+' else -1
        digits = zone[1:].replace(':', '')
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:] or 0))
        return (parsed.replace(tzinfo=timezone(sign * offset))).timestamp()
    # No zone, or explicit UTC: providers that omit it use UTC.
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _parse_clock(match: re.Match, now: float, tz_hint: Optional[str]) -> Optional[float]:
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or '').lower().replace('.', '')
    zone_name = match.group(4)
    # A bare number such as "at 3" is far too ambiguous to trust.
    if not match.group(2) and not meridiem:
        return None
    if meridiem == 'pm' and hour < 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    zone = _to_zone(zone_name) or _to_zone(tz_hint)
    base = datetime.fromtimestamp(now, tz=zone) if zone else datetime.fromtimestamp(now).astimezone()
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _headers_reset(headers: Mapping[str, str], now: float) -> Optional[float]:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    candidates: list[float] = []

    if 'retry-after-ms' in lowered:
        try:
            candidates.append(now + float(lowered['retry-after-ms']) / 1000.0)
        except ValueError:
            pass
    if 'retry-after' in lowered:
        raw = lowered['retry-after'].strip()
        try:
            candidates.append(now + float(raw))
        except ValueError:
            try:
                candidates.append(parsedate_to_datetime(raw).timestamp())
            except Exception:
                pass

    # Anthropic: one reset per bucket (requests / tokens / input / output). Only the
    # exhausted buckets matter, otherwise a long-reset bucket we are not blocked on
    # would wrongly delay us.
    for key, value in lowered.items():
        if not re.fullmatch(r'(?:anthropic|x)-ratelimit-[a-z-]+?-reset', key):
            continue
        remaining = lowered.get(key[: -len('-reset')] + '-remaining')
        if remaining is not None and remaining.strip() not in ('0', '0.0'):
            continue
        try:
            candidates.append(datetime.fromisoformat(value.strip().replace('Z', '+00:00')).timestamp())
        except ValueError:
            seconds = parse_duration_seconds(value)
            if seconds is not None:
                candidates.append(now + seconds)

    if not candidates:
        return None
    return max(candidates)


def detect_limit(
    error: Any,
    *,
    status: Optional[int] = None,
    headers: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
    tz: Optional[str] = None,
) -> Optional[LimitInfo]:
    """Return a LimitInfo if ``error`` is a usage/rate limit, else None.

    ``tz`` is an IANA zone name used to read clock times that carry no zone
    ("resets at 5pm"); defaults to the server's local zone.
    """
    now = now if now is not None else time.time()
    text = error_to_text(error)
    if not text and status is None:
        return None

    kind: Optional[str] = None
    if status == 429:
        kind = KIND_RATE_LIMIT
    elif status in (503, 529):
        kind = KIND_OVERLOADED

    if not (_FALSE_POSITIVES.search(text) and status != 429):
        for candidate_kind, pattern in _TEXT_LIMIT_PATTERNS:
            if pattern.search(text):
                # Text is more precise than a bare 429: a "429 ... usage limit reached"
                # is a long window, not a per-minute throttle.
                if kind is None or (kind == KIND_RATE_LIMIT and candidate_kind in (KIND_USAGE_LIMIT, KIND_CREDIT)):
                    kind = candidate_kind
                break
    if kind is None:
        return None

    info = LimitInfo(kind=kind, message=text.strip()[:1000], status=status)

    reset: Optional[float] = None
    if headers:
        reset = _headers_reset(headers, now)
        if reset is not None:
            info.source = 'header'

    if reset is None:
        match = _CLAUDE_CODE_EPOCH.search(text)
        if match:
            value = float(match.group(1))
            reset = value / 1000.0 if value > 1e11 else value
            info.source = 'epoch'

    if reset is None:
        match = _ISO.search(text)
        if match:
            reset = _parse_iso(match)
            info.source = 'iso' if reset is not None else None

    if reset is None:
        match = _RELATIVE.search(text)
        if match:
            seconds = parse_duration_seconds(match.group(1))
            if seconds is not None:
                reset = now + seconds
                info.source = 'relative'

    if reset is None and kind in (KIND_USAGE_LIMIT, KIND_RATE_LIMIT, KIND_CREDIT):
        match = _CLOCK.search(text)
        if match:
            reset = _parse_clock(match, now, tz)
            info.source = 'clock' if reset is not None else None

    if reset is not None:
        info.reset_at = _clamp_reset(reset, now)
    else:
        info.source = None
    return info


def default_wait_seconds(kind: str, attempt: int, *, max_probe: float = 1800) -> float:
    """Delay before trying again when the provider gave no reset time.

    Nothing is announced, so we *probe*: a refused request costs nothing and tells us the
    limit is over as soon as it is. Delays grow with each failed probe:

      usage_limit   10 min, 15, 22, 30 min (capped by ``max_probe``)  - long windows (5 h...)
      rate_limit    1 min, 2, 4 ... 10 min
      overloaded    30 s, 1 min, 2 ... 10 min
      credit        30 min (the user has to top up)
    """
    attempt = max(0, attempt)
    if kind == KIND_CREDIT:
        return 30 * 60
    if kind == KIND_USAGE_LIMIT:
        return float(min(600 * (1.5**attempt), max_probe))
    base = 60 if kind == KIND_RATE_LIMIT else 30
    return float(min(base * (2**attempt), 600))


def format_wait(seconds: float) -> str:
    """Human readable: 3h 12m, 45m, 30s."""
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}h {minutes:02d}m'
    if minutes:
        return f'{minutes}m'
    return f'{secs}s'
