from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))
CACHED_WEIGHT = 0.1
OUTPUT_WEIGHT = 8


def credits(
    input_tokens: int | None, cached_tokens: int | None, output_tokens: int | None
) -> int:
    fresh = (input_tokens or 0) - (cached_tokens or 0)
    return round(
        fresh
        + (cached_tokens or 0) * CACHED_WEIGHT
        + (output_tokens or 0) * OUTPUT_WEIGHT
    )


def today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(KST).date()


def resets_at(now: datetime | None = None) -> datetime:
    midnight = datetime.combine(
        today(now) + timedelta(days=1), datetime.min.time(), KST
    )
    return midnight.astimezone(UTC)


def usage_json(used: int, limit: int) -> dict[str, Any]:
    return {
        "used": used,
        "limit": limit,
        "resets_at": resets_at()
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
