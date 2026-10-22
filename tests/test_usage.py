from datetime import UTC, datetime

from app.usage import credits, resets_at, today


def test_cached_input_is_cheap_and_output_is_dear():
    assert credits(1000, 0, 0) == 1000
    assert credits(1000, 1000, 0) == 100
    assert credits(0, 0, 100) == 800
    assert credits(None, None, None) == 0


def test_the_day_turns_over_at_midnight_in_korea():
    before = datetime(2026, 10, 22, 14, 59, tzinfo=UTC)
    after = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)

    assert str(today(before)) == "2026-10-22"
    assert str(today(after)) == "2026-10-23"
    assert resets_at(before) == datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    assert resets_at(after) == datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
