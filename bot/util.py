"""Small shared helpers. Anything bigger belongs in a service."""

import re
from datetime import UTC, datetime

# "+3", "-5", "+5:30", "UTC+3" — an optional "UTC" prefix, an optional sign
# (missing sign means positive), 1-2 digit hours, optional ":MM" minutes.
_OFFSET_RE = re.compile(r"^(?:utc)?\s*([+-])?(\d{1,2})(?::([0-5]\d))?$", re.IGNORECASE)


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Timestamps are stored as UTC ISO strings — SQLite has no date type."""
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def mask(secret: str | None) -> str:
    """Render a secret for logs: the length and nothing else (SPEC 1.5).

    Not even a prefix or suffix — a refresh token is long-lived, and partial
    exposure is still exposure. Length is enough to tell "empty" from "present".
    """
    if not secret:
        return "<empty>"
    return f"<secret, {len(secret)} chars>"


def humanize_ago(timestamp: str | None) -> str:
    """ "2 часа назад" — shown in both panels, so it lives here."""
    moment = parse_iso(timestamp)
    if moment is None:
        return "никогда"
    seconds = int((utcnow() - moment).total_seconds())
    if seconds < 120:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def thousands(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def parse_utc_offset(text: str) -> int | None:
    """Free-text timezone entry ("+3", "-5", "+5:30", "UTC+3") to minutes
    from UTC, or None if it doesn't parse or falls outside a real offset
    (-12h..+14h) — the manual alternative to picking a button, same parser
    everywhere a person can type a UTC offset by hand (personal panel,
    onboarding, admin's per-chat timezone, SPEC 6.1.1 and 6.4).
    """
    match = _OFFSET_RE.match(text.strip())
    if match is None:
        return None
    sign, hours_str, minutes_str = match.groups()
    minutes = int(hours_str) * 60 + int(minutes_str or 0)
    if sign == "-":
        minutes = -minutes
    return minutes if -12 * 60 <= minutes <= 14 * 60 else None


def cooldown_minutes_left(last: float | None, now: float, cooldown_seconds: int) -> int:
    """Minutes until a rate-limited action is allowed again; 0 means now.

    Used for every on-demand button or command with a per-target cooldown —
    the panel sync, the admin refresh, /summary — so they all round the same
    way instead of each rolling its own off-by-one.
    """
    if last is None:
        return 0
    waited = now - last
    if waited >= cooldown_seconds:
        return 0
    return int((cooldown_seconds - waited) // 60) + 1
