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
