"""Amount and time rendering."""

from __future__ import annotations

import pytest

from app.web.format import format_amount, relative_time


@pytest.mark.parametrize(
    "raw,decimals,expected",
    [
        (2_400_000_000_000_000_000, 18, "2.4"),
        (4_821_000_000_000_000_000, 18, "4.821"),
        (31_420_000_000_000_000_000, 18, "31.42"),
        (120_000_000_000_000_000, 18, "0.12"),
        (500_000_000, 6, "500"),
        (0, 18, "0"),
        (5 * 10**18, 18, "5"),
        (1, 18, "<0.000001"),  # dust is flagged, never rounded to a bare 0
        (123_456_789_012_345_678_901_234, 18, "123,456.79"),
        (10**30, 18, "1,000,000,000,000"),
        (-2_500_000_000_000_000_000, 18, "-2.5"),
        (12345, 0, "12,345"),
    ],
)
def test_format_amount(raw, decimals, expected):
    assert format_amount(raw, decimals) == expected


def test_format_amount_accepts_a_string_from_sqlite():
    assert format_amount("2400000000000000000", 18) == "2.4"


def test_format_amount_is_exact_beyond_float_precision():
    raw = 123_456_789_012_345_678_901_234_567
    assert format_amount(raw, 0) == "123,456,789,012,345,678,901,234,567"


def test_format_amount_survives_absurd_token_supplies():
    """decimal's default 28-digit context is smaller than a uint256, and a spam
    token minting 2**256-1 units must render rather than raise."""
    assert format_amount(2**256 - 1, 18) == (
        "115,792,089,237,316,195,423,570,985,008,687,907,853,269,984,665,640,564,039,457.58"
    )
    assert format_amount(10**40, 0).count(",") == 13


@pytest.mark.parametrize(
    "age,expected",
    [(0, "0s ago"), (8, "8s ago"), (59, "59s ago"), (60, "1m ago"), (3600, "1h ago"), (172_800, "2d ago")],
)
def test_relative_time(age, expected):
    assert relative_time(1_000_000 - age, now=1_000_000) == expected


def test_relative_time_without_a_timestamp():
    assert relative_time(None) == "never"


# ----------------------------------------------------------- day labelling


def test_day_label_uses_words_for_the_recent_past():
    from datetime import date

    from app.web.format import day_label

    today = date(2026, 9, 3)
    assert day_label(date(2026, 9, 3), today=today) == "Today"
    assert day_label(date(2026, 9, 2), today=today) == "Yesterday"
    assert day_label(date(2026, 9, 1), today=today) == "Sep 1"
    assert day_label(date(2026, 8, 31), today=today) == "Aug 31"
    assert day_label(date(2025, 12, 25), today=today) == "Dec 25, 2025"


def test_day_label_has_no_leading_zero():
    from datetime import date

    from app.web.format import day_label

    assert day_label(date(2026, 3, 5), today=date(2026, 3, 20)) == "Mar 5"
