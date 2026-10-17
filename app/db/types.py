"""Custom column types."""

from decimal import Decimal

from sqlalchemy import BigInteger
from sqlalchemy.types import TypeDecorator

MINOR_UNIT_DIGITS = 2  # INR, USD, EUR, ... all use 2 decimal places


class Money(TypeDecorator):
    """A Decimal in Python, stored as an integer count of minor units (e.g. paise).

    SQLite has no exact decimal type, so storing integers avoids float rounding.
    Floats are rejected, and so are values with more precision than the minor unit,
    so amounts are never rounded silently.
    """

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, float | bool) or not isinstance(value, Decimal | int | str):
            raise TypeError(f"Money expects Decimal, int or str, got {type(value).__name__}")
        amount = Decimal(value)
        if not amount.is_finite():
            raise ValueError(f"Money must be finite, got {value}")
        minor = amount.scaleb(MINOR_UNIT_DIGITS)
        if minor != minor.to_integral_value():
            raise ValueError(f"{value} has more than {MINOR_UNIT_DIGITS} decimal places")
        return int(minor)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(value).scaleb(-MINOR_UNIT_DIGITS)
