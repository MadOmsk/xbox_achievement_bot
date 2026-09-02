"""Small shared helpers. Anything bigger belongs in a service."""

from datetime import UTC, datetime


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
